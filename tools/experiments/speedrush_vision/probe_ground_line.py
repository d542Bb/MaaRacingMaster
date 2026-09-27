# -*- coding: utf-8 -*-
"""道路边界基线修正：把「逐行独立分位」换成「几何正确的地面直线」。

诊断收口（2026-09-26，BEV 验证）：问题不在图像空间 vs 俯视空间，而在
**地面基线本身**。平坦路面 + 相对视差仿射 ⇒ 地面视差是 (行−地平线) 的一次函数
d_g(v)=A·(v−y_h)+B（两个参数）。现行产码用「每行各自取 20% 分位」= 375 个独立值，
远带（y<390）路面是窄条、分位被雾/天带偏 → 远处路面相对基线 −46%（蓝带）→
块内沿采错 → 撞墙型坏读数。

修法：对逐行 q20 点集做 **RANSAC 直线拟合**——近中带可靠行定 A、B，远带雾行
判外点剔除，再用直线外推到全带。基线从「逐行自由」变「全局两参」，远带不再各自
为政。输出仍是左右路沿车道数，下游零改动。

对照：rowq20（产码）vs line（本探针），同守卫同选择环，金标 45 卷。
用法：python tools/experiments/speedrush_vision/probe_ground_line.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
import probe_ground_ab as pab  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

CAL = pab.CAL
NPY_DIR = pd.OUT / "npy"


def line_field(m, tol_px=0.06, iters=200, rng=np.random.default_rng(0)):
    """地面直线基线场：RANSAC 拟合 d_g(v)=A·(v−y_h)+B → 2D 场（沿 x 复制）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    h, w = band.shape
    cols = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:w]
    q20 = np.quantile(band[:, cols], dg.Q_GROUND, axis=1)      # 逐行种子
    t = (np.arange(h, dtype=np.float64) + dg.Y0 - CAL.y_h)     # (v−y_h)
    good = q20 > 1e-3
    t, q = t[good], q20[good].astype(np.float64)
    best = (None, -1)
    n = len(t)
    for _ in range(iters):
        i, j = rng.integers(0, n, 2)
        if t[i] == t[j]:
            continue
        a = (q[i] - q[j]) / (t[i] - t[j])
        b = q[i] - a * t[i]
        if a <= 0:                                              # 视差随行增
            continue
        r = np.abs(q - (a * t + b)) / np.maximum(a * t + b, 1e-6)
        inl = int((r < tol_px).sum())
        if inl > best[1]:
            best = ((a, b), inl)
    (a, b), _ = best
    # 内点重拟合（最小二乘）精修
    r = np.abs(q - (a * t + b)) / np.maximum(a * t + b, 1e-6)
    sel = r < tol_px
    A = np.stack([t[sel], np.ones(sel.sum())], 1)
    coef, *_ = np.linalg.lstsq(A, q[sel], rcond=None)
    a, b = float(coef[0]), float(coef[1])
    dg_v = (a * (np.arange(h, dtype=np.float32) + dg.Y0 - CAL.y_h) + b)
    return np.broadcast_to(dg_v[:, None], (h, w)).astype(np.float32).copy()


def load_frames():
    labels = list(csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")))
    out = []
    for r in labels:
        p = NPY_DIR / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy"
        if p.exists():
            out.append((r, np.load(p).astype(np.float32)))
    return out


def gold_row(frames, fields, gl, gr, bad=1.0):
    devs = {"L": [], "R": []}
    both = 0
    for i, (r, m) in enumerate(frames):
        blocks = pab._blocks_with_field(m, gl, gr, fields[i])
        got = {}
        for side in ("L", "R"):
            f = pab._select(blocks, m, side, gl, gr)
            if f is None:
                continue
            gx = pab._gold_x_at(r, side, f["y_ref"])
            if gx is None:
                continue
            got[side] = f
            devs[side].append(f["lane"] - dg.x_lane_of(
                int(round(gx)), int(round(f["y_ref"])), CAL))
        both += len(got) == 2
    cells = [f"{gl:.2f}/{gr:.2f}"]
    for side in ("L", "R"):
        d = devs[side]
        cells += [f"{side}n={len(d):>2}",
                  f"dev={np.median(d):+.2f}" if d else "dev=n/a",
                  f"p90={np.percentile(np.abs(d),90):.2f}" if d else "p90=n/a",
                  f"坏={sum(1 for v in d if abs(v)>bad)}"]
    cells += [f"双={both}"]
    return " ".join(cells)


def road_region_edges(m, g, tol=0.18):
    """不找墙，直接找路面区域：与地面直线相符的像素=路。
    返回逐行路的左/右边界 x（内沿），喂给 dg._fit 出车道读数。
    近处路宽、远处路窄都直接量，不依赖"护栏过相对门"。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    h, w = band.shape
    rel = (band - g) / np.maximum(g, 1e-6)
    road = (np.abs(rel) < tol) & (band > 1e-3)
    road[:, dg.EGO_COLS[0]:dg.EGO_COLS[1]] = False   # 自车列不算路
    innerL, innerR = {}, {}
    for r in range(h):
        xs = np.nonzero(road[r])[0]
        if len(xs) < 40:                              # 该行路太碎，弃
            continue
        y = r + dg.Y0
        left = xs[xs < 640]
        right = xs[xs >= 640]
        # 只在护栏真的落在画面内时记边：贴到画面边框（近处路铺满）的行不算
        if len(left) and left.min() > 8:
            innerL[y] = int(left.min())               # 路向左的尽头=左护栏
        if len(right) and right.max() < w - 8:
            innerR[y] = int(right.max())              # 路向右的尽头=右护栏
    return innerL, innerR


def gold_row_region(frames, fields, tol, bad=1.0):
    devs = {"L": [], "R": []}
    both = 0
    for i, (r, m) in enumerate(frames):
        innerL, innerR = road_region_edges(m, fields[i], tol)
        got = {}
        for side, inner in (("L", innerL), ("R", innerR)):
            f = dg._fit(inner, CAL)
            if f is None or f["dead"] or not dg._pass(f, side):
                continue
            gx = pab._gold_x_at(r, side, f["y_ref"])
            if gx is None:
                continue
            got[side] = f
            devs[side].append(f["lane"] - dg.x_lane_of(
                int(round(gx)), int(round(f["y_ref"])), CAL))
        both += len(got) == 2
    cells = [f"tol={tol:.2f}"]
    for side in ("L", "R"):
        d = devs[side]
        cells += [f"{side}n={len(d):>2}",
                  f"dev={np.median(d):+.2f}" if d else "dev=n/a",
                  f"p90={np.percentile(np.abs(d),90):.2f}" if d else "p90=n/a",
                  f"坏={sum(1 for v in d if abs(v)>bad)}"]
    cells += [f"双={both}"]
    return " ".join(cells)


def main():
    frames = load_frames()
    print(f"金标缓存 {len(frames)} 帧")
    fields = {
        "rowq20": [pab._row_q20_field(m) for _, m in frames],
        "line": [line_field(m) for _, m in frames],
    }
    m0 = frames[0][1]
    g0 = fields["line"][0]
    band = m0[dg.Y0:dg.DIAG_Y1]
    near = slice(130, 230)
    res = float(np.median(np.abs((band[near] - g0[near]) / np.maximum(g0[near], 1e-6))))
    print(f"line 近带路面残差中位 |rel|={res:.3f}（对照 rowq20 全带蓝带 −46%）")

    print("\n── A. 现行「找墙块」+ 基线对照（守卫=产码）──")
    for gl, gr in ((0.08, 0.10), (0.10, 0.12)):
        for tag in ("rowq20", "line"):
            print(f"{tag:>7}", gold_row(frames, fields[tag], gl, gr))
        print()

    print("── B. 新「路面区域」提边 + 基线对照（tol 扫描）──")
    for tol in (0.12, 0.18, 0.25):
        for tag in ("rowq20", "line"):
            print(f"{tag:>7}", gold_row_region(frames, fields[tag], tol))
        print()


if __name__ == "__main__":
    main()
