# -*- coding: utf-8 -*-
"""焦距求解残差地形探针（临时）：在已存帧 jpg 上复跑 MoGe 前向，对
recover_focal_shift 的目标函数做全网格扫描，回答两件事——

1. R(s)/f(s) 地形：局部极小有几个、产线模式搜索落在哪个、起步/巡航/UI 帧
   的解是「盆地跳变」还是「单盆地连续移动」（GPT §3 的二选一）。
2. 双采样对照：产线固定格点采样 vs 官方式窗内最近有效采样，f/shift 差多少
   （GPT §2 的可证伪项）。
3. 同帧反事实：钉焦距（巡航中位 fx_norm=0.46）重解 shift，重建点云喂
   reading_from_points，看左右缘读数位移多少（解释 A：shift–f 补偿）。

用完即清或转正入册。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, r"D:\maaracing_assistant")

from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush import moge_post as mp
from maaracing_master.plugins.speedrush.world_model import load_calib

ROOT = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush"
            r"\control_traces")
FRAMES = [
    ("start_192334_d1",  ROOT / "depth_debug_20261010_192334" / "d00001_frame.jpg"),
    ("cruise_192334_d40", ROOT / "depth_debug_20261010_192334" / "d00040_frame.jpg"),
    ("cruise_192334_d55", ROOT / "depth_debug_20261010_192334" / "d00055_frame.jpg"),
    ("ui_192334_d67",    ROOT / "depth_debug_20261010_192334" / "d00067_frame.jpg"),
    ("start_151742_d1",  ROOT / "depth_debug_20261010_151742" / "d00001_frame.jpg"),
    ("fxmin_151742_d41", ROOT / "depth_debug_20261010_151742" / "d00041_frame.jpg"),
]
FX_PIN_NORM = 0.460   # 三局巡航 p50（0.456/0.477/0.457）
CAL = load_calib()

sess = dg.load_session(
    Path(__import__("maaracing_master.plugins.speedrush", fromlist=["DEPTH_MODEL_FILE"])
         .DEPTH_MODEL_FILE))


def _solver_inputs(points, mask):
    """产线口径的 64×64 采样（_masked_nearest_resize）→ (uv, xy, z, z_med)。"""
    H, W = mask.shape
    uv_full = mp.normalized_view_plane_uv(W, H)
    uv, m = mp._masked_nearest_resize(uv_full, mask)
    pts, _ = mp._masked_nearest_resize(points, mask)
    uv_s = uv[m].astype(np.float64)
    xy = pts[m][..., :2].astype(np.float64)
    z = np.maximum(pts[m][..., 2].astype(np.float64), 1e-6)
    return uv_s, xy, z


def _window_nearest(points, mask):
    """官方式窗内最近有效采样：输出格 (i,j) 的输入窗取窗内离中心最近的有效像素。"""
    H, W = mask.shape
    oh = ow = mp.DOWN
    ys0 = (np.arange(oh) * H / oh).astype(int)
    ys1 = np.minimum(((np.arange(oh) + 1) * H / oh).astype(int) + 1, H)
    xs0 = (np.arange(ow) * W / ow).astype(int)
    xs1 = np.minimum(((np.arange(ow) + 1) * W / ow).astype(int) + 1, W)
    uv_full = mp.normalized_view_plane_uv(W, H)
    uv = np.zeros((oh, ow, 2), np.float64)
    xy = np.zeros((oh, ow, 2), np.float64)
    z = np.zeros((oh, ow), np.float64)
    m = np.zeros((oh, ow), bool)
    for i in range(oh):
        for j in range(ow):
            win = slice(ys0[i], ys1[i]), slice(xs0[j], xs1[j])
            mw = mask[win]
            if not mw.any():
                continue
            cy = (ys0[i] + ys1[i] - 1) / 2.0
            cx = (xs0[j] + xs1[j] - 1) / 2.0
            rr, cc = np.nonzero(mw)
            k = np.argmin((rr + ys0[i] - cy) ** 2 + (cc + xs0[j] - cx) ** 2)
            r, c = rr[k] + ys0[i], cc[k] + xs0[j]
            m[i, j] = True
            uv[i, j] = uv_full[r, c]
            xy[i, j] = points[r, c, :2]
            z[i, j] = max(points[r, c, 2], 1e-6)
    return uv[m], xy[m], z[m]


def _terrain(uv, xy, z, n=351, lo=-1.0, hi=2.5):
    """R(s)（focal 逐 s 闭式回代）与 f(s) 全网格。返回 (S, R, F) 与局部极小列表。"""
    z_med = float(np.median(z))
    S = np.linspace(lo * z_med, hi * z_med, n)
    R = np.empty(n); F = np.empty(n)
    with np.errstate(all="ignore"):
        for k, s in enumerate(S):
            p = xy / (z + s)[:, None]
            F[k] = (p * uv).sum() / np.square(p).sum()
            d = F[k] * p - uv
            R[k] = float((d * d).sum())
    mins = [k for k in range(1, n - 1) if R[k] < R[k - 1] and R[k] <= R[k + 1]]
    return S, R, F, mins, z_med


def _rebuild_full(points, mask, ms, fx_norm, s):
    """给定 (fx_norm, shift) 重建全幅点云（口径同 infer_points：原生重建→升采样→nan）。"""
    H, W = points.shape[:2]
    z = points[..., 2] + s
    u = (np.arange(W) + 0.5) / W
    v = (np.arange(H) + 0.5) / H
    X = (u[None, :] - 0.5) * z / fx_norm
    Y = (v[:, None] - 0.5) * z / (fx_norm * W / H)
    pts = np.stack([X, Y, z], -1) * ms
    valid = mask & (z > 0)
    pts = np.stack([cv2.resize(pts.astype(np.float32)[..., k], (1280, 720),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (1280, 720),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~vf] = np.nan
    return pts


for name, fp in FRAMES:
    if not fp.exists():
        print(f"\n=== {name}: 帧不在盘 {fp.name} ==="); continue
    rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
    blob = mp.preprocess(rgb, 1280, 720)
    points, mask, ms = mp.forward(sess, blob, 1032)
    H, W = points.shape[:2]
    ar = W / H

    uvA, xyA, zA = _solver_inputs(points, mask)
    sA, fA = mp._solve_shift(uvA, xyA, zA, None)
    S, R, F, mins, z_med = _terrain(uvA, xyA, zA)
    fxn = lambda f: f / 2 * (1 + ar**2) ** 0.5 / ar     # focal → fx_norm(u∈[0,1])
    prod = fxn(fA) * 1280
    print(f"\n=== {name}  native={W}x{H} n={len(zA)} cov={mask.mean():.3f} ms={ms:.2f} ===")
    print(f"z_med={z_med:.4f}  产线解: s*={sA:+.4f}({sA/z_med:+.2f}·z_med) "
          f"fx={prod:.0f}px  R(s*)={R[np.argmin(np.abs(S-sA))]:.4f}")
    real = [k for k in mins if fxn(F[k]) * 1280 > 50]
    degen = len(mins) - len(real)
    if degen:
        print(f"  （另有 {degen} 个 f→0 退化架极小，fx<50px，略）")
    for k in real:
        print(f"  盆地 s={S[k]:+.4f}({S[k]/z_med:+.2f}z) fx={fxn(F[k])*1280:.0f}px R={R[k]:.4f}"
              + ("  ←产线落此" if abs(S[k] - sA) < 0.02 * z_med else ""))

    uvB, xyB, zB = _window_nearest(points, mask)
    sB, fB = mp._solve_shift(uvB, xyB, zB, None)
    print(f"双采样: 窗内最近 n={len(zB)} s={sB:+.4f} fx={fxn(fB)*1280:.0f}px "
          f"(Δfx={fxn(fB)*1280-prod:+.0f}px, Δs={sB-sA:+.4f})")

    # 反事实：钉 fx_norm=0.46，固定 f 重解 shift，重建喂 reading
    f_pin = FX_PIN_NORM * 2 * ar / (1 + ar**2) ** 0.5

    def resid_fixed(s, xy=xyA, z=zA, uv=uvA, f=f_pin):
        p = xy / (z + s)[:, None]
        d = f * p - uv
        return float((d * d).sum())
    s_pin = mp._minimize_local(resid_fixed, 0.05 * z_med)
    pts_pin = _rebuild_full(points, mask, ms, FX_PIN_NORM, s_pin)
    rd_pin = dg.reading_from_points(pts_pin, FX_PIN_NORM * 1280, CAL)
    rd_as = dg.reading_from_points(_rebuild_full(points, mask, ms, fxn(fA), sA),
                                   fxn(fA) * 1280, CAL)
    def _fmt(rd):
        c = rd.coef
        return (f"L={rd.left_edge_lane} R={rd.right_edge_lane} sides={rd.sides} "
                f"cam_h={'?' if c is None else f'{c[2]:.2f}'} rej={rd.rejects}")
    print(f"反事实: as-is  s={sA:+.4f} fx={prod:.0f}px  {_fmt(rd_as)}")
    print(f"          pinned s={s_pin:+.4f} fx={FX_PIN_NORM*1280:.0f}px  {_fmt(rd_pin)}")
