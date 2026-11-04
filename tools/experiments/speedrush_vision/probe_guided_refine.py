# -*- coding: utf-8 -*-
"""家族 6 探针：跨模态引导细化——编码器不动，全分辨率 RGB 补边界亚 token 精度。

机制（RESOLUTION_MECHANISMS 家族 6）：@336 折叠输出的视差图（resize 回 720×1280）
作为输入 p，原帧 RGB 作引导 I，跑经典 guided filter（He et al. 2010，颜色引导
形态）：编码器输入、上下文、归一化锚一字未改；细化成本对像素线性。

**零新依赖**：guided filter 只需要 box filter——用核心 `cv2.boxFilter` 实现，
不引 opencv-contrib（本机 OpenCV 5.0.0 无 ximgproc，已实测）。

已知失效模式 texture copying（本域三类污染物=白虚线/横向标线/反光团恰是 RGB
强边）：渲染第三列给出 |Δlog d| 热图，滤波把改动推到了哪里一目了然——污染物
区域出现新台阶即可视判定 texture copying 代价（金标量化在后置评分 todo）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_guided_refine.py \
        [--frames 000906,000260,001340] [--radius 8] [--eps 0.01]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402 —— OUT 数据目录真源

from gold_score import gold_x_at  # noqa: E402

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DEFAULT_SHORT, infer_map, load_session)

H, W = 720, 1280


def guided_filter(I: np.ndarray, p: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """颜色引导 guided filter（He et al. 2010 §3.2）。I:(H,W,3) float32 0~1，
    p:(H,W) float32；返回 q:(H,W)。批量化 3×3 求解，无逐像素 Python 循环。"""
    k = (2 * radius + 1, 2 * radius + 1)
    kw = dict(ddepth=-1, ksize=k, borderType=cv2.BORDER_REFLECT101)

    def box(x: np.ndarray) -> np.ndarray:
        return cv2.boxFilter(x, **kw)

    mean_I = box(I)                                   # (H,W,3)
    mean_p = box(p)[..., None]                        # (H,W,1)
    II = (I[..., :, None] * I[..., None, :])          # (H,W,3,3) 外积
    Ip = I * p[..., None]                             # (H,W,3)
    corr_II = np.empty(II.shape, np.float32)
    for i in range(3):
        for j in range(3):
            corr_II[..., i, j] = box(II[..., i, j])   # OpenCV5 boxFilter 不收 4 维
    cov_II = corr_II - mean_I[..., :, None] * mean_I[..., None, :] \
        + eps * np.eye(3, dtype=np.float32)           # (H,W,3,3)
    cov_Ip = box(Ip) - mean_I * mean_p                # (H,W,3)
    a = np.linalg.solve(cov_II, cov_Ip[..., None])[..., 0]   # (H,W,3)
    b = mean_p[..., 0] - (a * mean_I).sum(-1)
    q = (box(a) * I).sum(-1) + box(b)
    return q


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="000906,000260,001340")
    ap.add_argument("--radius", type=int, default=8)
    ap.add_argument("--eps", type=float, default=0.01)
    args = ap.parse_args()

    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}
    sess = load_session(DEPTH_MODEL_FILE)
    ts = []
    for stem in args.frames.split(","):
        r = labels.get(stem)
        if r is None:
            print(f"{stem}: 不在金标集，跳过"); continue
        p = Path(r["path"])
        if not p.exists():
            print(f"{stem}: 帧不存在 {p}"); continue
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        m = infer_map(sess, rgb, DEFAULT_SHORT)

        t0 = time.perf_counter()
        I = rgb.astype(np.float32) / 255.0
        scale = max(float(m.max()), 1e-6)
        q = guided_filter(I, m / scale, args.radius, args.eps) * scale
        dt = (time.perf_counter() - t0) * 1e3
        ts.append(dt)

        band = np.s_[340:715]
        logs = np.log(np.clip(np.concatenate(
            [m[band].ravel(), q[band].ravel()]), 1e-3, None))
        lo, hi = np.percentile(logs, [2, 98])

        def color(x):
            return cv2.applyColorMap(
                (np.clip((np.log(np.clip(x, 1e-3, None)) - lo) / max(hi - lo, 1e-6),
                         0, 1) * 255).astype(np.uint8), cv2.COLORMAP_JET)

        dlog = np.log(np.clip(q, 1e-3, None)) - np.log(np.clip(m, 1e-3, None))
        amp = np.clip(np.abs(dlog) / 0.35, 0, 1)   # |Δlog|=0.35（≈±42%）顶格
        heat = cv2.applyColorMap((amp * 255).astype(np.uint8), cv2.COLORMAP_HOT)

        game = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
        for side, col in (("l", (0, 255, 0)), ("r", (255, 0, 255))):
            if r[side + "cls"] == "skip":
                continue
            for y in range(300, 716):
                x = gold_x_at(r, side, y)
                if x is not None and 0 <= x < W:
                    cv2.circle(game, (int(round(x)), y), 1, col, -1)

        g = np.vstack([np.hstack([game, color(m), color(q)]),
                       np.hstack([np.zeros_like(game), color(m), heat])])
        cv2.putText(g, "game (green=L gold, magenta=R gold)", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "raw @336 (upsampled)", (8 + W, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, f"guided refine r={args.radius} eps={args.eps}", (8 + 2 * W, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "raw @336", (8, H + 22), cv2.FONT_HERSHEY_SIMPLEX, .6,
                    (255, 255, 255), 2)
        cv2.putText(g, "|dlog| heat (>42% white)", (8 + 2 * W, H + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        out = pd.OUT / f"guided_cmp_{stem}.jpg"
        cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"{stem}: 路带 mean|dlog|={np.abs(dlog[band]).mean():.4f}  "
              f"filter {dt:.1f}ms  → {out}")
    if ts:
        ts = sorted(ts)
        print(f"guided filter: p50={ts[len(ts)//2]:.1f}ms  n={len(ts)}")


if __name__ == "__main__":
    main()
