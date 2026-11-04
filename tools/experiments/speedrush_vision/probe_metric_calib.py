# -*- coding: utf-8 -*-
"""钉定度量版提边的三个标定常量（T1 落产前的离线标定，跑一次收口）：

1. 相机内参 K*：UniDepth 自估内参（同游戏相机）跨金标帧的中位——产码用钉死常量，
   不依赖 UniDepth 进包；
2. 路半宽 W_ref：UniDepth fp32 场上逐帧「平面拟合 → 逐行提边 → 近带 Z4-14 半宽」
   的中位——产码逐帧尺度自标定的基准（s = W_ref / 半宽读数）；
3. 种子尺度 s0：DA-V2s 视差与 UniDepth 度量的逐帧配准尺度（median(Z_ud·d)，口径
   同 ud_dav2_field.py）的中位——产码平面拟合的初值（度量门限需要近似米制才成立）。

用法: probe_metric_calib.py   （数据路径写死，仅本机金标环境可跑）
输出: 三个常量的中位/离散度 + 按分层(stratum)的半宽分布（半宽是否场景常数的判据）。
"""
import csv
import json
from pathlib import Path

import numpy as np

OUT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/depth_review")
NPY = OUT / "npy"
CACHE = Path(r"D:/ud_test/cache")
CARS = json.loads(Path(r"D:/ud_test/car_boxes.json").read_text(encoding="utf-8"))

ABOVE = 0.15
TOL_LOW, TOL_OFF = 0.02, 0.30
ZBANDS = range(4, 14, 2)   # 近带（尺度标定与 W_ref 的口径带，产码同此）
GAP = 1.5


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
    labels = [r for r in csv.DictReader((OUT / "gold_labels.csv").open(encoding="utf-8"))
              if (CACHE / f"{Path(r['path']).stem}.npz").exists()]

    Ks, hws, ss, hs, frs, hw_by_stratum = [], [], [], [], [], {}
    for row in labels:
        stem = Path(row["path"]).stem
        d = np.load(CACHE / f"{stem}.npz")
        X = d["X"].astype(np.float32)
        Y = d["Y"].astype(np.float32)
        Z = d["Z"].astype(np.float32)
        Ks.append([d["K"][0, 0], d["K"][1, 1], d["K"][0, 2], d["K"][1, 2]])
        H, W = X.shape
        ok = np.isfinite(X) & (Z > 0)
        cm = car_mask(stem, H, W)
        sel = ok & (Z > 3) & (Z < 10) & (np.abs(X) < 5) & ~cm
        if sel.sum() < 80:
            continue
        coef = None
        for it in range(3):
            A = np.stack([X[sel], Z[sel], np.ones(sel.sum())], 1)
            coef, *_ = np.linalg.lstsq(A, Y[sel], rcond=None)
            res = Y - (coef[0] * X + coef[1] * Z + coef[2])
            sel = ok & (res > -TOL_LOW * Z - TOL_OFF) & (res < TOL_LOW * Z + TOL_OFF) \
                & (Z > 2) & (Z < 45) & (np.abs(X) < 12) & ~cm
            if sel.sum() < 80:
                break
        if coef is None or sel.sum() < 80:
            continue
        hs.append(float(coef[2]))                      # 平面常数 c = 相机离地高 h 的候选
        frs.append(float(Ks[-1][0] / Ks[-1][1]))       # 逐帧 fx/fy 比（输出换算用）
        ground = sel
        above = ok & (res < -ABOVE) & (Z > 2) & (Z < 45) & (np.abs(X) < 12) & ~cm
        Zrow = Z.mean(axis=1)
        frame_hw = []
        for zb in ZBANDS:
            rows = np.where((Zrow >= zb - 1.6) & (Zrow < zb + 1.6))[0]
            if rows.size < 6:
                continue
            r0, r1 = rows.min(), rows.max() + 1
            bm = np.zeros((H, W), bool)
            bm[r0:r1] = True
            carz = X[cm & bm]
            occL = bool(carz.size and np.any(carz < -1.5))
            occR = bool(carz.size and np.any(carz > 1.5))
            r = row_edges(X, ground, above, r0, r1)
            if r is None or occL or occR:
                continue
            frame_hw.append((r[1] - r[0]) / 2)
        if frame_hw:
            hws.append(float(np.median(frame_hw)))
            hw_by_stratum.setdefault(row["stratum"], []).append(hws[-1])
        # 种子尺度 s0：口径同 ud_dav2_field（UniDepth Z 配 DA 视差）
        match = list(NPY.glob(f"*{stem}__d336q4f16.npy"))
        if match:
            dd = np.load(match[0]).astype(np.float32)
            Zu = Z
            m = np.isfinite(Zu) & (Zu > 4) & (Zu < 20) & (dd > 0.5)
            if m.sum() >= 5000:
                ss.append(float(np.median(Zu[m] * dd[m])))

    K = np.array(Ks)
    print(f"K* 中位 fx={np.median(K[:,0]):.2f} fy={np.median(K[:,1]):.2f} "
          f"cx={np.median(K[:,2]):.2f} cy={np.median(K[:,3]):.2f}")
    print(f"K 离散(std/med): fx {np.std(K[:,0])/np.median(K[:,0]):.2%} "
          f"fy {np.std(K[:,1])/np.median(K[:,1]):.2%} "
          f"cx {np.std(K[:,2]):.1f}px cy {np.std(K[:,3]):.1f}px  (n={len(K)})")
    hw = np.array(hws)
    print(f"W_ref 近带半宽: 中位={np.median(hw):.2f}m std={np.std(hw):.2f}m "
          f"CV={np.std(hw)/np.median(hw):.1%} (n={len(hw)} 帧)")
    for st, v in sorted(hw_by_stratum.items()):
        v = np.array(v)
        print(f"  stratum {st}: n={len(v)} 中位={np.median(v):.2f} std={np.std(v):.2f}")
    s = np.array(ss)
    if len(s):
        print(f"种子 s0: 中位={np.median(s):.2f} CV={np.std(s)/np.median(s):.1%} "
              f"范围=[{s.min():.2f},{s.max():.2f}] (n={len(s)})")
    h = np.array(hs)
    fr = np.array(frs)
    if len(h):
        print(f"相机高 h(平面c): 中位={np.median(h):.2f}m std={np.std(h):.2f} (n={len(h)})")
        print(f"fx/fy 比: 中位={np.median(fr):.4f} std={np.std(fr):.4f} "
              f"(逐帧比值的离散远小于 fx 本身则比值可钉)")


if __name__ == "__main__":
    main()
