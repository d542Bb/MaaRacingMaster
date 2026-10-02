# -*- coding: utf-8 -*-
"""同帧多源点云对比（忠实展示版）：UniDepth 离线米制 vs 生产 q4f16@336 vs fp32@518
vs fp16@392 vs MoGe-2。

为什么有这个文件（2026-09-30，维护者"眼见为实"要求）：pc3d 离线包看着平、
生产链包塌——但两者模型、分辨率、量化、标定链全不同，差异归因需要同帧同网格
同相机切源对比。每个源各自做平面摆正 + HUD 读数（相机高/半宽/残差/滚转俯仰），
网格与三轴共用（y=0 参考面），切换数据源时网格不动，谁平谁塌一眼可见。

忠实展示（2026-10-01 维护者要求）：早先版本把带外行（检测带 340~715 之外的
远车等）和 45m 外的点直接丢掉，造成"街车消失"的观感。现在平面拟合仍只在
生产检测带内做（读数口径与历史一致），但**全部数据点都进 HTML**，检测带裁剪
与 45m 截断降级为前端开关；并嵌入原图缩略图标出检测带边界线。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/compare_depth_sources.py --stem 000100
输出：<depth_review>/pc3d_compare/<stem>_compare.html
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402
from tools.experiments.speedrush_vision import moge2_post as mp  # noqa: E402

GOLD = eph.GOLD
CACHE = eph.CACHE
OUT = eph.OUT.parent / "pc3d_compare"
WEIGHTS = eph.OUT.parent / "weights"
STEP = 4
Z_KEEP = 300.0  # 数值 sanity 上限，展示全量；前端另有 ≤45m 模拟开关


def _load_session_at(weights: Path, short: int, static: bool = False):
    """DA-S 相对视差权重按目标短边折叠（三自由维全固定，机制同 dg.load_session）。
    metric_vkitti 静态导出模型无动态维，static=True 时直载。"""
    import onnxruntime as ort
    providers = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
                 if p in ort.get_available_providers()]
    so = ort.SessionOptions()
    if static:
        return ort.InferenceSession(str(weights), sess_options=so, providers=providers)
    probe = dg.preprocess(np.zeros((720, 1280, 3), np.uint8), short)
    for name, dim in zip(("batch_size", "height", "width"),
                         (probe.shape[0], probe.shape[2], probe.shape[3])):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(weights), sess_options=so, providers=providers)


def _metric_depth(sess, frame: np.ndarray) -> np.ndarray:
    """metric_vkitti 输出 = 米制深度（大=远）；上采样回全幅。"""
    d = sess.run(None, {"pixel_values": dg.preprocess(frame, 336)})[0][0]
    return cv2.resize(d.astype(np.float32), (1280, 720), interpolation=cv2.INTER_LINEAR)


def _full_metric_cloud(D: np.ndarray):
    """metric 深度 → 全幅云（Z=D 直接米制，不经 1/d）。"""
    vv, uu = np.meshgrid(np.arange(720, dtype=np.float32),
                         np.arange(1280, dtype=np.float32), indexing="ij")
    Z = np.clip(D.astype(np.float32), 0.1, None)
    X = (uu - dg.CX) * Z / dg._FX
    Y = (vv - dg.CY) * Z / dg.FY
    return X.astype(np.float32), Y.astype(np.float32), Z.astype(np.float32)


def _moge_session(tokens: int, wpath: Path | None = None):
    """MoGe-2 静态折叠会话（720×1280 固定档）。"""
    import onnxruntime as ort
    w = wpath or WEIGHTS / f"moge2_vits_static_720x1280_t{tokens}.onnx"
    so = ort.SessionOptions()
    for n, v in (("batch_size", 1), ("height", 720), ("width", 1280)):
        so.add_free_dimension_override_by_name(n, v)
    return ort.InferenceSession(str(w), sess_options=so,
                                providers=("DmlExecutionProvider", "CPUExecutionProvider"))


HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>深度源对比 __STEM__</title><style>
html,body{margin:0;height:100%;background:#000;color:#ddd;font:13px/1.4 system-ui,sans-serif;overflow:hidden}
#c{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab}
#hud{position:absolute;left:12px;top:10px;background:rgba(0,0,0,.55);padding:8px 12px;border-radius:8px;pointer-events:none;max-width:560px}
#hud b{color:#7fd}
#btns{position:absolute;right:12px;top:10px;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:50%}
button{background:#222;color:#ddd;border:1px solid #555;border-radius:6px;padding:5px 10px;cursor:pointer}
button:hover{background:#333}
#thumb{position:absolute;left:12px;bottom:12px;border:1px solid #555;border-radius:6px;display:block}
</style></head><body>
<canvas id="c"></canvas>
<img id="thumb" width="__TW__">
<div id="hud"><b>__STEM__</b> · <span id="srclbl"></span><br><span id="hud2"></span><br>
拖=旋转 · 滚轮=缩放 · 右键拖=平移 · 颜色：<span id="mode">原帧 RGB</span><br>
网格=1m 参考面（y=0）；<b>红=X(右) 绿=Y(上) 蓝=Z(前)</b><br>
平面拟合只在工作检测带（原图红线间）内做；<b>带外与远场点全部保留</b>，用右侧开关切换口径</div>
<div id="btns"><button id="b3">切数据源 ▶</button><button id="b1">切色 RGB/高度</button>
<button id="b4">范围: <b>全幅</b>/检测带</button><button id="b5">远场: <b>全部</b>/≤45m</button>
<button id="b6">原图 开/关</button><button id="b2">复位</button></div>
<script>
function f32(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return new Float32Array(u.buffer)}
function u8(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return new Uint8Array(u.buffer)}
const GP=f32(`__GP__`), GC=u8(`__GC__`);
const SRC=__SRC__;
const c=document.getElementById('c'), ctx=c.getContext('2d');
const thumb=document.getElementById('thumb');
let cur=0, XYZ,RGB,HT,BD,NP,COL,COLH, cx=0,cy=0,cz=0,span=10;
let yaw=-0.55,pitch=0.28,camD=20,panX=0,panY=0,mode=0,showAll=true,cap45=false;
const hue=t=>`hsl(${240-240*Math.max(0,Math.min(1,t))},95%,`+(35+35*Math.max(0,Math.min(1,t)))+`%)`;
function setSrc(i){
  cur=i; const s=SRC[i];
  XYZ=s.xyz; RGB=s.rgb; HT=s.ht; BD=s.bd; NP=XYZ.length/3;
  COL=[]; COLH=[];
  for(let k=0;k<NP;k++){
    COL.push(`rgb(${RGB[3*k]},${RGB[3*k+1]},${RGB[3*k+2]})`);
    COLH.push(hue((Math.max(-0.35,Math.min(0.35,HT[k]))+0.35)/0.7));
  }
  cx=0;cy=0;cz=0;
  for(let k=0;k<NP;k++){cx+=XYZ[3*k];cy+=XYZ[3*k+1];cz+=XYZ[3*k+2]}
  cx/=NP;cy/=NP;cz/=NP;
  span=0;
  for(let k=0;k<NP;k++){const d=Math.abs(XYZ[3*k+2]-cz); if(d>span)span=d}
  span=Math.max(span,10); camD=span*1.6; panX=panY=0;
  document.getElementById('srclbl').textContent=s.name;
  document.getElementById('hud2').innerHTML=s.hud;
  render();
}
function vis(k){
  const z=XYZ[3*k+2];
  if(z<0.5)return false;
  if(!showAll&&!BD[k])return false;
  if(cap45&&z>45)return false;
  return true;
}
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
    if(!vis(i))continue;
    const p=[0,0,0];
    if(proj(XYZ[3*i],XYZ[3*i+1],XYZ[3*i+2],p))buf.push([p[2],p[0],p[1],i]);
  }
  buf.sort((a,b)=>b[0]-a[0]);
  ctx.fillStyle='#000'; ctx.fillRect(0,0,W,H);
  const PAL=mode? COLH:COL;
  for(let k=0;k<buf.length;k++){const b=buf[k]; ctx.fillStyle=PAL[b[3]]; ctx.fillRect(b[1],b[2],1.6,1.6)}
  const NGP=GP.length/3, p=[0,0,0];
  for(let i=0;i<NGP;i++){
    if(proj(GP[3*i],GP[3*i+1],GP[3*i+2],p)){
      ctx.fillStyle=`rgb(${GC[3*i]},${GC[3*i+1]},${GC[3*i+2]})`;
      ctx.fillRect(p[0],p[1],1.4,1.4);
    }
  }
}
let W,H; function rz(){W=c.width=c.clientWidth;H=c.height=c.clientHeight} addEventListener('resize',rz); rz();
let drag=0,lx=0,ly=0;
c.addEventListener('mousedown',e=>{drag=e.button===2?2:1;lx=e.clientX;ly=e.clientY;c.style.cursor='grabbing';});
addEventListener('mouseup',()=>{drag=0;c.style.cursor='grab'});
c.addEventListener('mousemove',e=>{if(!drag)return;const dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;
  if(drag===1){yaw+=dx*0.008;pitch=Math.max(-1.5,Math.min(1.5,pitch+dy*0.008))}else{panX+=dx;panY+=dy} render();});
c.addEventListener('contextmenu',e=>e.preventDefault());
c.addEventListener('wheel',e=>{e.preventDefault();camD*=Math.exp(e.deltaY*0.0012);render()},{passive:false});
document.getElementById('b3').onclick=()=>{setSrc((cur+1)%SRC.length)};
document.getElementById('b1').onclick=()=>{mode=1-mode;document.getElementById('mode').textContent=mode?'高度残差(负=高出路面)':'原帧 RGB';render()};
document.getElementById('b4').onclick=()=>{showAll=!showAll;render()};
document.getElementById('b5').onclick=()=>{cap45=!cap45;render()};
document.getElementById('b6').onclick=()=>{thumb.style.display=thumb.style.display==='none'?'block':'none'};
document.getElementById('b2').onclick=()=>{yaw=-0.55;pitch=0.28;camD=span*1.6;panX=panY=0;render()};
setSrc(0);
</script></body></html>"""


def math_deg(x: float) -> float:
    import math
    return math.degrees(math.atan(float(x)))


def _worldize(name: str, X, Y, Z, rgb, excl, s, step: int,
              band_mask=None, note: str = "", fit_fn=None) -> dict:
    """单源 → 平面摆正世界系 + 平整度读数。

    X/Y/Z 与 excl 同为 (720,1280) 全幅形状（带外数据允许 NaN）；平面拟合与
    HUD 读数只在 band_mask（工作检测带）内做（读数口径与历史一致）；点云全量
    保留（带外/远场不丢），前端开关负责口径切换。``fit_fn``：摆正拟合注入点
    （签名同 eph.fit_road_plane，缺省=实验区走廊拟合；A/B 新旧摆正用，如产线
    dg._fit_road_plane 的 fy 路径）。
    """
    if band_mask is None:
        band_mask = np.ones_like(Z, dtype=bool)
    fit_sel = band_mask & np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
    fit = fit_fn if fit_fn is not None else eph.fit_road_plane
    coef = fit(
        np.where(fit_sel, X, np.nan), np.where(fit_sel, Y, np.nan),
        np.where(fit_sel, Z, np.nan), excl)
    ht = Y - (coef[0] * X + coef[1] * Z + coef[2]) if coef is not None else np.zeros_like(X)
    if coef is None:
        roll = pitch = p50 = p95 = hw = cam_h = float("nan")
    else:
        roll = math_deg(coef[0]); pitch = math_deg(coef[1])
        road = band_mask & (np.abs(X) < 4) & (Z > 4) & (Z < 14) & (np.abs(ht) < 0.2)
        p50 = float(np.percentile(np.abs(ht[road]), 50)) * 100 if road.sum() else float("nan")
        p95 = float(np.percentile(np.abs(ht[road]), 95)) * 100 if road.sum() else float("nan")
        hw = float(np.median(np.abs(X[road]))) if road.sum() else float("nan")
        cam_h = abs(float(coef[2]))
    sl = (slice(None, None, step), slice(None, None, step))
    xyz = np.stack([X[sl], Y[sl], Z[sl]], -1).reshape(-1, 3)
    rgbf = rgb[sl].reshape(-1, 3)
    h = ht[sl].reshape(-1)
    bd = band_mask[sl].reshape(-1).astype(np.uint8)
    if coef is not None:                      # 摆正到世界系（同 _emit）
        n = np.array([-coef[0], 1.0, -coef[1]], float)
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
            xyz[:, 1] -= float(np.nanmedian(xyz[road, 1]) if int(road.sum()) >= 500
                               else np.nanmedian(xyz[:, 1]))
    keep = np.isfinite(xyz).all(1) & (xyz[:, 2] > 0.5) & (xyz[:, 2] < Z_KEEP)
    xyz, rgbf, h, bdk = xyz[keep], rgbf[keep], h[keep], bd[keep]
    if np.isfinite(cam_h):
        hud = (f"s={f'{s:.1f}' if s is not None else '米制'} · 相机高≈{cam_h:.2f}m · "
               f"半宽≈{hw:.2f}m<br>残差 p50={p50:.1f}cm / p95={p95:.1f}cm · "
               f"滚转={roll:+.2f}° 俯仰={pitch:+.2f}°"
               f"<span style='color:#9aa'>（点云地面平面倾角；来源=深度图横向梯度，非相机姿态）</span><br>"
               f"点数 带内={int(bdk.sum())} 带外={int(len(bdk)-bdk.sum())}{(' · ' + note) if note else ''}")
    else:
        hud = f"平面拟合失败{(' · ' + note) if note else ''}"
    return {"name": name, "hud": hud, "bd": bdk,
            "xyz": xyz.astype(np.float32), "rgb": rgbf.astype(np.uint8),
            "ht": h.astype(np.float32)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stem", default=None, help="金标帧 stem（用金标 path 定位原帧）")
    ap.add_argument("--frame", default=None, help="任意帧 jpg 路径（金标外的演示帧用这个）")
    args = ap.parse_args()
    assert args.stem or args.frame
    if args.frame:
        fp = Path(args.frame)
        stem = f"{fp.parent.parent.name}_{fp.stem}"
        frame = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB).astype(np.uint8)
    else:
        stem = args.stem
        rows = {Path(r["path"]).stem: r for r in csv.DictReader(GOLD.open(encoding="utf-8"))}
        r = rows[stem]
        frame = cv2.cvtColor(cv2.imread(r["path"]), cv2.COLOR_BGR2RGB).astype(np.uint8)
    ego_full = dg.DepthRoadObserver._load_ego_mask()
    ego_band = ego_full[dg.Y0:dg.DIAG_Y1]
    band_mask_full = np.zeros((720, 1280), bool)
    band_mask_full[dg.Y0:dg.DIAG_Y1] = True

    srcs = []
    def _to_full(A, fill=np.nan):
        out = np.full((720, 1280), fill, np.float32)
        out[dg.Y0:dg.DIAG_Y1] = A
        return out
    # 1) UniDepth 离线米制缓存（全幅；仅金标帧有缓存，演示帧跳过）
    if (CACHE / f"{stem}.npz").exists():
        d = np.load(CACHE / f"{stem}.npz")
        Xu, Yu, Zu = [d[k].astype(np.float32) for k in ("X", "Y", "Z")]
        srcs.append(_worldize("① UniDepth 离线米制", Xu, Yu, Zu, frame,
                              ego_full, None, STEP,
                              band_mask=band_mask_full, note="全幅"))
        print("① UniDepth: done")
    # 2) 生产 DA-S q4f16@336（相对视差 → 1/d 反投影；模型输出即检测带，带外补 NaN 展示）
    sess = _load_session_at(DEPTH_MODEL_FILE, 336)
    m = dg.infer_map(sess, frame, 336)
    s = eph._calib_scale(m, ego_band, np.zeros_like(ego_band))
    X, Y, Z = dg._cloud_band(m, s)
    srcs.append(_worldize("② 生产 DA-S q4f16@336", _to_full(X), _to_full(Y), _to_full(Z),
                          frame, ego_full, s, STEP,
                          band_mask=band_mask_full, note="仅检测带有数据（模型输出即带）"))
    # 3) DA-S fp32@518（排除量化/分辨率嫌疑）
    sess = _load_session_at(WEIGHTS / "da2_small.onnx", 518)
    m = dg.infer_map(sess, frame, 518)
    s = eph._calib_scale(m, ego_band, np.zeros_like(ego_band))
    X, Y, Z = dg._cloud_band(m, s)
    srcs.append(_worldize("③ DA-S fp32@518", _to_full(X), _to_full(Y), _to_full(Z),
                          frame, ego_full, s, STEP,
                          band_mask=band_mask_full, note="仅检测带有数据（模型输出即带）"))
    # 4/5) UniDepth-VKITTI metric@336（fp32 / q4f16；输出=米制深度，全幅直建）
    for name, w in (("④ VKITTI metric@336 fp32", "metric_vkitti_vits_336.onnx"),
                    ("⑤ VKITTI metric@336 q4f16", "metric_vkitti_vits_336_q4f16.onnx")):
        sess = _load_session_at(WEIGHTS / w, 336, static=True)
        D = _metric_depth(sess, frame)
        X, Y, Z = _full_metric_cloud(D)
        srcs.append(_worldize(name, X, Y, Z, frame,
                              ego_full, None, STEP,
                              band_mask=band_mask_full, note="全幅"))
        print(f"{name}: done")
    # 6/7/8) MoGe-2 ViT-S（官方 ONNX 静态折叠版；点图直出无 1/d，官方 auto-focal 后处理）
    moge_jobs = [("⑥ MoGe-2 ViT-S t1800", 1800, None),
                 ("⑦ MoGe-2 ViT-S t1032", 1032, None),
                 ("⑧ MoGe-2 t1032 q4f16", 1032,
                  WEIGHTS / "moge2_vits_static_336x598_t1032_q4f16.onnx")]
    for name, tok, wpath in moge_jobs:
        sess = _moge_session(tok, wpath=wpath)
        points, mask, mscale = mp.forward(sess, mp.preprocess(frame, 1280, 720), tok)
        res = mp.reconstruct(points, mask, mscale)
        print(f"MoGe t{tok}{'' if wpath is None else ' q4'}: focal={res['focal']:.4f} shift={res['shift']:.4f} scale={mscale:.3f}")
        pts = res["pts"].astype(np.float32)
        if pts.shape[:2] != (720, 1280):  # 336-out 档：图外上采样回全幅
            pts = np.stack([cv2.resize(pts[..., k], (1280, 720),
                                       interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
            valid = cv2.resize(res["valid"].astype(np.float32), (1280, 720),
                               interpolation=cv2.INTER_NEAREST).astype(bool)
        else:
            valid = res["valid"]
        pts[~valid] = np.nan
        X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
        srcs.append(_worldize(name, X, Y, Z, frame,
                              ego_full, None, STEP,
                              band_mask=band_mask_full,
                              note=f"全幅 · 模型mask剔除{(1-valid.mean()):.0%}"))
    gpts, gcols = eph._grid_axes(0.0)

    # 原图缩略图（带检测带边界线）
    th = frame.copy()
    for yy, col in ((dg.Y0, (255, 60, 60)), (dg.DIAG_Y1, (255, 60, 60))):
        cv2.line(th, (0, yy), (1279, yy), col, 2)
    th = cv2.cvtColor(cv2.resize(th, (480, 270)), cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", th, [cv2.IMWRITE_JPEG_QUALITY, 82])
    thumb_b64 = base64.b64encode(buf).decode()

    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
    src_js = "[" + ",".join(
        '{name:"' + s["name"] + '",hud:`' + s["hud"] + '`,'
        'xyz:f32(`' + b64(s["xyz"]) + '`),rgb:u8(`' + b64(s["rgb"]) + '`),ht:f32(`' + b64(s["ht"]) + '`),'
        'bd:u8(`' + b64(s["bd"]) + '`)}'
        for s in srcs) + "]"
    html = (HTML.replace("__STEM__", stem).replace("__GP__", b64(gpts))
            .replace("__GC__", b64(gcols)).replace("__SRC__", src_js)
            .replace("__TW__", "480"))
    html = html.replace('<img id="thumb" width="480">',
                        f'<img id="thumb" width="480" src="data:image/jpeg;base64,{thumb_b64}">')
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{stem}_compare.html"
    out.write_text(html, encoding="utf-8")
    print(f"{out}  {len(html)/1e6:.2f} MB  {len(srcs)} 源")


if __name__ == "__main__":
    main()
