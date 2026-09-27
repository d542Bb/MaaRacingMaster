# -*- coding: utf-8 -*-
"""车道单位与米制宽度互证（用户问：路宽单位和值正确吗？）。

三口径对表（全部独立来源，互不循环）：
1. **米制宽**：金标边界线过 UniDepth fp32 场的地面平面（独立单目尺度头）反投影
   → 米；双侧同帧相减 = 米制路宽。
2. **车道宽**：同一像素过 gate0 车道尺（x_lane_of，标定工程 L≈583px 收杆产物）
   → 车道；同帧相减 = 车道制路宽。
3. **换算比** 米/车道 = 隐含车道物理宽：若尺与米制一致，应跨帧、跨行恒定且
   ≈3~3.7m；随行漂移=尺的投影模型误差；随帧散=尺度问题。
另用标定工程的独立车道间距 L=583px 在自车行 v_ego 换算一份米制车道宽对表。

用法（仓库根 .venv）：python tools/experiments/speedrush_vision/probe_units_check.py
数据：depth_review/gold_labels.csv + D:/ud_test/cache（UniDepth fp32 场）。
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import numpy as np

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

OUT = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush" / "depth_review"
CACHE = Path(r"D:\ud_test\cache")
CARS = json.loads(Path(r"D:\ud_test\car_boxes.json").read_text(encoding="utf-8"))
TOL_LOW, TOL_OFF = 0.02, 0.30
VS = (520, 560, 600, 640, 680)
L_LANES = 583.0            # 标定工程四场收敛车道间距 px（格阵口径，CV 0.2%）


def car_mask(stem, h, w):
    m = np.zeros((h, w), bool)
    for cx, cy, bw, bh, conf in CARS.get(stem, []):
        x1, y1 = max(cx - bw // 2 - 8, 0), max(cy - bh // 2, 0)
        x2, y2 = min(cx + bw // 2 + 8, w), min(cy + bh // 2 + 15, h)
        m[y1:y2, x1:x2] = True
    return m


def main() -> None:
    cal = load_calib()
    rows = [r for r in csv.DictReader((OUT / "gold_labels.csv").open(encoding="utf-8"))
            if (CACHE / f"{Path(r['path']).stem}.npz").exists()]
    ratios = []          # (stem, v, 米宽, 车道宽, 比值)
    lane_ego = []        # 自车行换算的车道物理宽
    for row in rows:
        stem = Path(row["path"]).stem
        d = np.load(CACHE / f"{stem}.npz")
        X, Y, Z, K = (d["X"].astype(np.float32), d["Y"].astype(np.float32),
                      d["Z"].astype(np.float32), d["K"])
        fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
        H, W = X.shape
        ok = np.isfinite(X) & (Z > 0)
        cm = car_mask(stem, H, W)
        sel = ok & (Z > 3) & (Z < 10) & (np.abs(X) < 5) & ~cm
        if sel.sum() < 80:
            continue
        coef = None
        for _ in range(3):
            A = np.stack([X[sel], Z[sel], np.ones(sel.sum())], 1)
            coef, *_ = np.linalg.lstsq(A, Y[sel], rcond=None)
            res = Y - (coef[0] * X + coef[1] * Z + coef[2])
            sel = ok & (res > -TOL_LOW * Z - TOL_OFF) & (res < TOL_LOW * Z + TOL_OFF) \
                & (Z > 2) & (Z < 45) & (np.abs(X) < 12) & ~cm
            if sel.sum() < 80:
                break
        if coef is None or sel.sum() < 80:
            continue
        a, b, c = coef
        vals = {}
        for side in ("l", "r"):
            if row[side + "cls"] == "skip":
                continue
            p1 = np.array([float(row[side + "_nx"]), float(row[side + "_ny"])])
            p2 = np.array([float(row[side + "_fx"]), float(row[side + "_fy"])])
            lo, hi = sorted((p1[1], p2[1]))
            for v in range(int(lo), int(hi) + 1, 4):   # 沿线段全程采样
                t = (v - p1[1]) / (p2[1] - p1[1])
                u = float(p1[0] + t * (p2[0] - p1[0]))
                if not (v > cy + 1 and 0.0 <= u < W):
                    continue
                den = (v - cy) / fy - b - a * (u - cx) / fx
                if abs(den) < 1e-6:
                    continue
                Zg = c / den
                if not (2.0 < Zg < 60.0):
                    continue
                Xm = (u - cx) * Zg / fx
                vals.setdefault(v, {})[side.upper()] = (Xm, x_lane_of(int(round(u)), v, cal))
        for v, dv in vals.items():
            if "L" in dv and "R" in dv:
                xm = dv["R"][0] - dv["L"][0]
                xl = dv["R"][1] - dv["L"][1]
                if xl > 0.3:
                    ratios.append((stem, v, xm, xl, xm / xl))
        # 自车行换算：地面 Z(v_ego) × 583px / fx
        den = (cal.v_ego - cy) / fy - b
        Zg = c / den
        if 2.0 < Zg < 30.0:
            lane_ego.append(L_LANES * Zg / fx)
    q = lambda xs, p: float(np.percentile(np.asarray(xs), p))
    print(f"对表帧 {len(set(r[0] for r in ratios))}，同帧双侧行样本 {len(ratios)}")
    rs = [r[4] for r in ratios]
    print(f"隐含车道物理宽（米/车道）: p10/50/90 = {q(rs,10):.2f}/{q(rs,50):.2f}/{q(rs,90):.2f}")
    per_stem = {}
    for stem, _, _, _, r in ratios:
        per_stem.setdefault(stem, []).append(r)
    spread = [max(v) - min(v) for v in per_stem.values() if len(v) >= 3]
    print(f"帧内行间散布（最大-最小）: p50={q(spread,50):.2f} p90={q(spread,90):.2f} m")
    if lane_ego:
        print(f"口径3 自车行 583px 换算车道宽: p10/50/90 = "
              f"{q(lane_ego,10):.2f}/{q(lane_ego,50):.2f}/{q(lane_ego,90):.2f} m")
    byv = {}
    for _, v, _, _, r in ratios:
        byv.setdefault(v, []).append(r)
    print("随行偏移（尺投影模型漂移检查）: " + "  ".join(
        f"v{v}={q(byv[v],50):.2f}m" for v in sorted(byv)))


if __name__ == "__main__":
    main()
