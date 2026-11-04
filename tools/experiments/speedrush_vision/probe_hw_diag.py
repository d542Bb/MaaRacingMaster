# -*- coding: utf-8 -*-
"""诊断：每帧近带 L/R 缘的米值与旧 a_x 车道单位值，按 stratum/lcls/rcls 分组看谁稳定。

背景：probe_metric_calib.py 首跑显示近带半宽按分层散布 2.3~5.4m（curve_cont 5.30 vs
curve_cont2 2.98，同为弯道层却差 1.8 倍）。本探针回答「半宽的哪个口径才是场景常数」：
米制（UniDepth 平面 X）与旧 a_x 车道单位（x_lane_of）逐帧并排——谁稳定，逐帧尺度
自标定就锚在谁上。
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
OUT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/depth_review")
CACHE = Path(r"D:/ud_test/cache")
CARS = json.loads(Path(r"D:/ud_test/car_boxes.json").read_text(encoding="utf-8"))
ABOVE, TOL_LOW, TOL_OFF, GAP = 0.15, 0.02, 0.30, 1.5
ZB = (5.0, 9.0)  # 单一参考带

from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402
CAL = load_calib()


def car_mask(stem, h, w):
    m = np.zeros((h, w), bool)
    for cx, cy, bw, bh, conf in CARS.get(stem, []):
        x1, y1 = max(cx - bw // 2 - 8, 0), max(cy - bh // 2, 0)
        x2, y2 = min(cx + bw // 2 + 8, w), min(cy + bh // 2 + 15, h)
        m[y1:y2, x1:x2] = True
    return m


def row_edges(X, ground, above, r0, r1):
    L, R = [], []
    for r in range(r0, r1):
        gx = X[r][ground[r]]
        if gx.size < 15:
            continue
        med = np.median(gx)
        xl, xr = gx.min(), gx.max()
        ax = np.sort(X[r][above[r]])
        if ax.size:
            s = [ax[0], ax[0]]
            cols = []
            for x in ax[1:]:
                if x - s[1] > 0.6:
                    cols.append((s[0], s[1]))
                    s = [x, x]
                else:
                    s[1] = x
            cols.append((s[0], s[1]))
            for (a, b) in cols:
                if a > med + GAP:
                    xr = min(xr, a)
                elif b < med - GAP:
                    xl = max(xl, b)
        if xr - xl > 1.0:
            L.append(xl)
            R.append(xr)
    if len(L) < 5:
        return None
    return float(np.median(L)), float(np.median(R))


def main():
    labels = list(csv.DictReader((OUT / "gold_labels.csv").open(encoding="utf-8")))
    res = []
    for row in labels:
        stem = Path(row["path"]).stem
        f = CACHE / f"{stem}.npz"
        if not f.exists():
            continue
        d = np.load(f)
        X = d["X"].astype(np.float32)
        Y = d["Y"].astype(np.float32)
        Z = d["Z"].astype(np.float32)
        fx, fy, cx, cy = d["K"][0, 0], d["K"][1, 1], d["K"][0, 2], d["K"][1, 2]
        H, W = X.shape
        ok = np.isfinite(X) & (Z > 0)
        cm = car_mask(stem, H, W)
        sel = ok & (Z > 3) & (Z < 10) & (np.abs(X) < 5) & ~cm
        if sel.sum() < 80:
            continue
        for _ in range(3):
            A = np.stack([X[sel], Z[sel], np.ones(sel.sum())], 1)
            coef, *_ = np.linalg.lstsq(A, Y[sel], rcond=None)
            res_ = Y - (coef[0] * X + coef[1] * Z + coef[2])
            sel = ok & (res_ > -TOL_LOW * Z - TOL_OFF) & (res_ < TOL_LOW * Z + TOL_OFF) \
                & (Z > 2) & (Z < 45) & (np.abs(X) < 12) & ~cm
            if sel.sum() < 80:
                break
        if sel.sum() < 80:
            continue
        ground, above = sel, ok & (res_ < -ABOVE) & (Z > 2) & (Z < 45) & (np.abs(X) < 12) & ~cm
        Zrow = Z.mean(axis=1)
        rows = np.where((Zrow >= ZB[0] - 1.6) & (Zrow < ZB[1] + 1.6))[0]
        if rows.size < 6:
            continue
        r0, r1 = rows.min(), rows.max() + 1
        bm = np.zeros((H, W), bool)
        bm[r0:r1] = True
        carz = X[cm & bm]
        occL = bool(carz.size and np.any(carz < -1.5))
        occR = bool(carz.size and np.any(carz > 1.5))
        e = row_edges(X, ground, above, r0, r1)
        if e is None:
            continue
        xl, xr = e
        Zmed = float(np.median(Zrow[rows]))
        v_ref = int(np.median(rows))
        uL = xl * fx / Zmed + cx
        uR = xr * fx / Zmed + cx
        laneL = x_lane_of(int(round(uL)), v_ref, CAL)
        laneR = x_lane_of(int(round(uR)), v_ref, CAL)
        res.append((row["stratum"], row["lcls"], row["rcls"], occL or occR,
                    xl, xr, (xr - xl) / 2, laneL, laneR, (laneR - laneL) / 2, stem))

    print(f"{'stratum':<13}{'lcls':<6}{'rcls':<6}{'occ':<5}{'X_L':>6}{'X_R':>6}{'hw_m':>6}"
          f" | {'laneL':>6}{'laneR':>6}{'hw_lane':>8}  stem")
    for st, lc, rc, occ, xl, xr, hw, lL, lR, hwl, stem in res:
        print(f"{st:<13}{lc:<6}{rc:<6}{str(occ):<5}{xl:>6.2f}{xr:>6.2f}{hw:>6.2f}"
              f" | {lL:>6.2f}{lR:>6.2f}{hwl:>8.2f}  {stem}")
    by = {}
    for st, lc, rc, occ, xl, xr, hw, lL, lR, hwl, stem in res:
        by.setdefault(st, []).append((hw, hwl))
    print("\n汇总: stratum n  hw_m中位±std   hw_lane中位±std")
    for st, v in sorted(by.items()):
        a = np.array([x[0] for x in v])
        b = np.array([x[1] for x in v])
        print(f"{st:<13} {len(a):>2}  {np.median(a):5.2f}±{np.std(a):4.2f}"
              f"   {np.median(b):5.2f}±{np.std(b):4.2f}")


if __name__ == "__main__":
    main()
