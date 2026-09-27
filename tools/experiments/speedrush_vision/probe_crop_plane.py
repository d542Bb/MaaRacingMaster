# -*- coding: utf-8 -*-
"""裁切假设检验：平面基线残差按行分布 + 不同裁切窗口的金标卷对照。

维护者假设（2026-09-26，看图圈裁切范围）：路是平地，平面模型该成立；
残差若集中在带顶（雾/天锁基线）与带底（近处运动模糊/自车影），则裁掉两端
能同时修好 plane 的 R 崩与 rowq20 的远带蓝带。

两步：
1. 残差剖面：每行「地面代理（非自车列 25 分位）相对平面」的偏差，45 帧聚合。
   看残差堆在哪几行——决定裁切窗口，不靠猜。
2. 金标卷：全带 [340,715] vs 数据驱动的若干窗口，报 L/R 覆盖/dev/p90/坏。

用法：python tools/experiments/speedrush_vision/probe_crop_plane.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

CAL = dg.Calib if hasattr(dg, "Calib") else None
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402
CAL = load_calib()
NPY_DIR = pd.OUT / "npy"
EGO = dg.EGO_COLS


def load_frames():
    labels = list(csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")))
    out = []
    for r in labels:
        p = NPY_DIR / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy"
        if p.exists():
            out.append((r, np.load(p).astype(np.float32)))
    return out


def plane_fit(band, y0, stride=4, iters=4):
    """band=[y0,y1) 视差 → 平面系数 (a,b,c)，dy=y−y_h。"""
    h, w = band.shape
    yy = (np.arange(h, dtype=np.float32)[:, None] + y0 - CAL.y_h)
    xx = (np.arange(w, dtype=np.float32)[None, :] - 640.0)
    dy = np.broadcast_to(yy, (h, w))[::stride, ::stride]
    dx = np.broadcast_to(xx, (h, w))[::stride, ::stride]
    db = band[::stride, ::stride]
    cols = np.r_[0:EGO[0], EGO[1]:w]
    q = np.quantile(band[:, cols], 0.2, axis=1)[::stride]
    keep = db <= q[:, None] * 1.02
    A = np.stack([dy[keep], dx[keep], np.ones(int(keep.sum()), np.float32)], 1)
    coef, *_ = np.linalg.lstsq(A, db[keep], rcond=None)
    for _ in range(iters):
        pred = dy * coef[0] + dx * coef[1] + coef[2]
        r = db - pred
        mad = float(np.median(np.abs(r[keep]))) or 1e-6
        keep = (r < 0.5 * mad) & (r > -3.0 * mad)
        A = np.stack([dy[keep], dx[keep], np.ones(int(keep.sum()), np.float32)], 1)
        coef, *_ = np.linalg.lstsq(A, db[keep], rcond=None)
    return coef


def residual_profile(frames):
    """每行「路面像素（非自车列，低于该行40分位）相对平面」的偏差，45帧中位。
    残差堆在哪几行=平面模型在哪失效，直接决定裁切窗口。"""
    y0, y1 = dg.Y0, dg.DIAG_Y1
    prof = []
    for _, m in frames:
        band = m[y0:y1]
        h, w = band.shape
        coef = plane_fit(band, y0)
        dy = np.broadcast_to((np.arange(h, dtype=np.float32)[:, None] + y0 - CAL.y_h), (h, w))
        dx = np.broadcast_to((np.arange(w, dtype=np.float32)[None, :] - 640.0), (h, w))
        plane = coef[0] * dy + coef[1] * dx + coef[2]
        cols = np.r_[0:EGO[0], EGO[1]:w]
        thr = np.percentile(band[:, cols], 40, axis=1)[:, None]
        road = (band <= thr) & (band > 0)
        rel = np.where(road, (band - plane) / np.maximum(plane, 1e-6), np.nan)
        prof.append(np.nanmedian(np.abs(rel), axis=1))
    P = np.nanmedian(np.stack(prof), 0)
    print("残差剖面（路面像素相对平面，|rel| 中位，全带[340,715]拟合）：")
    for start in range(0, y1 - y0, 25):
        seg = P[start:start + 25]
        yv = y0 + start
        print(f"  y∈[{yv:3d},{yv+len(seg):3d})  |rel|中位={np.nanmedian(seg):.3f}")


def blocks_with_field(m, gl, gr, g, y0, y1):
    band = m[y0:y1]
    r = np.full(m.shape, np.nan, np.float32)
    r[y0:y1] = (band - g) / np.maximum(g, 1e-6)
    gate_row = np.where(np.arange(m.shape[1], dtype=np.float32) < m.shape[1] // 2,
                        np.float32(gl), np.float32(gr))
    over = (r > gate_row).astype(np.float32)
    mask = (cv2.filter2D(over, -1, np.ones((1, dg.HOLD), np.float32))
            >= dg.HOLD).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((dg.V_HOLD, 1), np.uint8))
    ncc, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for c in range(1, ncc):
        x0, yy0, w, h = (int(v) for v in stats[c, :4])
        touchL, touchR = x0 <= 2, x0 + w - 1 >= 1277
        if not (touchL or touchR) or h - 1 < dg.MIN_SPAN:
            continue
        sel = lab[yy0:yy0 + h, x0:x0 + w] == c
        grid = np.broadcast_to(np.arange(x0, x0 + w, dtype=np.float32), (h, w))
        lcol = cv2.reduce(np.where(sel & (grid < 640), grid, np.float32(-1.0)),
                          1, cv2.REDUCE_MAX).reshape(-1)
        rows = np.nonzero(lcol >= 0)[0]
        innerL = dict(zip((rows + yy0).tolist(), lcol[rows].astype(np.int32).tolist()))
        rcol = cv2.reduce(np.where(sel & (grid >= 640), grid, np.float32(1e9)),
                          1, cv2.REDUCE_MIN).reshape(-1)
        rows = np.nonzero(rcol <= 1279)[0]
        innerR = dict(zip((rows + yy0).tolist(), rcol[rows].astype(np.int32).tolist()))
        out.append((yy0, yy0 + h - 1, innerL, innerR, touchL, touchR))
    return out


def select(blocks, m, side, gl, gr):
    tkey = 4 if side == "L" else 5
    gate = gl if side == "L" else gr
    for _, _, il, ir, _, _ in sorted((b for b in blocks if b[tkey]),
                                     key=lambda b: b[1] - b[0], reverse=True):
        inner = il if side == "L" else ir
        if not inner:
            continue
        f = dg._fit(inner, CAL)
        if f is None:
            break
        if not dg._pass(f, side):
            continue
        if not dg._baseline_ok(m, inner, side, f["y_ref"], gate):
            continue
        return f
    return None


def gold_x_at(r, side, y):
    k = side.lower()
    if r[k + "cls"] == "skip":
        return None
    nx, ny = float(r[k + "_nx"]), float(r[k + "_ny"])
    fx, fy = float(r[k + "_fx"]), float(r[k + "_fy"])
    if abs(ny - fy) < 1:
        return None
    return nx + (y - ny) * (fx - nx) / (fy - ny)


def gold_row(frames, y0, y1, gl, gr, bad=1.0, fit0=None):
    """fit0=平面拟合窗上沿（None=与检测窗同）；检测始终在全带 [y0,y1)。
    fit0>y0 即「可靠行拟合 + 全带检测」——裁切假设的正确形态。"""
    devs = {"L": [], "R": []}
    both = 0
    for r, m in frames:
        f0 = y0 if fit0 is None else fit0
        coef = plane_fit(m[f0:y1], f0)
        h, w = m[y0:y1].shape
        dy = np.broadcast_to((np.arange(h, dtype=np.float32)[:, None] + y0 - CAL.y_h), (h, w))
        dx = np.broadcast_to((np.arange(w, dtype=np.float32)[None, :] - 640.0), (h, w))
        g = (coef[0] * dy + coef[1] * dx + coef[2]).astype(np.float32)
        blocks = blocks_with_field(m, gl, gr, g, y0, y1)
        got = {}
        for side in ("L", "R"):
            f = select(blocks, m, side, gl, gr)
            if f is None:
                continue
            gx = gold_x_at(r, side, f["y_ref"])
            if gx is None:
                continue
            got[side] = f
            devs[side].append(f["lane"] - x_lane_of(int(round(gx)), int(round(f["y_ref"])), CAL))
        both += len(got) == 2
    cells = [f"[{y0},{y1}] {gl:.2f}/{gr:.2f}"]
    for side in ("L", "R"):
        d = devs[side]
        cells += [f"{side}n={len(d):>2}",
                  f"p90={np.percentile(np.abs(d),90):.2f}" if d else "p90=n/a",
                  f"坏={sum(1 for v in d if abs(v)>bad)}"]
    cells += [f"双={both}"]
    return " ".join(cells)


def main():
    frames = load_frames()
    print(f"金标缓存 {len(frames)} 帧\n")
    residual_profile(frames)
    print("\n── 检测窗固定全带 [340,715]，只变平面拟合窗上沿 fit0 ──")
    print("（fit0>340 = 可靠行拟合+全带检测；对照 rowq20 产码 L n=36 p90=0.93 坏4 R n=20 p90=0.49 坏1 双20）")
    for gl, gr in ((0.08, 0.10), (0.10, 0.12), (0.06, 0.08)):
        for fit0 in (340, 390, 420, 450, 480):
            print(gold_row(frames, 340, 715, gl, gr, fit0=fit0))
        print()


if __name__ == "__main__":
    main()
