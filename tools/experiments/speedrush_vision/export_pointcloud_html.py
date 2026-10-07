# -*- coding: utf-8 -*-
"""把一帧深度点云导成**单文件交互式 HTML**（零依赖：canvas-2D 软渲染，无 WebGPU/无 CDN）。

两种数据源，同一个查看器：
- **离线**（默认）：`--stem 000100`，读 UniDepth 米制点云缓存 `D:/ud_test/cache`。
- **运行时**：`--runtime-dir <depth_debug_dir>`，读实机落的 `d*_band.npy`（生产 DA-S
  q4f16 视差带）+ `_ego/_mask.npy` + `<stem>.jpg`（原帧）。点云按**生产 depth_geo 口径**
  复算（_cloud_band + 逐帧尺度自标定），验证「生产眼睛看到的」而不是离线高精度场。

为什么做这个：判断"护栏到底在不在点云里"不该靠我画的小图或统计量的间接推断——
维护者要能自己拖着看。颜色默认取**原帧 RGB**（一眼认出护栏/黄线），可切到**高度残差**
（负=高出路面；护栏若被建模应是一道明显的负值脊）。

用法：
    python tools/experiments/speedrush_vision/export_pointcloud_html.py --stem 000100 --stem 000260
    python tools/experiments/speedrush_vision/export_pointcloud_html.py --runtime-dir "<...>/depth_debug_20260930_110714"
输出：<data>/speedrush/depth_review/pc3d/<stem>.html（浏览器直接打开）
"""

from __future__ import annotations

import argparse
import base64
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

GOLD = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/"
            r"speedrush/depth_review/gold_labels.csv")
CACHE = Path(r"D:/ud_test/cache")
OUT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/"
           r"speedrush/depth_review/pc3d")
DEMO_OUT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/"
                r"speedrush/depth_review/pc3d_demo")
Z_MAX = 60.0          # 远场截断（天空/远景不进画）
STEP = 4              # 抽稀步长（720×1280 → 180×320 ≈ 5.8 万点）


def fit_road_plane(X, Y, Z, excl=None):
    """**世界系**：用「路面点」拟合平面，高度 = 相对该平面（不是逐行、也不是宽域全局）。

    2026-09-30 修正（维护者点云指正）：把墙/背景/自车也纳进拟合域时，平面被拽歪，
    路面残差会"随距离增长"（我据此误判过"路面非平面"）。只用路面点后，实测路面
    残差中位 **3–9 cm 且不随距离增长** —— 路面确实是平的（与 RULES 一致）。
    路面点 = |X|<4 且 Z∈[4,30]，**排除自车区**（|X|<1.3 且 Z<10，车是隆起物）；
    ``excl``（运行时：ego∪YOLO 掩码带）再从拟合域挖掉车/障碍。
    """
    ok = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 0)
    road = ok & (np.abs(X) < 4.0) & (Z > 4) & (Z < 30) & ~((np.abs(X) < 1.3) & (Z < 10))
    if excl is not None:
        road = road & ~excl
    sel = road
    if sel.sum() < 500:
        return None
    coef = None
    for _ in range(3):
        A = np.stack([X[sel], Z[sel], np.ones(int(sel.sum()))], 1)
        coef, *_ = np.linalg.lstsq(A, Y[sel], rcond=None)
        res = Y - (coef[0] * X + coef[1] * Z + coef[2])
        sel = road & (np.abs(res) < 0.15)
        if sel.sum() < 500:
            return None
    return coef


HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>pointcloud __STEM__</title><style>
html,body{margin:0;height:100%;background:#000;color:#ddd;font:13px/1.4 system-ui,sans-serif;overflow:hidden}
#c{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab}
#hud{position:absolute;left:12px;top:10px;background:rgba(0,0,0,.55);padding:8px 12px;border-radius:8px;pointer-events:none}
#hud b{color:#7fd}
#btns{position:absolute;right:12px;top:10px;display:flex;gap:6px}
button{background:#222;color:#ddd;border:1px solid #555;border-radius:6px;padding:5px 10px;cursor:pointer}
button:hover{background:#333}</style></head><body>
<canvas id="c"></canvas>
<div id="hud"><b>__STEM__</b> · __NP__ 点<br>拖=旋转 · 滚轮=缩放 · 右键拖=平移<br>颜色：<span id="mode">原帧 RGB</span><br>
__HUD__<br>
网格=1m 地面参考面；<b>红=X(右) 绿=Y(上) 蓝=Z(前)</b>；高度＝相对路面，蓝=高出</div>
<div id="btns"><button id="b1">切色: RGB/高度</button><button id="b2">复位</button></div>
<script>
const XYZ=f32(`__XYZ__`), RGB=u8(`__RGB__`), HT=f32(`__HT__`);
const GP=f32(`__GP__`), GC=u8(`__GC__`);   // 地面网格+三轴（参考物，画在最上层）
function f32(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return new Float32Array(u.buffer)}
function u8(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return new Uint8Array(u.buffer)}
const NP=XYZ.length/3, c=document.getElementById('c'), ctx=c.getContext('2d');
// 中心与尺度
let cx=0,cy=0,cz=0; for(let i=0;i<NP;i++){cx+=XYZ[3*i];cy+=XYZ[3*i+1];cz+=XYZ[3*i+2]} cx/=NP;cy/=NP;cz/=NP;
let span=0; for(let i=0;i<NP;i++){const d=Math.abs(XYZ[3*i+2]-cz); if(d>span)span=d} span=Math.max(span,10);
let yaw=-0.55,pitch=0.28,camD=span*1.6,panX=0,panY=0,mode=0;
const COL=[]; const hue=t=>`hsl(${240-240*Math.max(0,Math.min(1,t))},95%,`+(35+35*Math.max(0,Math.min(1,t)))+`%)`;
for(let i=0;i<NP;i++){COL.push(`rgb(${RGB[3*i]},${RGB[3*i+1]},${RGB[3*i+2]})`)}
const COLH=[]; for(let i=0;i<NP;i++){COLH.push(hue((Math.max(-0.35,Math.min(0.35,HT[i]))+0.35)/0.7))}
let W,H; function rz(){W=c.width=c.clientWidth;H=c.height=c.clientHeight} addEventListener('resize',rz); rz();
function render(){
  const f=1.15*Math.min(W,H), cyw=Math.cos(yaw),syw=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
  const proj=(x,y,z,out)=>{
    x-=cx;y=-(y-cy);z-=cz;
    const x1=cyw*x+syw*z, z1=-syw*x+cyw*z;
    const y1=cp*y-sp*z1, z2=sp*y+cp*z1;
    const zc=z2+camD;
    if(zc<0.6)return false;
    const s=f/zc, px=W/2+x1*s+panX, py=H/2-y1*s+panY;
    if(px<-2||px>W+2||py<-2||py>H+2)return false;
    out[0]=px;out[1]=py;out[2]=zc;return true;
  };
  const buf=[];
  for(let i=0;i<NP;i++){
    const p=[0,0,0];
    if(proj(XYZ[3*i],XYZ[3*i+1],XYZ[3*i+2],p))buf.push([p[2],p[0],p[1],i]);
  }
  buf.sort((a,b)=>b[0]-a[0]);
  ctx.fillStyle='#000'; ctx.fillRect(0,0,W,H);
  const PAL=mode? COLH:COL;
  for(let k=0;k<buf.length;k++){const b=buf[k]; ctx.fillStyle=PAL[b[3]]; ctx.fillRect(b[1],b[2],1.6,1.6)}
  // 参考层：1m 网格 + 三轴，画在主点之后保持可见
  const NGP=GP.length/3, p=[0,0,0];
  for(let i=0;i<NGP;i++){
    if(proj(GP[3*i],GP[3*i+1],GP[3*i+2],p)){
      ctx.fillStyle=`rgb(${GC[3*i]},${GC[3*i+1]},${GC[3*i+2]})`;
      ctx.fillRect(p[0],p[1],1.4,1.4);
    }
  }
}
let drag=0,lx=0,ly=0;
c.addEventListener('mousedown',e=>{drag=e.button===2?2:1;lx=e.clientX;ly=e.clientY;c.style.cursor='grabbing';});
addEventListener('mouseup',()=>{drag=0;c.style.cursor='grab'});
addEventListener('mousemove',e=>{if(!drag)return;const dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;
  if(drag===1){yaw+=dx*0.008;pitch=Math.max(-1.5,Math.min(1.5,pitch+dy*0.008))}else{panX+=dx;panY+=dy} render();});
c.addEventListener('contextmenu',e=>e.preventDefault());
c.addEventListener('wheel',e=>{e.preventDefault();camD*=Math.exp(e.deltaY*0.0012);render()},{passive:false});
document.getElementById('b1').onclick=()=>{mode=1-mode;document.getElementById('mode').textContent=mode?'高度残差(负=高出路面)':'原帧 RGB';render()};
document.getElementById('b2').onclick=()=>{yaw=-0.55;pitch=0.28;camD=span*1.6;panX=panY=0;render()};
render();
</script></body></html>"""


def _cloud_from_map(m_full: np.ndarray, frame_rgb: np.ndarray, ego_mask):
    """视差图 + 原帧 + ego 掩码 → 检测带点云 (X, Y, Z, rgb, excl, s)。"""
    ego = (ego_mask if ego_mask is not None
           else np.zeros((720, 1280), bool))[dg.Y0:dg.DIAG_Y1]
    obj = np.zeros_like(ego)
    s = _calib_scale(m_full, ego, obj)
    X, Y, Z = dg._cloud_band(m_full, s)
    return X, Y, Z, frame_rgb[dg.Y0:dg.DIAG_Y1], ego, s


def _load_demo(sess, frames_dir: Path, names: list[str], ego_mask):
    """demo 的**raw 帧**（1280×720 jpg）→ 逐帧跑生产深度模型 → 点云。

    与实机的差别只有一处：demo 不带 YOLO 物体掩码（离线拿不到），故 excl 只用
    ego 静态掩码；路面拟合域另有 |X|<1.3∧Z<10 的车区排除兜底。"""
    for name in names:
        bgr = cv2.imread(str(frames_dir / name))
        frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        m = dg.infer_map(sess, frame, dg.DEFAULT_SHORT)
        yield Path(name).stem, _cloud_from_map(m, frame, ego_mask)


def _calib_scale(m_full: np.ndarray, ego_band: np.ndarray, obj_band: np.ndarray) -> float:
    """复刻 depth_geo.reading_from_map 的逐帧尺度自标定（米·视差），使运行时
    点云与生产同一米制系（否则只有种子尺度、高度读数偏 CV≈14%）。"""
    dig_band = ego_band | obj_band
    X, Y, Z = dg._cloud_band(m_full, dg.S0)
    coef, ground, above = dg._plane_fit_band(X, Y, Z, dig_band)
    if coef is None:
        return dg.S0
    if ego_band.any():
        grown = dg._grow_ego_above(above, ego_band, Z)
        if grown is not None:
            dig_band = dig_band | grown
            coef, ground, above = dg._plane_fit_band(X, Y, Z, dig_band)
            if coef is None:
                return dg.S0
    rows = dg._row_scan(X, Z, ground, above, dig_band, obj_band, coef, step=2)
    hw = [(r.xr - r.xl) / 2.0 for r in rows
          if not (r.occ_l or r.occ_r) and r.zmed < dg.Z_HI]
    if len(hw) < 3:
        return dg.S0
    hwm = float(np.percentile(hw, dg.HW_PCT * 100))
    if not (dg.HW_LO <= hwm <= dg.HW_HI):
        return dg.S0
    return float(np.clip(dg.S0 * dg.W_REF / hwm,
                         dg.S0 * dg.S_RATIO[0], dg.S0 * dg.S_RATIO[1]))


def _grid_axes(y0: float, x_span: float = 10.0, z_span: float = 30.0, step: float = 1.0):
    """世界系参考物：y=y0 处 1m 地面网格（灰）+ 过原点三轴（红X/绿Y/蓝Z）。
    采样点画法（canvas 软渲染只有点），密度按 0.25m。"""
    pts, cols = [], []
    g = (110, 110, 110)

    def line(a, b, col):
        n = int(np.linalg.norm(np.array(b) - np.array(a)) / 0.25) + 1
        for t in np.linspace(0, 1, n):
            pts.append([a[k] + (b[k] - a[k]) * t for k in range(3)])
            cols.append(col)

    xs = np.arange(-x_span, x_span + 0.1, step)
    zs = np.arange(0.0, z_span + 0.1, step)
    for x in xs:                                   # 纵向网格线
        col = (150, 150, 150) if abs(x) > 1e-6 else (200, 200, 200)
        line((float(x), y0, 0.0), (float(x), y0, z_span), col)
    for z in zs:                                   # 横向网格线
        line((-x_span, y0, float(z)), (x_span, y0, float(z)), g)
    line((0, y0, 0), (6, y0, 0), (255, 60, 60))    # X 红=右
    line((0, y0, 0), (0, y0 + 5, 0), (60, 255, 60))   # Y 绿=上
    line((0, y0, 0), (0, y0, 12), (60, 120, 255))  # Z 蓝=前
    return np.asarray(pts, np.float32), np.asarray(cols, np.uint8)


def _emit(stem: str, label: str, X, Y, Z, rgb, excl, out_dir: Path,
          step: int, drop_sky: bool = False, s: float | None = None) -> None:
    cf = fit_road_plane(X, Y, Z, excl)
    ht = (Y - (cf[0] * X + cf[1] * Z + cf[2])) if cf is not None else np.zeros_like(X)
    sl = (slice(None, None, step), slice(None, None, step))
    xyz = np.stack([X[sl], Y[sl], Z[sl]], -1).reshape(-1, 3)
    rgbf = rgb[sl].reshape(-1, 3)
    h = ht[sl].reshape(-1)
    # 摆正到**世界系**：把地面法线转到竖直，路面即为水平面（维护者要求的"游戏世界"视角）
    if cf is not None:
        n = np.array([-cf[0], 1.0, -cf[1]], float)
        n /= np.linalg.norm(n)
        t = np.array([0.0, 1.0, 0.0])
        v = np.cross(n, t)
        sn = float(np.linalg.norm(v))
        if sn > 1e-9:
            vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
            R = np.eye(3) + vx + vx @ vx * ((1.0 - float(n @ t)) / (sn * sn))
            xyz = xyz @ R.T
            # 落 0 基准必须是路面点（|Δh|<0.2 的走廊内点）中位：全点中位被墙/天空
            # 拽高，路面会悬离网格 7~25cm（000560 +0.07 / 000660 +0.27 实测）。
            road = ((np.abs(xyz[:, 0]) < 4) & (xyz[:, 2] > 4) & (xyz[:, 2] < 30)
                    & (np.abs(h) < 0.2) & np.isfinite(xyz).all(1))
            xyz[:, 1] -= float(np.median(xyz[road, 1]) if road.sum() >= 500
                               else xyz[:, 1])           # 路面落到 0
    keep = np.isfinite(xyz).all(1) & (xyz[:, 2] > 0.5) & (xyz[:, 2] < Z_MAX)
    if drop_sky:      # 离线全帧：检测带起点以上（天空/背景）不进画
        rown = np.repeat(np.arange(X.shape[0])[sl[0]], X[sl].shape[1])
        keep &= rown >= 340
    xyz, rgbf, h = xyz[keep], rgbf[keep], h[keep]
    # HUD 读数（诊断口径，单位米）：相机高 / 路半宽 / 路面残差
    if cf is not None:
        road = (np.abs(xyz[:, 0]) < 4) & (xyz[:, 2] > 4) & (xyz[:, 2] < 14) & (np.abs(h) < 0.2)
        hw = float(np.median(np.abs(xyz[road][:, 0]))) if road.sum() > 500 else float("nan")
        res = np.abs(h[road])
        p50, p95 = (float(np.percentile(res, 50)), float(np.percentile(res, 95))) \
            if res.size else (float("nan"), float("nan"))
        s_txt = f"{s:.1f}" if s is not None else "UniDepth 米制"
        hud = (f"尺度 s={s_txt} · 相机高≈{abs(cf[2]):.2f}m · 近带半宽≈{hw:.2f}m<br>"
               f"路面残差 |Δh| p50={p50*100:.1f}cm / p95={p95*100:.1f}cm（路段 4~14m）")
    else:
        hud = "路面拟合失败：无尺度/高度读数"
    gpts, gcols = _grid_axes(0.0)          # 网格在摆正后的世界系：路面即 y=0
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
    html = (HTML.replace("__STEM__", label).replace("__NP__", str(len(xyz)))
            .replace("__HUD__", hud)
            .replace("__XYZ__", b64(xyz.astype(np.float32)))
            .replace("__RGB__", b64(rgbf.astype(np.uint8)))
            .replace("__HT__", b64(h.astype(np.float32)))
            .replace("__GP__", b64(gpts))
            .replace("__GC__", b64(gcols)))
    p = out_dir / f"{stem}.html"
    p.write_text(html, encoding="utf-8")
    print(f"{p}  {len(xyz)} 点  {len(html)/1e6:.2f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stem", action="append", default=[])
    ap.add_argument("--demo-dir", type=Path, default=None,
                    help="demo 录像目录（含 frames/）：跑生产模型重建点云")
    ap.add_argument("--frames", type=str, default=None,
                    help="demo 帧名，逗号分隔（默认等间隔取 --n 帧）")
    ap.add_argument("--n", type=int, default=6, help="等间隔取样帧数（默认 6）")
    ap.add_argument("--step", type=int, default=STEP)
    args = ap.parse_args()

    if args.demo_dir is not None:
        frames_dir = args.demo_dir / "frames"
        allf = sorted(p.name for p in frames_dir.glob("*.jpg"))
        if args.frames:
            want = [f.strip() for f in args.frames.split(",")]
            names = [n for n in allf if Path(n).stem in
                     {Path(w).stem for w in want}]
        else:
            idx = np.linspace(0, len(allf) - 1, min(args.n, len(allf))).astype(int)
            names = [allf[i] for i in idx]
        sess = dg.load_session(DEPTH_MODEL_FILE)
        ego = dg.DepthRoadObserver._load_ego_mask()
        DEMO_OUT.mkdir(parents=True, exist_ok=True)
        tag = args.demo_dir.name
        for stem, (X, Y, Z, rgb, excl, s) in _load_demo(sess, frames_dir, names, ego):
            _emit(f"{tag}_{stem}", f"{tag}/{stem}",
                  X, Y, Z, rgb, excl, DEMO_OUT, args.step, s=s)
        return

    OUT.mkdir(parents=True, exist_ok=True)
    rows = {Path(r["path"]).stem: r for r in csv.DictReader(GOLD.open(encoding="utf-8"))}
    for stem in args.stem:
        f = CACHE / f"{stem}.npz"
        if not f.exists() or stem not in rows:
            print("skip", stem)
            continue
        d = np.load(f)
        X, Y, Z = [d[k].astype(np.float32) for k in ("X", "Y", "Z")]
        img = cv2.cvtColor(cv2.imread(rows[stem]["path"]), cv2.COLOR_BGR2RGB).astype(np.uint8)
        _emit(stem, stem, X, Y, Z, img, None, OUT, args.step, drop_sky=True)


if __name__ == "__main__":
    main()
