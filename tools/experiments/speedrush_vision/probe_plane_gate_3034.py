# -*- coding: utf-8 -*-
"""平面门拒只读诊断探针（临时，2026-10-14）：fid 3034 证据包逐项打点。

三候选分离（GPT 裁决路径）：①候选点选错（可见路面点没进拟合输入/被掩码过度排除）
②三维点不一致（路面点还原不出稳定平面）③质量门过苛（平面不错但阈值拒）。
只读：不修改产线任何行为；判定一律调产线函数，打印的是它消费的输入量。
对照实验：排除物体掩码（膨胀）后重拟合，比较 coef 与门判定。
用法（仓库根）：.venv/Scripts/python tools/experiments/speedrush_vision/probe_plane_gate_3034.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.speedrush.depth_geo import (
    DEGRADED_CAM_H_HI, DEGRADED_CAM_H_LO, DEGRADED_HGT_CLIP,
    DEGRADED_INLIER_MIN, DEGRADED_QUAD_MIN, DEGRADED_RESID_P50_MAX,
    DIAG_Y1, EGO_ABS_X, EGO_ABS_Z, MIN_PLANE_PTS, PLANE_INLIER_FLOOR,
    PLANE_INLIER_MIN, PLANE_QUALITY_Z_HI, PLANE_RESID_P50_MAX, PLANE_TOL,
    Y0, _dig_3d_mask, _dig_band, _fit_road_plane, _plane_degraded_ok,
    _plane_plausible, reading_from_points)

FULL_W, FULL_H = 1280, 720
PKG = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def load(stem: Path):
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    mask = None
    mp = Path(str(stem) + "_mask.npy")
    if mp.exists():
        mask = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
    print(f"fid={int(z['fid'])} fx={float(z['fx']):.3f}->{fx:.0f}px fy={float(z['fy']):.3f} "
          f"sol: sh={float(z['sol_sh']):.3f} resid={float(z['sol_resid']):.3f} n={int(z['sol_n'])}")
    return pts, fx, fy, mask


def report(stem: Path, tag: str, exclude: np.ndarray | None) -> None:
    pts, fx, fy, mask = load(stem)
    W = pts.shape[1]
    obj_band = _dig_band(mask, W)
    X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]

    # ---- 拟合输入构成（候选①：可见路面点进没进拟合） ----
    fin = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 0)
    road_dom = fin & (np.abs(X) < 4.0) & (Z > 4.0) & (Z < 30.0) \
        & ~((np.abs(X) < 1.3) & (Z < 10.0))
    print(f"\n[{tag}] 拟合输入构成（行 {Y0}~{DIAG_Y1}）:")
    print(f"  有限 3D 点: {int(fin.sum())}")
    print(f"  路面拟合域点(进拟合): {int(road_dom.sum())}  (需>={MIN_PLANE_PTS})")
    if exclude is not None:
        ex = exclude[Y0:DIAG_Y1]
        print(f"  路面域中被排除(对照实验): {int((road_dom & ex).sum())}")
    print(f"  域外: |X|>=4 {int((fin & (np.abs(X) >= 4.0)).sum())}  "
          f"Z<=4 {int((fin & (Z <= 4.0) & (np.abs(X) < 4.0)).sum())}  "
          f"Z>=30 {int((fin & (Z >= 30.0) & (np.abs(X) < 4.0)).sum())}  "
          f"自车框 {int((fin & (np.abs(X) < 1.3) & (Z < 10.0)).sum())}")

    coef = _fit_road_plane(X, Y, Z, fy=fy)
    if coef is None:
        print(f"[{tag}] _fit_road_plane=None（拟合失败，非门拒）")
        return
    a, b, c = float(coef[0]), float(coef[1]), float(coef[2])
    print(f"[{tag}] 拟合 coef: a(roll)={a:+.4f}({np.degrees(np.arctan(a)):+.2f}°) "
          f"b(pitch)={b:+.4f}({np.degrees(np.arctan(b)):+.2f}°) c={c:+.3f} cam_h={abs(c):.2f}")

    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    dig = _dig_3d_mask(X, Z, hgt, obj_band)
    road_q = (np.isfinite(hgt) & (np.abs(X) < 4.0)
              & (Z >= 4.0) & (Z < PLANE_QUALITY_Z_HI) & (~dig))
    inl_q = road_q & (np.abs(hgt) < PLANE_TOL)
    n_q, n_i = int(road_q.sum()), int(inl_q.sum())
    rate = n_i / n_q if n_q else float("nan")
    resid = float(np.nanmedian(np.abs(hgt[road_q]))) if n_q else float("nan")
    p90 = float(np.nanpercentile(np.abs(hgt[road_q]), 90)) if n_q else float("nan")
    print(f"[{tag}] 主门输入: 评判路区点={n_q} 内点={n_i} 内点率={rate:.3f} "
          f"(过{PLANE_INLIER_MIN=:.2f}? 残差中位={resid:.3f} p90={p90:.3f} "
          f"(旁路需<={PLANE_RESID_P50_MAX}、地板{PLANE_INLIER_FLOOR})")
    print(f"[{tag}] 挖除缝吃掉的路区点: {int((road_q | (np.isfinite(hgt) & (np.abs(X) < 4.0) & (Z >= 4.0) & (Z < PLANE_QUALITY_Z_HI))).sum()) - n_q}")

    # 四象限覆盖（补考判据④的输入量）
    ok_q = np.isfinite(hgt) & (np.abs(X) < 4.0) & (Z >= 4.0) & (Z < PLANE_QUALITY_Z_HI) & (~dig)
    clip = ok_q & (np.abs(hgt) <= DEGRADED_HGT_CLIP)
    xl, zmid = X[clip] < 0, Z[clip] < 8.0
    quads = {"近左(x<0,z<8)": int((xl & zmid).sum()), "近右(x≥0,z<8)": int((~xl & zmid).sum()),
             "远左(x<0,z≥8)": int((xl & ~zmid).sum()), "远右(x≥0,z≥8)": int((~xl & ~zmid).sum())}
    cinl = clip & (np.abs(hgt) < PLANE_TOL)
    crate = int(cinl.sum()) / len(clip[clip]) if clip.any() else float("nan")
    cresid = float(np.nanmedian(np.abs(hgt[clip]))) if clip.any() else float("nan")
    print(f"[{tag}] 补考输入: 剪后点(|hgt|<={DEGRADED_HGT_CLIP})={int(clip.sum())} "
          f"四象限={quads} (各需>={DEGRADED_QUAD_MIN})")
    print(f"[{tag}] 补考: 剪后内点率={crate:.3f}(需>={DEGRADED_INLIER_MIN}) "
          f"残差中位={cresid:.3f}(需<={DEGRADED_RESID_P50_MAX}) "
          f"cam_h={abs(c):.2f}(需∈[{DEGRADED_CAM_H_LO},{DEGRADED_CAM_H_HI}])")

    verdict = _plane_plausible(coef, X, Y, Z, dig)
    dverdict = _plane_degraded_ok(coef, X, Y, Z, dig)
    print(f"[{tag}] 产线判定: 主门={'过' if verdict else '拒'} 补考={'过' if dverdict else '拒'} "
          f"→ {'放行' if verdict or dverdict else '全弃权'}")


def main() -> None:
    base = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\debug\speedrush"
                r"\20261014_112029_p2\depth_debug")
    stem = base / (PKG if PKG else "d00014")
    print("=" * 72)
    report(stem, "原路径(产线同款)", None)
    print("=" * 72)
    # 对照实验：排除物体掩码膨胀后的重拟合
    z = np.load(Path(str(stem) + "_evid.npz"))
    mp = Path(str(stem) + "_mask.npy")
    if mp.exists():
        mask = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        excl = cv2.dilate(mask.astype(np.uint8), np.ones((61, 61), np.uint8)).astype(bool)
        report(stem, "对照(排除物体掩码膨胀61px)", excl)
    else:
        print("无 mask.npy，对照实验跳过")


if __name__ == "__main__":
    main()
