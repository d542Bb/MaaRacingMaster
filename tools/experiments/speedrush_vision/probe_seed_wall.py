# -*- coding: utf-8 -*-
"""种子被墙劫持只读诊断探针（临时，2026-10-14）：回归四帧上复放产线特征行走。

背景：自车贴右护栏时种子平面在种子阶段已歪（d00014/d00021 roll ≈ −5.4°/−6.1°），
走廊 15cm 内点带以错误平面为参照稳定收敛到错误答案。修法方向（GPT/维护者裁决）：
候选点的**地面物理可行性**判据（相机安装高度带 + 坡度界，独立于待估平面）过滤
行走输出，再进种子拟合。本探针只读：不改产线；先拿「被标为路面点」的相机高度
分布，再试验可行性过滤对种子/走廊/门判定的影响。

用法（仓库根）：.venv/Scripts/python tools/experiments/speedrush_vision/probe_seed_wall.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.speedrush.depth_geo import (
    DEGRADED_CAM_H_HI, DEGRADED_CAM_H_LO, DEGRADED_HGT_CLIP,
    DEGRADED_INLIER_MIN, DEGRADED_QUAD_MIN, DEGRADED_RESID_P50_MAX,
    DIAG_Y1, EGO_ABS_X, EGO_ABS_Z, MIN_PLANE_PTS, PLANE_INLIER_FLOOR,
    PLANE_INLIER_MIN, PLANE_QUALITY_Z_HI, PLANE_RESID_P50_MAX, PLANE_TOL,
    PLANE_ITERS, WALK_Z_MIN, Y0, _dig_3d_mask, _dig_band,
    _fit_road_plane_seed, _plane_degraded_ok, _plane_plausible,
    _road_by_column_profile)

FULL_W, FULL_H = 1280, 720
BASE = Path(os.environ["APPDATA"]) / "MaaRacingMaster/debug/speedrush"
SESS = BASE / "20261014_112029_p2" / "depth_debug"
# 回归帧：2783 干净基准 / 3034、3178 贴右护栏（dgeo 拒帧）
STEMS = {"d00002": "2783 干净", "d00014": "3034 贴栏", "d00021": "3178 贴栏"}
# 可行性试验坡度界（tan）：6°=保守公路坡度，8°=含游戏坡道余量
PITCH_BOUNDS = {"6°": 0.1051, "8°": 0.1405}


def load(stem: str):
    z = np.load(SESS / f"{stem}_evid.npz")
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    mp = SESS / f"{stem}_mask.npy"
    mask = None
    if mp.exists():
        mask = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
    return pts, fx, fy, mask, int(z["fid"])


def seed_coef(road, X, Y, Z):
    return _fit_road_plane_seed(road, X, Y, Z)


def corridor_from_seed(seed, X, Y, Z):
    """产线 _fit_road_plane 的走廊收敛段（种子起 15cm 内点重选）。"""
    if seed is None:
        return None
    ok = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 0)
    corr = (ok & (np.abs(X) < 4.0) & (Z > 4.0) & (Z < 30.0)
            & ~((np.abs(X) < 1.3) & (Z < 10.0)))
    xs, ys, zs = X[corr], Y[corr], Z[corr]
    m = np.abs(ys - (seed[0] * xs + seed[1] * zs + seed[2])) < PLANE_TOL
    coef = seed
    for _ in range(PLANE_ITERS):
        if int(m.sum()) < MIN_PLANE_PTS:
            return None
        A = np.stack([xs[m], zs[m], np.ones(int(m.sum()), np.float32)], 1)
        coef = np.linalg.lstsq(A, ys[m], rcond=None)[0]
        m_next = np.abs(ys - (coef[0] * xs + coef[1] * zs + coef[2])) < PLANE_TOL
        if np.array_equal(m_next, m):
            break
        m = m_next
    return coef


def gate_report(tag, coef, X, Y, Z, obj_band):
    if coef is None:
        print(f"  [{tag}] coef=None")
        return
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    dig = _dig_3d_mask(X, Z, hgt, obj_band)
    road_q = (np.isfinite(hgt) & (np.abs(X) < 4.0)
              & (Z >= 4.0) & (Z < PLANE_QUALITY_Z_HI) & (~dig))
    inl_q = road_q & (np.abs(hgt) < PLANE_TOL)
    rate = float(inl_q.sum()) / max(1, int(road_q.sum()))
    resid = float(np.nanmedian(np.abs(hgt[road_q]))) if road_q.any() else float("nan")
    ok = _plane_plausible(coef, X, Y, Z, dig)
    dok = _plane_degraded_ok(coef, X, Y, Z, dig)
    print(f"  [{tag}] roll={np.degrees(np.arctan(a)):+.2f}° pitch={np.degrees(np.arctan(b)):+.2f}° "
          f"cam_h={abs(c):.2f} 内点率={rate:.3f} 残差p50={resid:.3f} "
          f"主门={'过' if ok else '拒'} 补考={'过' if dok else '拒'} "
          f"→ {'放行' if ok or dok else '全弃权'}")


def feasible(road, X, Y, Z, pl):
    """地面物理可行性：Y ∈ [cam_h_lo − pl·s, cam_h_hi + pl·s]，s=|X|+Z。

    独立于待估平面：只用安装高度界与坡度界，不消费任何拟合结果。"""
    s = np.abs(X) + Z
    return road & (Y > DEGRADED_CAM_H_LO - pl * s) & (Y < DEGRADED_CAM_H_HI + pl * s)


def main() -> None:
    for stem, label in STEMS.items():
        pts, fx, fy, mask, fid = load(stem)
        obj_band = _dig_band(mask, pts.shape[1])
        X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
        bad = (~np.isfinite(Z) | (Z <= WALK_Z_MIN)
               | ((np.abs(X) < EGO_ABS_X) & (Z < EGO_ABS_Z)))
        road = _road_by_column_profile(Z, fy, bad)
        print("=" * 76)
        print(f"[{stem} fid={fid} {label}] 行走路点={int(road.sum())}")

        # ---- 路点相机高度分布（右半=X≥0 护栏侧；按 Z 桶给 Y 分位） ----
        m = road & np.isfinite(Y) & np.isfinite(Z)
        print("  路点 Y（相机高度，向下为正）按 Z 桶分位 [p5/p50/p95]，左|右半:")
        for z0, z1 in ((4, 8), (8, 12), (12, 18), (18, 30)):
            for side, sm in (("左", m & (X < 0)), ("右", m & (X >= 0))):
                mm = sm & (Z >= z0) & (Z < z1)
                if int(mm.sum()) < 20:
                    print(f"    Z[{z0:>2},{z1:>2}) {side}: n={int(mm.sum()):>6}  (少)")
                    continue
                ys = Y[mm]
                print(f"    Z[{z0:>2},{z1:>2}) {side}: n={int(mm.sum()):>6} "
                      f"Y {np.percentile(ys, 5):+.2f}/{np.percentile(ys, 50):+.2f}"
                      f"/{np.percentile(ys, 95):+.2f}")

        # ---- 种子（产线现状）与走廊收敛 + 门判定 ----
        seed = seed_coef(road, X, Y, Z)
        if seed is not None:
            n_seed = int((road & np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
                          & (Z > 4.0) & (Z < 30.0) & (np.abs(X) < 4.0)).sum())
            print(f"  种子(现状): roll={np.degrees(np.arctan(seed[0])):+.2f}° "
                  f"pitch={np.degrees(np.arctan(seed[1])):+.2f}° cam_h={abs(seed[2]):.2f} "
                  f"种子点={n_seed}")
        coef = corridor_from_seed(seed, X, Y, Z)
        gate_report("走廊收敛(现状)", coef, X, Y, Z, obj_band)

        # ---- 可行性过滤试验 ----
        for pname, pl in PITCH_BOUNDS.items():
            fmask = feasible(road, X, Y, Z, pl)
            cut = int(m.sum() - fmask.sum())
            seed2 = seed_coef(fmask, X, Y, Z)
            line = f"  可行性过滤({pname}): 割路点={cut}/{int(m.sum())}"
            if seed2 is None:
                print(line + " → 种子失败(None)")
                continue
            line += (f" 种子roll={np.degrees(np.arctan(seed2[0])):+.2f}°"
                     f" cam_h={abs(seed2[2]):.2f}")
            coef2 = corridor_from_seed(seed2, X, Y, Z)
            print(line)
            gate_report(f"过滤({pname})后走廊", coef2, X, Y, Z, obj_band)


if __name__ == "__main__":
    main()
