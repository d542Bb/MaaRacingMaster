# -*- coding: utf-8 -*-
"""弯道平面质量门拒绝拆解（2026-10-10，只读 stdout）。

问：弯道段（212349_p2 hud 0108~0128 ≈ fid 4351~4522）为什么一直平面质量门
拒绝？8 个拒绝帧证据包（d00073/74/75/76/77/78/80/82）+ 4 个正常对照
（d00069/70/71/79）离线复跑：生产同口径重建点云（原生 336×598 → 720×1280），
_fit_road_plane(fy 路径) + _plane_plausible 判据逐项拆开——滚转/俯仰角、
路区内点率、残差中位、残差随距离的结构（近/中/远三段），回答「弯道拒绝的
门判据是哪一项、残差结构是不是弯道曲率」。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_curve_plane.py
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

DD = (os.path.join(os.environ["APPDATA"], "MaaRacingMaster", "data", "speedrush",
                   "control_traces", "depth_debug_20261010_212349"))

REJECT = ["d00073", "d00074", "d00075", "d00076", "d00077", "d00078",
          "d00080", "d00082"]
NORMAL = ["d00069", "d00070", "d00071", "d00079"]


def rebuild(pkg: str):
    d = np.load(os.path.join(DD, f"{pkg}_evid.npz"), allow_pickle=True)
    pts_n = d["pts"].astype(np.float32)
    valid_shape = tuple(int(v) for v in d["valid_shape"]) if "valid_shape" in d else pts_n.shape[:2]
    valid_n = np.unpackbits(d["valid"])[:int(np.prod(valid_shape))].reshape(valid_shape) \
        if d["valid"].dtype == np.uint8 and d["valid"].ndim == 1 else d["valid"]
    h, w = 720, 1280
    pts = np.stack([cv2.resize(np.ascontiguousarray(pts_n[..., k]), (w, h),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid = cv2.resize(valid_n.astype(np.float32), (w, h),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid] = np.nan
    fx = float(d["fx"]) * w
    fy = float(d["fy"]) * h
    mp = os.path.join(DD, f"{pkg}_mask.npy")
    raw = np.load(mp) if os.path.exists(mp) else None
    obj = None if raw is None else (
        (np.unpackbits(raw)[:921600].reshape(720, 1280)
         if raw.dtype == np.uint8 and raw.ndim == 1 and raw.size < 921600
         else raw.reshape(720, 1280)))
    return pts, fx, fy, obj, int(d["fid"])


def gate_parts(pts, fx, fy, obj):
    obj_band = dg._dig_band(obj, pts.shape[1])
    X, Y, Z = pts[dg.Y0:dg.DIAG_Y1, :, 0], pts[dg.Y0:dg.DIAG_Y1, :, 1], pts[dg.Y0:dg.DIAG_Y1, :, 2]
    coef = dg._fit_road_plane(X, Y, Z, fy=fy)
    if coef is None:
        return None, "拟合失败(域内点不足)"
    a, b, c = float(coef[0]), float(coef[1]), float(coef[2])
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    dig = dg._dig_3d_mask(X, Z, hgt, obj_band)
    # 门判据逐项（_plane_plausible 同口径）
    roll = np.degrees(np.arctan(a))
    pitch = np.degrees(np.arctan(b))
    road = (np.isfinite(hgt) & (np.abs(X) < dg.ROAD_X_MAX)
            & (Z >= dg.ROAD_Z_LO) & (Z < dg.PLANE_QUALITY_Z_HI) & (~dig))
    n = int(road.sum())
    if n < dg.MIN_PLANE_PTS:
        return coef, f"路区点不足 n={n}"
    inl = road & (np.abs(hgt) < dg.PLANE_TOL)
    rate = float(inl.sum()) / n
    resid = float(np.nanmedian(np.abs(hgt[road])))
    # 残差随距离的结构：近/中/远三段中位 |hgt| 与内点率
    bands = []
    for lo, hi in ((4, 8), (8, 11), (11, 14)):
        m = road & (Z >= lo) & (Z < hi)
        if int(m.sum()) < 50:
            bands.append((lo, hi, np.nan, np.nan))
            continue
        bands.append((lo, hi,
                      float(np.nanmedian(np.abs(hgt[m]))),
                      float((np.abs(hgt[m]) < dg.PLANE_TOL).sum() / m.sum())))
    ok = dg._plane_plausible(coef, X, Y, Z, dig)
    why = []
    if abs(roll) > dg.PLANE_ROLL_MAX_DEG:
        why.append("滚转超界")
    if abs(pitch) > dg.PLANE_PITCH_MAX_DEG:
        why.append("俯仰超界")
    if rate < dg.PLANE_INLIER_FLOOR:
        why.append(f"内点率 {rate:.2f} < 地板 0.5")
    elif rate < dg.PLANE_INLIER_MIN and resid > dg.PLANE_RESID_P50_MAX:
        why.append(f"内点率 {rate:.2f}<0.7 且残差中位 {resid:.3f}m>0.06")
    return coef, dict(ok=ok, roll=roll, pitch=pitch, rate=rate, resid=resid,
                      n=n, why="+".join(why) or "过门", bands=bands)


def main():
    print(f"{'pkg':8} {'fid':>5} {'verdict':10} {'roll°':>6} {'pitch°':>6} "
          f"{'内点率':>6} {'残差中位':>8}   近4-8/中8-11/远11-14 残差(m)|内点率")
    for pkgs, tag in ((REJECT, "拒"), (NORMAL, "正常")):
        for pkg in pkgs:
            pts, fx, fy, obj, fid = rebuild(pkg)
            coef, info = gate_parts(pts, fx, fy, obj)
            if not isinstance(info, dict):
                print(f"{pkg:8} {fid:>5} {tag}: {info}")
                continue
            bd = "  ".join(f"{lo}-{hi}:{r:.3f}|{q:.2f}"
                           for lo, hi, r, q in info["bands"])
            print(f"{pkg:8} {fid:>5} {tag} {'过' if info['ok'] else '拒':>2} "
                  f"{info['roll']:+6.2f} {info['pitch']:+6.2f} "
                  f"{info['rate']:6.2f} {info['resid']:8.3f}   {bd}  {info['why']}")


if __name__ == "__main__":
    main()
