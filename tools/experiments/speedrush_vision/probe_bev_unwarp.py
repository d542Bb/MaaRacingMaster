# -*- coding: utf-8 -*-
"""BEV 逆透视验证：用纯相机几何把画面展开成俯视度量图，看路沿是否变平行直线。

假设（维护者 2026-09-26 批准短期验证）：路是平地 → 每个像素是一条已知角度光线
→ 地面像素反投影到 (横偏 X, 前距 Z) 的俯视格。若展开后路沿变直且平行，则证明
「透视 + 假高度」两个病同源，BEV 才是该换的地基。不依赖模型给绝对深度。

几何：v = y_h + (h·f)/Z，u = vpx + (X·f)/Z（针孔 + 地平面）。
f、h 未知但只影响前向/横向的**缩放**，不影响「是否变直/平行」——故取定值 f=1000、
h=2.0m（追车相机量级），标定里的 y_h、vpx 用真值。

每帧输出三联：原图 | BEV(原图) | BEV(现行 rel 高度图)。
用法：python tools/experiments/speedrush_vision/probe_bev_unwarp.py
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
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

CAL = load_calib()
NPY_DIR = pd.OUT / "npy"
BEV_DIR = pd.OUT / "bev_review"
F, H = 1000.0, 2.0          # 焦距(px)/相机高(m)：只定缩放，不定直线性
Z_NEAR, Z_FAR = 6.0, 110.0  # 前向量程(m)
X_MAX = 9.0                 # 横向半幅(m)
W_BEV, H_BEV = 480, 600


def bev_maps():
    Zs = np.linspace(Z_NEAR, Z_FAR, H_BEV)[::-1]        # 上=远
    Xs = np.linspace(-X_MAX, X_MAX, W_BEV)
    Xg, Zg = np.meshgrid(Xs, Zs)
    v = CAL.y_h + (H * F) / Zg
    u = CAL.vpx + (Xg * F) / Zg
    return u.astype(np.float32), v.astype(np.float32)


def rel_map(m):
    """现行产码 rowq20 相对高度（全帧，带外=nan）。"""
    band = m[dg.Y0:dg.DIAG_Y1]
    cols = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:1280]
    q20 = np.quantile(band[:, cols], dg.Q_GROUND, axis=1)
    rel = np.full(m.shape, np.nan, np.float32)
    rel[dg.Y0:dg.DIAG_Y1] = (band - q20[:, None]) / np.maximum(q20[:, None], 1e-6)
    return rel


def to_bev(src, um, vm, fill=0):
    return cv2.remap(src, um, vm, cv2.INTER_LINEAR, borderValue=fill)


def colorize_rel(rel, lo=-0.15, hi=0.35):
    v = np.clip((np.nan_to_num(rel, nan=-9) - lo) / (hi - lo), 0, 1)
    img = cv2.applyColorMap((v * 255).astype(np.uint8), cv2.COLORMAP_JET)
    img[~np.isfinite(rel)] = (30, 30, 30)
    return img


def pick_frames():
    labels = list(csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")))
    want = ("000260", "000100", "000914", "000180")
    out = []
    for r in labels:
        stem = Path(r["path"]).stem
        if stem in want:
            p = NPY_DIR / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy"
            if p.exists():
                out.append((r, np.load(p).astype(np.float32)))
    return out


def main():
    BEV_DIR.mkdir(parents=True, exist_ok=True)
    um, vm = bev_maps()
    frames = pick_frames()
    for r, m in frames:
        src = cv2.imread(r["path"])
        rel = rel_map(m)
        bev_rgb = to_bev(src, um, vm)
        bev_rel = to_bev(colorize_rel(rel), um, vm, fill=(30, 30, 30))
        # 拼：原图(缩到同高) | BEV-RGB | BEV-rel
        orig = cv2.resize(src, (int(1280 * H_BEV / 720), H_BEV))
        strip = np.hstack([orig, bev_rgb, bev_rel])
        for y in (0, H_BEV // 3, 2 * H_BEV // 3):
            cv2.line(strip, (orig.shape[1] + 0, y), (strip.shape[1], y), (255, 255, 255), 1)
        cv2.putText(strip, "orig", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 255, 0), 2)
        cv2.putText(strip, "BEV RGB", (orig.shape[1] + 10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 255, 0), 2)
        cv2.putText(strip, "BEV rel(heat)", (orig.shape[1] + bev_rgb.shape[1] + 10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 255, 0), 2)
        out = BEV_DIR / f"bev_{Path(r['path']).stem}_{r['stratum']}.jpg"
        cv2.imwrite(str(out), strip, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print("BEV:", out)


if __name__ == "__main__":
    main()
