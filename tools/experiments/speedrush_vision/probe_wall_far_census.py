# -*- coding: utf-8 -*-
"""墙专项远场普查：护栏在 MoGe 点云里延伸到多远、各距离带墙点密度（stdout-only 零文件写）。

问题（2026-10-10 预登记，维护者"BEV 连两边的墙都看不到"）：
墙检测的深度箱 ZBIN 只扫 3~16m（depth_geo 真源），16m 外的墙在产线里根本
没消化。要决定「墙检测要不要扩远场箱 / BEV 要不要画远场墙」，先答地基：

  Q1 墙在点云里延伸到多远？——各 z 带（3~40m）内路侧（|X|∈[8,12.5]m）
     |hgt|>0.3m（WALL_HGT 同阈）的墙点数，按帧聚合中位 + 有墙帧占比。
     判据：某带有墙帧占比 ≥50% 视为「墙在该带可靠可见」，作为扩箱/显示
     的候选边界；<50% 的带只有零星墙点，画出来是噪声。
  Q2 墙点横向位置稳定吗？——各带有墙帧里墙点 |X| 中位（左右分侧）：
     护栏应在 ±9~11m 缓变；若散乱无结构，说明远带墙点混入了其他高物。
  Q3 路面质量随距离怎么衰减？——各带路面点（|X|<7m）平面残差 p50：
     旁路判据（PLANE_RESID_P50_MAX=0.06m）在哪一带失效，即远带路面
     「看得见但拟合不保证」的边界。

方法：depth_debug 证据包重放（解包口径与 replay_debug_evid 逐位一致）→
reading_from_points 产线入口拿平面 coef → hgt=Y−(aX+bZ+c) → 分带统计。
ego/object 掩码照喂（墙检测同口径，车不冒充墙）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_wall_far_census.py \
        --dir "%APPDATA%/MaaRacingMaster/data/speedrush/control_traces/depth_debug_20261009_210023"
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

FULL_W, FULL_H = 1280, 720
ZBANDS = ((3, 5), (5, 7), (7, 9), (9, 12), (12, 16), (16, 20), (20, 25),
          (25, 30), (30, 40))
WALL_HGT = 0.3        # 与产线墙线检测阈同源（depth_geo.WALL_HGT）
WALL_X_LO, WALL_X_HI = 8.0, 12.5   # 路侧护栏横向带（护栏实测 ±9~11m）
MIN_WALL_PTS = 20     # 单帧单带「有墙」的最少墙点数（噪声地板）
ROAD_X_MAX = 7.0      # 路面质量统计的横向半窗


def load_packet(stem: Path, cal):
    """解包口径与 replay_debug_evid.replay 逐位一致（产线入口拿 coef）。"""
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR)
                    for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H

    ego = obj = None
    ego_p = Path(str(stem) + "_ego.npy")
    if ego_p.exists():
        ego = np.unpackbits(np.load(ego_p))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        mask_p = Path(str(stem) + "_mask.npy")
        if mask_p.exists():
            merged = np.unpackbits(np.load(mask_p))[: FULL_W * FULL_H] \
                .reshape(FULL_H, FULL_W).astype(bool)
            obj = merged & ~ego

    rd = reading_from_points(pts, fx, cal, ego_mask=ego, object_mask=obj, fy=fy)
    return pts, rd


def band_stats(pts: np.ndarray, coef, z0: float, z1: float) -> dict:
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    band = np.isfinite(X) & (Z >= z0) & (Z < z1)
    n_all = int(band.sum())
    out = {"n": n_all, "wall_l": None, "wall_r": None,
           "resid_p50": None, "wall_x_l": None, "wall_x_r": None}
    if n_all == 0 or coef is None:
        return out
    hgt = Y - (coef[0] * X + coef[1] * Z + coef[2])
    wall = band & (np.abs(X) >= WALL_X_LO) & (np.abs(X) <= WALL_X_HI) \
        & (np.abs(hgt) > WALL_HGT)
    for side, key in ((-1, "l"), (1, "r")):
        ws = wall & (X * side > 0)
        if int(ws.sum()) >= MIN_WALL_PTS:
            out[f"wall_{key}"] = int(ws.sum())
            out[f"wall_x_{key}"] = float(np.median(np.abs(X[ws])))
    road = band & (np.abs(X) < ROAD_X_MAX) & (np.abs(hgt) < 2.0)
    if int(road.sum()) >= 50:
        out["resid_p50"] = float(np.nanmedian(np.abs(hgt[road])))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    d = Path(a.dir)
    stems = sorted(p.with_suffix("").parent / p.name[:-9]
                   for p in d.glob("*_evid.npz"))
    cal = load_calib()
    print(f"包数={len(stems)} dir={d.name}")
    rows = {b: {"n": [], "wl": [], "wr": [], "res": [], "xl": [], "xr": []}
            for b in ZBANDS}
    n_coef = 0
    for s in stems:
        try:
            pts, rd = load_packet(s, cal)
        except Exception as e:  # noqa: BLE001 —— 坏包跳过计数
            print(f"skip {s.name}: {e}")
            continue
        if rd.coef is None:
            continue
        n_coef += 1
        for b in ZBANDS:
            st = band_stats(pts, rd.coef, *b)
            r = rows[b]
            r["n"].append(st["n"])
            if st["wall_l"] is not None:
                r["wl"].append(st["wall_l"])
                r["xl"].append(st["wall_x_l"])
            if st["wall_r"] is not None:
                r["wr"].append(st["wall_r"])
                r["xr"].append(st["wall_x_r"])
            if st["resid_p50"] is not None:
                r["res"].append(st["resid_p50"])
    n = n_coef or 1
    print(f"coef 有效={n_coef}/{len(stems)}")
    hdr = (f"{'z带(m)':>9} | {'点数med':>8} | {'左墙帧%':>7} {'左墙点med':>9} "
           f"{'左X med':>8} | {'右墙帧%':>7} {'右墙点med':>9} {'右X med':>8} | "
           f"{'路残差p50':>9}")
    print(hdr)
    print("-" * len(hdr))
    for b, r in rows.items():
        wl_pct = 100.0 * len(r["wl"]) / n
        wr_pct = 100.0 * len(r["wr"]) / n
        wlm = f"{statistics.median(r['wl']):.0f}" if r["wl"] else "-"
        wrm = f"{statistics.median(r['wr']):.0f}" if r["wr"] else "-"
        xlm = f"{statistics.median(r['xl']):.1f}" if r["xl"] else "-"
        xrm = f"{statistics.median(r['xr']):.1f}" if r["xr"] else "-"
        res = f"{statistics.median(r['res']) * 100:.1f}cm" if r["res"] else "-"
        nm = f"{statistics.median(r['n']):.0f}" if r["n"] else "-"
        print(f"{b[0]:>3}-{b[1]:<5} | {nm:>8} | {wl_pct:>6.0f}% {wlm:>9} "
              f"{xlm:>8} | {wr_pct:>6.0f}% {wrm:>9} {xrm:>8} | {res:>9}")


if __name__ == "__main__":
    main()
