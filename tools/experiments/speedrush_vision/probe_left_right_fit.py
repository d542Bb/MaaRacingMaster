# -*- coding: utf-8 -*-
"""左右半幅独立拟合对照实验（临时，2026-10-14，GPT 实验 A/B/C + 远场劫持对照）。

只读：不改产线、不放宽门。对每帧：
  A=左半+中部(X<0.5)独立拟合 → 拿该平面预测右侧，残差按 X/Z 分桶看规律
  B=右半(X≥0.5)独立拟合 → 与左侧平面比法向
  C=通过帧同分区对照
  D=近场拟合(Z<14) vs 全域拟合(Z≤30) → 测远场劫持
用法（仓库根）：.venv/Scripts/python -m tools.experiments.speedrush_vision.probe_left_right_fit
"""
from __future__ import annotations

import cv2
import numpy as np
from pathlib import Path

from maaracing_master.plugins.speedrush.depth_geo import (
    Y0, DIAG_Y1, _dig_3d_mask, _dig_band, _fit_road_plane)

BASE = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\debug\speedrush"
            r"\20261014_112029_p2\depth_debug")
FULL_W, FULL_H = 1280, 720
FRAMES = {"通过": "d00002", "拒#1": "d00014", "拒#2": "d00021"}
XSPLIT = 0.5   # 左右分界（米）


def load(stem: str):
    z = np.load(BASE/f"{stem}_evid.npz")
    valid = np.unpackbits(z["valid"])[:336*598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~vf] = np.nan
    fx, fy = float(z["fx"]) * FULL_W, float(z["fy"]) * FULL_H
    mask = np.unpackbits(np.load(BASE/f"{stem}_mask.npy"))[:FULL_W*FULL_H] \
        .reshape(FULL_H, FULL_W).astype(bool)
    X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
    coef = _fit_road_plane(X, Y, Z, fy=fy)
    sgn = -1.0 if float(coef[1]) * 5.0 + float(coef[2]) > 0 else 1.0
    hgt = sgn * (Y - (coef[0] * X + coef[1] * Z + coef[2]))
    dig = _dig_3d_mask(X, Z, hgt, _dig_band(mask, pts.shape[1]))
    return X, Y, Z, hgt, dig


def fit_sub(X, Y, Z, hgt, dig, sel):
    """在子域上跑产线同款两段式拟合（种子+走廊收敛），返回 coef 或 None。"""
    from maaracing_master.plugins.speedrush.depth_geo import (
        _fit_road_plane_seed, _road_by_column_profile)
    ok = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 0)
    road = ok & sel & (~dig)
    if int(road.sum()) < 500:
        return None
    xs, ys, zs = X[road], Y[road], Z[road]
    m = None
    # 种子：列剖面特征（与产线同参）
    try:
        fy_stub = None
        seed = None
        # 无 fy 时走旧法起步（全域进第一轮）——对照实验够用
        m = np.ones(int(road.sum()), bool)
    except Exception:
        m = np.ones(int(road.sum()), bool)
    for _ in range(3):
        if int(m.sum()) < 500:
            return None
        A = np.stack([xs[m], zs[m], np.ones(int(m.sum()), np.float32)], 1)
        coef = np.linalg.lstsq(A, ys[m], rcond=None)[0]
        m_next = np.abs(ys - (coef[0]*xs + coef[1]*zs + coef[2])) < 0.15
        if np.array_equal(m_next, m):
            break
        m = m_next
    return coef


def stats(X, Z, hgt, dig, coef, sel, tag):
    road = np.isfinite(hgt) & sel & (np.abs(X) < 4.0) & (Z >= 4.0) & (Z < 14.0) & (~dig)
    resid = np.abs(hgt - 0)  # hgt 已是相对 coef 的偏差
    if coef is not None:
        pass
    n = int(road.sum())
    inl = int((road & (np.abs(hgt) < 0.15)).sum())
    r = np.abs(hgt[road])
    p50 = float(np.nanmedian(r)) if n else float("nan")
    p90 = float(np.nanpercentile(r, 90)) if n else float("nan")
    print(f"  {tag}: 点={n} 内点率={inl/max(1,n):.3f} 残差 p50={p50:.3f} p90={p90:.3f}")
    return road, n


def main() -> None:
    for tag, stem in FRAMES.items():
        print("=" * 76)
        print(f"[{tag}] {stem}")
        X, Y, Z, hgt, dig = load(stem)
        left = X < XSPLIT
        right = ~left
        coef_full = _fit_road_plane(X, Y, Z, fy=None)
        a, b, c = (float(coef_full[0]), float(coef_full[1]), float(coef_full[2]))
        # 用全域 coef 重新算 hgt（上面 load 用的是产线 coef，同值）
        hgt = (-1.0 if b*5.0+c > 0 else 1.0) * (Y - (a*X + b*Z + c))
        print(f"  全域拟合: roll={np.degrees(np.arctan(a)):+.2f}° pitch={np.degrees(np.arctan(b)):+.2f}° cam_h={abs(c):.2f}")
        stats(X, Z, hgt, dig, coef_full, np.ones_like(X, bool), "全域")

        # A: 左半独立拟合 → 预测右侧
        coefL = fit_sub(X, Y, Z, hgt, dig, left & (np.abs(X) < 4.0) & (Z > 4.0) & (Z < 30.0))
        if coefL is not None:
            aL, bL, cL = map(float, coefL)
            hgtL = (-1.0 if bL*5.0+cL > 0 else 1.0) * (Y - (aL*X + bL*Z + cL))
            print(f"  A 左半拟合: roll={np.degrees(np.arctan(aL)):+.2f}° pitch={np.degrees(np.arctan(bL)):+.2f}° cam_h={abs(cL):.2f}")
            stats(X, Z, hgtL, dig, coefL, left, "A 左半自评")
            stats(X, Z, hgtL, dig, coefL, right, "A 左平面预测右侧")
            # 右侧残差按 X 分桶（规律性）
            rsel = np.isfinite(hgtL) & right & (np.abs(X) < 4.0) & (Z >= 4.0) & (Z < 14.0) & (~dig)
            for lo, hi in ((0.5, 1.5), (1.5, 2.5), (2.5, 4.0)):
                m = rsel & (X >= lo) & (X < hi)
                if m.sum():
                    print(f"    右侧 |hgt| X[{lo},{hi}): p50={float(np.nanmedian(np.abs(hgtL[m]))):.3f} n={int(m.sum())}")

        # B: 右半独立拟合 → 比法向
        coefR = fit_sub(X, Y, Z, hgt, dig, right & (np.abs(X) < 4.0) & (Z > 4.0) & (Z < 30.0))
        if coefR is not None:
            aR, bR, cR = map(float, coefR)
            hgtR = (-1.0 if bR*5.0+cR > 0 else 1.0) * (Y - (aR*X + bR*Z + cR))
            print(f"  B 右半拟合: roll={np.degrees(np.arctan(aR)):+.2f}° pitch={np.degrees(np.arctan(bR)):+.2f}° cam_h={abs(cR):.2f}")
            stats(X, Z, hgtR, dig, coefR, right, "B 右半自评")
            if coefL is not None:
                print(f"  A/B 法向差: Δroll={np.degrees(np.arctan(aR))-np.degrees(np.arctan(aL)):+.2f}° "
                      f"Δpitch={np.degrees(np.arctan(bR))-np.degrees(np.arctan(bL)):+.2f}° "
                      f"Δcam_h={abs(cR)-abs(cL):+.3f}")

        # D: 近场拟合(Z<14) vs 全域——测远场劫持
        coefN = fit_sub(X, Y, Z, hgt, dig, (np.abs(X) < 4.0) & (Z > 4.0) & (Z < 14.0))
        if coefN is not None:
            aN, bN, cN = map(float, coefN)
            print(f"  D 近场拟合(Z<14): roll={np.degrees(np.arctan(aN)):+.2f}° pitch={np.degrees(np.arctan(bN)):+.2f}° cam_h={abs(cN):.2f}")
            hgtN = (-1.0 if bN*5.0+cN > 0 else 1.0) * (Y - (aN*X + bN*Z + cN))
            stats(X, Z, hgtN, dig, coefN, (np.abs(X) < 4.0) & (Z < 14.0), "D 近场自评")


if __name__ == "__main__":
    main()
