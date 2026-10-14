# -*- coding: utf-8 -*-
"""平面质量门判别实验（2026-10-10，只读 stdout）——GPT 三优先级。

背景：RULES「路面全程水平等宽平面」[权威] → 弯道残差只能是观测误差。
三竞争解释砍剩两个：重建翘曲 vs 点集污染。本探针对 8 弯道帧 + 4 对照帧：

  ① 种子路径：_road_by_column_profile 产种子是否成功、种子点数、seed=None
    时全域起步的拟合结果（法向/残差前后变化）。
  ② 近带 vs 全域：近带（Z∈[4,8]）独立拟合的内点率/残差/点数，近带平面与
    全域平面的偏差（法向夹角）。
  ③ 残差空间结构：质量域 (X,Z) 网格中位 |hgt|——离群在哪侧/哪段（单侧
    集中=污染嫌疑；随距离/横向平滑增长=翘曲嫌疑）；中心带（|X|<2）重拟
    合对照——去掉路肩/墙区残差掉不掉。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_plane_warp.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision.probe_curve_plane import rebuild  # noqa: E402

REJECT = ["d00073", "d00074", "d00075", "d00076", "d00077", "d00078",
          "d00080", "d00082"]
NORMAL = ["d00069", "d00070", "d00071", "d00079"]

X_BINS = (-4, -2.5, -1, 0, 1, 2.5, 4)
Z_BINS = (4, 6.5, 9, 11.5, 14)


def fit_on(xs, ys, zs, iters=3, tol=dg.PLANE_TOL):
    m = np.ones(len(xs), bool)
    coef = None
    for _ in range(iters):
        if int(m.sum()) < 3:
            return None
        A = np.stack([xs[m], zs[m], np.ones(int(m.sum()), np.float32)], 1)
        coef = np.linalg.lstsq(A, ys[m], rcond=None)[0]
        m_next = np.abs(ys - (coef[0] * xs + coef[1] * zs + coef[2])) < tol
        if np.array_equal(m_next, m):
            break
        m = m_next
    return coef


def stats(coef, X, Y, Z, domain):
    lo_z, hi_z, xmax = domain
    hgt = Y - (coef[0] * X + coef[1] * Z + coef[2])
    road = (np.isfinite(hgt) & (np.abs(X) < xmax) & (Z >= lo_z) & (Z < hi_z))
    n = int(road.sum())
    if n < 50:
        return None
    inl = road & (np.abs(hgt) < dg.PLANE_TOL)
    return dict(n=n, rate=float(inl.sum()) / n,
                resid=float(np.nanmedian(np.abs(hgt[road]))))


def angle_between(c1, c2):
    n1 = np.array([c1[0], -1, c1[1]])
    n2 = np.array([c2[0], -1, c2[1]])
    cosv = abs(np.dot(n1, n2)) / (np.linalg.norm(n1) * np.linalg.norm(n2))
    return float(np.degrees(np.arccos(np.clip(cosv, -1, 1))))


def main():
    for pkgs, tag in ((REJECT, "拒"), (NORMAL, "正常")):
        for pkg in pkgs:
            pts, fx, fy, obj, fid = rebuild(pkg)
            obj_band = dg._dig_band(obj, pts.shape[1]) if obj is not None else None
            X, Y, Z = pts[dg.Y0:dg.DIAG_Y1, :, 0], pts[dg.Y0:dg.DIAG_Y1, :, 1], pts[dg.Y0:dg.DIAG_Y1, :, 2]
            ok = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 0)
            bad = (~np.isfinite(Z) | (Z <= dg.WALK_Z_MIN)
                   | ((np.abs(X) < dg.EGO_ABS_X) & (Z < dg.EGO_ABS_Z)))
            # ① 种子路径
            road_map = dg._road_by_column_profile(Z, fy, bad)
            n_road = int(road_map.sum())
            seed = dg._fit_road_plane_seed(road_map, X, Y, Z)
            seed_ok = seed is not None
            coef_full = dg._fit_road_plane(X, Y, Z, fy=fy)
            hgt_full = Y - (coef_full[0] * X + coef_full[1] * Z + coef_full[2])
            dig = dg._dig_3d_mask(X, Z, hgt_full, obj_band)
            full = stats(coef_full, X, Y, Z, (dg.ROAD_Z_LO, dg.PLANE_QUALITY_Z_HI, dg.ROAD_X_MAX))
            # ② 近带独立拟合（同选择域、Z∈[4,8]）
            sel = (np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
                   & (np.abs(X) < dg.ROAD_X_MAX) & (Z >= 4) & (Z < 8)
                   & ~((np.abs(X) < dg.EGO_ABS_X) & (Z < dg.EGO_ABS_Z)))
            n_near = int(sel.sum())
            coef_near = fit_on(X[sel], Y[sel], Z[sel]) if n_near >= 50 else None
            near = stats(coef_near, X, Y, Z, (4, 8, dg.ROAD_X_MAX)) if coef_near is not None else None
            ang = angle_between(coef_full, coef_near) if coef_near is not None else np.nan
            # ③ 残差空间结构 + 中心带重拟合
            roadq = (np.isfinite(hgt_full) & (np.abs(X) < dg.ROAD_X_MAX)
                     & (Z >= dg.ROAD_Z_LO) & (Z < dg.PLANE_QUALITY_Z_HI) & (~dig))
            cells = []
            for i in range(len(Z_BINS) - 1):
                rowc = []
                for j in range(len(X_BINS) - 1):
                    m = (roadq & (Z >= Z_BINS[i]) & (Z < Z_BINS[i + 1])
                         & (X >= X_BINS[j]) & (X < X_BINS[j + 1]))
                    rowc.append(f"{np.nanmedian(np.abs(hgt_full[m]))*100:4.0f}" if int(m.sum()) >= 30 else "  .")
                cells.append(" ".join(rowc))
            mid = (np.abs(X) < 2) & roadq
            if int(mid.sum()) >= 200:
                coef_mid = fit_on(X[mid], Y[mid], Z[mid])
                midst = stats(coef_mid, X, Y, Z, (dg.ROAD_Z_LO, dg.PLANE_QUALITY_Z_HI, 2.0))
                mid_s = f"中心带重拟合: rate={midst['rate']:.2f} resid={midst['resid']*100:.1f}cm" if midst else ""
            else:
                mid_s = "中心带点不足"
            print(f"\n=== {pkg} fid={fid} [{tag}]")
            print(f"  ① seed={'成功' if seed_ok else '失败(None→全域起步)'} "
                  f"road_map点={n_road} 全域: rate={full['rate']:.2f} resid={full['resid']*100:.1f}cm")
            if near:
                print(f"  ② 近带(Z4-8): 点={near['n']} rate={near['rate']:.2f} "
                      f"resid={near['resid']*100:.1f}cm 与全域法向夹角={ang:.2f}°")
            else:
                print(f"  ② 近带点不足 n={n_near}")
            print(f"  ③ 残差|cm| (X: {X_BINS} × Z: {Z_BINS}):")
            for i, rowc in enumerate(cells):
                print(f"      Z{Z_BINS[i]:>4}-{Z_BINS[i+1]:<4}: {rowc}")
            print(f"  ③ {mid_s}")


if __name__ == "__main__":
    main()
