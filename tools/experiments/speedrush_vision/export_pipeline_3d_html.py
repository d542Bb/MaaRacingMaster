# -*- coding: utf-8 -*-
"""整条感知管线一帧在干什么 → **单文件 3D 可视化 HTML**（canvas-2D 软渲染，零依赖，
无 CDN/无动画）。帧间切换（数字按钮/左右键），看「哪里明显不对」。

**同一帧画四个层**（全部按生产语义离线复算或取 trace 原值，不另写算法）：
- 点云：MoGe 原生点图（infer_points native）抽稀，原帧 RGB / 高度残差两色；
- 路面平面：drivable_grid_from_points 的 coef（拟合失败=无平面，诚实不画）；
- 可行驶栅格：生产 BEV 三态格（可走绿/不可走红，unknown 不画），贴在平面高度上；
- 走廊分带 + 墙线 + 车：走廊带取 **trace scene 原值**（产线实际报告，measured 绿/
  mixed 黄/inferred 灰/occluded 紫）；墙线取 wall_line_reading 影子期分段线（青）；
  车取 trace mcars 锚位（品红）。
- 参考层：地面 1m 网格 + 三轴 + 距离环（与 export_pointcloud_html 同款）。

坐标系：相机系（原点=相机，z 前、y 下、x 右）；走廊带/墙线/车的 z 是车头系
（trace 原值），导出统一加回 CAM_Z_DATUM_M。查看器把 y 翻上来。

用法：
    python tools/experiments/speedrush_vision/export_pipeline_3d_html.py \
        --dir "<APPDATA>/MaaRacingMaster/debug/speedrush/20261020_202843_p2" \
        --fids 8,20,35,50,60,75,95,115
输出：<dir>/pipeline3d.html（浏览器直接打开）
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush import corridor as cr  # noqa: E402
from maaracing_master.plugins.speedrush.wall_track import WallTrack  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import Calib, load_calib  # noqa: E402

Z_CUT = 60.0        # 点云纵向截断（天空/远景不进画）
PT_STRIDE = 3       # 原生点图抽稀（336×598 → 112×200）
GRID_PLANE_H = True # 栅格/走廊带贴平面高度（False=贴参考地面）


def b64(a: np.ndarray) -> str:
    if a.dtype == np.float32:
        return base64.b64encode(a.tobytes()).decode()
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


# ── 参考层（与 export_pointcloud_html 同款思路：采样点画法）─────────────────
def _grid_axes(y0: float, x_span: float = 14.0, z_span: float = 40.0):
    pts, cols = [], []

    def line(a, b, col):
        n = int(np.linalg.norm(np.array(b) - np.array(a)) / 0.5) + 1
        for t in np.linspace(0, 1, n):
            pts.append([a[k] + (b[k] - a[k]) * t for k in range(3)])
            cols.append(col)

    for x in np.arange(-x_span, x_span + 0.1, 1.0):
        col = (190, 190, 190) if abs(x) < 1e-6 else (100, 100, 100)
        line((float(x), y0, 0.0), (float(x), y0, z_span), col)
    for z in np.arange(0.0, z_span + 0.1, 1.0):
        line((-x_span, y0, float(z)), (x_span, y0, float(z)), col)
    line((0, y0, 0), (6, y0, 0), (255, 60, 60))      # X 红=右
    line((0, y0, 0), (0, y0 + 5, 0), (60, 255, 60))  # Y 绿=上
    line((0, y0, 0), (0, y0, 12), (60, 120, 255))    # Z 蓝=前
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


_DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


def _ring_axes(y0: float, radii=(5, 10, 15, 20, 25, 30, 40)):
    pts, cols = [], []
    for r in radii:
        n = int(2 * np.pi * r / 0.5) + 1
        for t in np.linspace(0, 2 * np.pi, n):
            pts.append([r * np.cos(t), y0, r * np.sin(t)])
            cols.append((255, 220, 60))
        for i, ch in enumerate(str(r)):
            for ri, row in enumerate(_DIGITS[ch]):
                for ci, on in enumerate(row):
                    if on == "1":
                        pts.append([r + 0.6 + ci * 0.25, y0,
                                    0.6 + (4 - ri) * 0.25 + i * 1.0])
                        cols.append((240, 240, 240))
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


def _plane_dots(coef, x_span=14.0, z_lo=2.0, z_hi=40.0, y0=0.0):
    """平面采样点（灰青，半覆盖感）：a x + b z + c（相机 y 向下为正）。"""
    a, b, c = coef
    xs = np.arange(-x_span, x_span + 0.1, 1.0)
    zs = np.arange(z_lo, z_hi + 0.1, 1.0)
    pts = [[float(x), float(a * x + b * z + c), float(z)]
           for z in zs for x in xs]
    n = len(pts)
    return np.asarray(pts, np.float32), np.full((n, 3), (90, 170, 200), np.uint8)


_QUAL_COL = {"measured": (60, 220, 90), "mixed": (240, 200, 60),
             "inferred": (140, 140, 140), "occluded": (190, 90, 240),
             "unknown": (120, 120, 120)}


def _bands_dots(bands, coef, y0=0.0):
    """分带走廊左右缘（build_corridor 产物，带 z 车头系负值 → +CAM_Z_DATUM_M）。"""
    pts, cols = [], []
    for bd in bands:
        for b, side in ((bd.left, -1), (bd.right, 1)):
            if b.x_m is None or b.src == "unknown":
                continue
            col = _QUAL_COL.get(str(b.src), (200, 200, 200))
            x = float(b.x_m)
            for z in np.arange(bd.z0, bd.z1 + 0.1, 0.25):
                zc = float(z) + dg.CAM_Z_DATUM_M
                y = y0
                if coef is not None:
                    y = float(coef[0] * x + coef[1] * zc + coef[2])
                pts.append([x, y, zc])
                cols.append(col)
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


def _wall_dots(wlc, coef, y0=0.0):
    """墙线分段（wall_line_reading，车头系）：左黄右橙，粗一档（沿线双行点）。"""
    pts, cols = [], []
    for side, segs in ((-1, wlc.left_segs), (1, wlc.right_segs)):
        col = (255, 230, 80) if side < 0 else (255, 140, 40)
        for s in segs:
            for z in np.arange(s.z0, s.z1 + 0.1, 0.25):
                zc = float(z) + dg.CAM_Z_DATUM_M
                for dx in (-0.12, 0.12):
                    x = float(s.x_at(z)) + dx
                    y = y0
                    if coef is not None:
                        y = float(coef[0] * x + coef[1] * zc + coef[2])
                    pts.append([x, y, zc])
                    cols.append(col)
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


def _car_dots(coef, reading, y0=0.0):
    """车道级左右缘读数锚（品红竖线）：dgeo 近场供数的位置。"""
    pts, cols = [], []
    for lane in (reading.left_edge_lane, reading.right_edge_lane):
        if lane is None:
            continue
        x = lane * 3.38 if False else None  # lane→米需 lane_w_m，见调用点
        if x is None:
            continue
        for z in np.arange(3.0, 16.0, 0.5):
            zc = float(z)
            yy = y0
            if coef is not None:
                yy = float(coef[0] * x + coef[1] * zc + coef[2])
            pts.append([x, yy, zc])
            cols.append((255, 60, 200))
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


def build_frame(sess, cal: Calib, frames_dir: Path, fid: int, wt: WallTrack,
                ref_pts, ref_cols):
    img = cv2.imread(str(frames_dir / f"{fid:04d}_raw.jpg"))
    frame = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    pts, fx, fy = dg.infer_points(sess, frame, native=True)
    # 原生点图抽稀 + 有效域过滤（相机系：x 右 y 下 z 前）
    P = pts[::PT_STRIDE, ::PT_STRIDE, :]
    Hs, Ws = P.shape[:2]
    C = cv2.resize(frame, (Ws, Hs), interpolation=cv2.INTER_AREA)
    m = np.isfinite(P[..., 0]) & (P[..., 2] > 0.5) & (P[..., 2] < Z_CUT)
    xyz = P[m].astype(np.float32)
    rgb = C[m].astype(np.uint8)

    # 生产语义复算：栅格（含 coef + 质量门）→ 读数 → 墙线走廊 → 分带走廊
    grid = dg.drivable_grid_from_points(pts, fx, fy)
    coef = grid.coef
    reading = dg.reading_from_points(pts, fx, cal, fy=fy, coef=coef)
    wlc = dg.wall_line_reading(grid, cal.lane_w_m)
    wt.update(reading, fid)
    corr = cr.build_corridor(
        reading, grid, wt, fid,
        center_lane=dg.grid_center_lane(grid, cal.lane_w_m),
        width_lane=None, src="offline", lane_w_m=cal.lane_w_m)

    layers = []  # (name, xyz_f32, rgb_u8)
    if coef is not None:
        layers.append(("平面(青灰)", *_plane_dots(coef)))
    layers.append(("栅格绿/红", *_grid_dots(grid.state, coef)))
    layers.append(("走廊带(质量色)", *_bands_dots(corr.bands, coef)))
    if wlc is not None:
        layers.append(("墙线(黄左橙右)", *_wall_dots(wlc, coef)))
    layers.append(("参考网格+环", ref_pts, ref_cols))

    dl = reading.left_edge_lane; dr = reading.right_edge_lane
    hud = "<br>".join([
        f"dgeo L={dl} R={dr} rej={reading.rejects}",
        f"cor={corr.src} center={corr.center_lane} ph={corr.plane_health}",
        (f"wlc={wlc.health} w={wlc.width_lane} c={wlc.center_lane} "
         f"slope={wlc.slope} nseg={len(wlc.left_segs)}+{len(wlc.right_segs)}"
         if wlc is not None else "wlc=None"),
        "（离线无 YOLO：车框/物体掩码不在）",
    ])
    label = (f"fid {fid} · 平面{'有' if coef is not None else 'None(弃权)'}"
             f" · 栅格挖洞 {grid.dig_cells} 格"
             f" · 读数拒因 {reading.rejects or '无'}")

    return {
        "fid": fid, "label": label, "hud": hud,
        "pxyz": b64(xyz), "prgb": b64(rgb),
        "layers": [[n, b64(x), b64(c)] for n, x, c in layers],
        "np": int(xyz.shape[0]),
    }


def _grid_dots(state, coef, y0=0.0):
    """生产 BEV 三态格：可走绿 / 不可走红（unknown 不画）。栅格行号本身是
    相机系 z（GRID_Z_LO 起算自点云 Z），不加车头基准——加了就是 +5m 前移。"""
    nz, nx = state.shape
    pts, cols = [], []
    for iz in range(nz):
        z = dg.GRID_Z_LO + (iz + 0.5) * dg.GRID_CELL
        for ix in range(nx):
            st = state[iz, ix]
            if st == dg.GRID_UNKNOWN:
                continue
            x = -dg.GRID_X_MAX + (ix + 0.5) * dg.GRID_CELL
            y = y0
            if coef is not None:
                y = float(coef[0] * x + coef[1] * z + coef[2])
            pts.append([float(x), y, float(z)])
            cols.append((60, 220, 90) if st == dg.GRID_DRIVABLE
                        else (255, 60, 60))
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>speedrush 管线 3D</title><style>
html,body{margin:0;height:100%;background:#000;color:#ddd;font:13px/1.4 system-ui,sans-serif;overflow:hidden}
#c{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab}
#hud{position:absolute;left:12px;top:10px;background:rgba(0,0,0,.6);padding:8px 12px;border-radius:8px;pointer-events:none;max-width:46ch}
#hud b{color:#7fd}
#right{position:absolute;right:12px;top:10px;background:rgba(0,0,0,.6);padding:8px 12px;border-radius:8px}
#right label{display:block;cursor:pointer}
#frames{position:absolute;bottom:12px;left:50%;transform:translateX(-50%);display:flex;gap:6px;flex-wrap:wrap;justify-content:center}
button{background:#222;color:#ddd;border:1px solid #555;border-radius:6px;padding:4px 9px;cursor:pointer}
button:hover{background:#333}
button.on{background:#365;border-color:#7ac}
</style></head><body>
<canvas id="c"></canvas>
<div id="hud"><b id="lab"></b><br>拖=旋转 · 滚轮=缩放 · 右键拖=平移<br><span id="pmode">点色：原帧 RGB</span><br>
<div id="hudtxt" style="color:#9cd;font-size:12px"></div></div>
<div id="right"><b>图层</b><div id="toggles"></div></div>
<div id="frames"></div>
<script>
const FR=__FRAMES__;
function f32(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(s==null?0:i);return new Float32Array(u.buffer)}
function u8(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return new Uint8Array(u.buffer)}
function b64f(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return new Float32Array(u.buffer)}
const c=document.getElementById('c'),ctx=c.getContext('2d');
let fi=0,yaw=-0.55,pitch=0.28,camD=26,panX=0,panY=0,pmode=0;
let cx=0,cy=0,cz=0,span=26;
let show=[],cur=null;
function setFrame(i){
  fi=(i+FR.length)%FR.length; cur=FR[fi];
  // 层结构每帧一致（有 None 层会缺），以当前帧重建开关
  const tb=document.getElementById('toggles'); tb.innerHTML='';
  show=cur.layers.map(()=>true);
  cur.layers.forEach((L,k)=>{
    const lb=document.createElement('label');
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=true;
    cb.onchange=()=>{show[k]=cb.checked;render()};
    lb.appendChild(cb); lb.appendChild(document.createTextNode(' '+L[0]));
    tb.appendChild(lb);
  });
  // 中心/尺度：取点云包围盒
  const P=b64f(cur.pxyz); let n=P.length/3,mnx=1e9,mxx=-1e9,mny=1e9,mxy=-1e9,mnz=1e9,mxz=-1e9;
  for(let i=0;i<n;i++){const x=P[3*i],y=P[3*i+1],z=P[3*i+2];
    if(x<mnx)mnx=x;if(x>mxx)mxx=x;if(y<mny)mny=y;if(y>mxy)mxy=y;if(z<mnz)mnz=z;if(z>mxz)mxz=z}
  cx=(mnx+mxx)/2;cy=(mny+mxy)/2;cz=(mnz+mxz)/2;
  span=Math.max(Math.max(mxz-mnz,8)*0.75,10); camD=span*1.6; panX=panY=0;
  document.getElementById('lab').textContent=cur.label;
  document.getElementById('hudtxt').innerHTML=cur.hud;
  document.querySelectorAll('#frames button').forEach((b,k)=>b.className=k===fi?'on':'');
  render();
}
let W,H; function rz(){W=c.width=c.clientWidth;H=c.height=c.clientHeight} addEventListener('resize',()=>{rz();if(typeof cur!=='undefined'&&cur)render()}); rz();
const hue=t=>`hsl(${240-240*Math.max(0,Math.min(1,t))},95%,`+(35+35*Math.max(0,Math.min(1,t)))+`%)`;
function render(){
  const P=b64f(cur.pxyz),NP=P.length/3;
  const f=1.15*Math.min(W,H),cyw=Math.cos(yaw),syw=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
  const proj=(x,y,z,out)=>{
    x-=cx;y=-(y-cy);z-=cz;
    const x1=cyw*x+syw*z, z1=-syw*x+cyw*z;
    const y1=cp*y-sp*z1, z2=sp*y+cp*z1;
    const zc=z2+camD;
    if(zc<0.6)return false;
    const s=f/zc,px=W/2+x1*s+panX,py=H/2-y1*s+panY;
    if(px<-3||px>W+3||py<-3||py>H+3)return false;
    out[0]=px;out[1]=py;out[2]=zc;return true;
  };
  const buf=[];
  for(let i=0;i<NP;i++){const p=[0,0,0];
    if(proj(P[3*i],P[3*i+1],P[3*i+2],p))buf.push([p[2],p[0],p[1],i])}
  buf.sort((a,b)=>b[0]-a[0]);
  ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);
  const cols=cur.prgb?u8(cur.prgb):null;
  for(let k=0;k<buf.length;k++){const b=buf[k];
    ctx.fillStyle=pmode?hue(0.5):`rgb(${cols[3*b[3]]},${cols[3*b[3]+1]},${cols[3*b[3]+2]})`;
    ctx.fillRect(b[1],b[2],1.6,1.6)}
  // 图层（画在点之后，保持可见）
  cur.layers.forEach((L,k)=>{ if(!show[k])return;
    const X=b64f(L[1]),C=u8(L[2]);
    for(let i=0;i<X.length/3;i++){const p=[0,0,0];
      if(proj(X[3*i],X[3*i+1],X[3*i+2],p)){
        ctx.fillStyle=`rgb(${C[3*i]},${C[3*i+1]},${C[3*i+2]})`;
        ctx.fillRect(p[0],p[1],2.2,2.2)}}
  });
}
let drag=0,lx=0,ly=0;
c.addEventListener('mousedown',e=>{drag=e.button===2?2:1;lx=e.clientX;ly=e.clientY;c.style.cursor='grabbing'});
addEventListener('mouseup',()=>{drag=0;c.style.cursor='grab'});
addEventListener('mousemove',e=>{if(!drag)return;const dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;
  if(drag===1){yaw+=dx*0.008;pitch=Math.max(-1.5,Math.min(1.5,pitch+dy*0.008))}else{panX+=dx;panY+=dy} render()});
c.addEventListener('contextmenu',e=>e.preventDefault());
c.addEventListener('wheel',e=>{e.preventDefault();camD*=Math.exp(e.deltaY*0.0012);render()},{passive:false});
addEventListener('keydown',e=>{if(e.key==='ArrowRight')setFrame(fi+1);if(e.key==='ArrowLeft')setFrame(fi-1)});
document.getElementById('pmode').onclick=()=>{pmode=1-pmode;
  document.getElementById('pmode').textContent=pmode?'点色：高度(蓝低红高)':'点色：原帧 RGB';render()};
const fb=document.getElementById('frames');
FR.forEach((fr,k)=>{const b=document.createElement('button');b.textContent=fr.fid;
  b.onclick=()=>setFrame(k);fb.appendChild(b)});
const rs=document.createElement('button');rs.textContent='复位';
rs.onclick=()=>{yaw=-0.55;pitch=0.28;camD=span*1.6;panX=panY=0;render()};fb.appendChild(rs);
setFrame(0);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--fids", default="8,20,35,50,60,75,95,115")
    ap.add_argument("--out", default=None)
    ap.add_argument("--weights", default=None)
    a = ap.parse_args()
    d = Path(a.dir)
    wf = Path(a.weights) if a.weights else \
        Path(__file__).resolve().parents[3] / \
        "maaracing_master/plugins/speedrush/resources/onnx/depth" / \
        "moge2_vits_static_336x598_t1032_q4f16.onnx"
    sess = dg.load_session(wf)
    cal = load_calib()

    y0 = 0.0
    ref_pts, ref_cols = _grid_axes(y0)
    rpts, rcols = _ring_axes(y0)
    ref_pts = np.concatenate([ref_pts, rpts]).astype(np.float32)
    ref_cols = np.concatenate([ref_cols, rcols]).astype(np.uint8)

    fids = [int(x) for x in a.fids.split(",") if x]
    frames = []
    for fid in fids:
        jpg = d / "raw" / f"{fid:04d}_raw.jpg"
        if not jpg.exists():
            print(f"skip {fid}: no raw frame")
            continue
        print(f"render {fid} ...")
        wt = WallTrack(cal)
        frames.append(build_frame(sess, cal, d / "raw", fid, wt, ref_pts, ref_cols))

    html = HTML.replace("__FRAMES__", json.dumps(frames))
    out = Path(a.out) if a.out else d / "pipeline3d.html"
    out.write_text(html, encoding="utf-8")
    print(f"written {out} ({out.stat().st_size/1e6:.1f} MB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
