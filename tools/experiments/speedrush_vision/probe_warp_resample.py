# -*- coding: utf-8 -*-
"""家族 4 探针：非均匀重采样（foveated warp）——「保内容、只改采样密度」。

机制（RESOLUTION_MECHANISMS 家族 4）：不动模型与折叠会话，纯 preprocess——把
天空（y<340，检测带起点之上）纵向压缩 2×，路带（y∈[340,720]）拉伸 ~1.45×，
总像素数不变 ⇒ token 数、时延、折叠全不变，但路带的 token 采样密度升 ~1.45×。
与裁切的本质区别：天空仍在画面里（占的像素少），归一化锚的内容不删。

坐标口径：warp 只动 y（分段线性，kink 表）。维护者定界三分段（2026-09-25）：
天空 [0,340] 压 2×（170 out）、远中带 [340,550] 拉 1.90×（400 out，金标线主区）、
车尾近带 [550,720] 压 0.88×（150 out，已过道路低信息）。评分阶段（后续 todo）
按段拟合，本探针先出渲染与量级参照。

渲染：共享 log 色标（惯例同 probe_fp_vs_q：路带 [340,715] 取 p2/p98）。
附带每帧路带与 @448 参照的 log 域平均偏差（「谁更接近高分辨率参照」的量级旁证，
非金标判据）与各档推理耗时。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_warp_resample.py \
        [--frames 000906,000260,001340]
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

YS_KINK = (0, 340, 550, 720)      # 源坐标 kink（段：天空 / 远中带 / 车尾近带）
YO_KINK = (0, 170, 570, 720)      # warp 坐标 kink（压2× / 拉1.90× / 压0.88×）
H, W = 720, 1280


def _warp_inv(y_out: np.ndarray) -> np.ndarray:
    """warp 空间 y_out → 源空间 y_src（dst 行采样自哪一行）。"""
    return np.interp(y_out, YO_KINK, YS_KINK).astype(np.float32)


def _warp_fwd(y_src: np.ndarray) -> np.ndarray:
    """源空间 y_src → warp 空间 y_out。"""
    return np.interp(y_src, YS_KINK, YO_KINK).astype(np.float32)


def _row_maps() -> tuple[np.ndarray, np.ndarray]:
    ys_out = np.arange(H, dtype=np.float32)
    ys_src = np.arange(H, dtype=np.float32)
    map_x = np.tile(np.arange(W, dtype=np.float32), (H, 1))
    map_y_warp = np.tile(_warp_inv(ys_out)[:, None], (1, W))  # 出图→采源
    map_y_unwarp = np.tile(_warp_fwd(ys_src)[:, None], (1, W))  # 出图→采warp
    return map_x, (map_y_warp, map_y_unwarp)


def _folded(short: int):
    from maaracing_master.plugins.speedrush.depth_geo import preprocess
    probe = preprocess(np.zeros((H, W, 3), np.uint8), short)
    so = __import__("onnxruntime").SessionOptions()
    for n, v in zip(("batch_size", "height", "width"),
                    (probe.shape[0], probe.shape[2], probe.shape[3])):
        so.add_free_dimension_override_by_name(n, int(v))
    return so


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="000906,000260,001340")
    args = ap.parse_args()

    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}
    map_x, (my_warp, my_unwarp) = _row_maps()

    import onnxruntime as ort
    from maaracing_master.plugins.speedrush.depth_geo import preprocess
    sess336 = load_session(DEPTH_MODEL_FILE)
    so448 = _folded(448)
    sess448 = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=so448,
                                   providers=["DmlExecutionProvider"])
    times = {"full@336": [], "warp@336": [], "full@448": []}

    for stem in args.frames.split(","):
        r = labels.get(stem)
        if r is None:
            print(f"{stem}: 不在金标集，跳过"); continue
        p = Path(r["path"])
        if not p.exists():
            print(f"{stem}: 帧不存在 {p}"); continue
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        warped = cv2.remap(rgb, map_x, my_warp, cv2.INTER_LINEAR)

        t0 = time.perf_counter()
        m336 = infer_map(sess336, rgb, DEFAULT_SHORT)
        times["full@336"].append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter()
        mw = infer_map(sess336, warped, DEFAULT_SHORT)
        times["warp@336"].append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter()
        m448 = infer_map(sess448, rgb, 448)
        times["full@448"].append((time.perf_counter() - t0) * 1e3)
        mwx = cv2.remap(mw, map_x, my_unwarp, cv2.INTER_LINEAR)  # 反映射回原帧

        band = np.s_[340:715]
        logs = np.log(np.clip(np.concatenate(
            [m[band].ravel() for m in (m336, mwx, m448)]), 1e-3, None))
        lo, hi = np.percentile(logs, [2, 98])

        def color(m):
            return cv2.applyColorMap(
                (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6),
                         0, 1) * 255).astype(np.uint8), cv2.COLORMAP_JET)

        d336 = float(np.abs(np.log(np.clip(m336[band], 1e-3, None))
                            - np.log(np.clip(m448[band], 1e-3, None))).mean())
        dwarp = float(np.abs(np.log(np.clip(mwx[band], 1e-3, None))
                             - np.log(np.clip(m448[band], 1e-3, None))).mean())

        game = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
        for side, col in (("l", (0, 255, 0)), ("r", (255, 0, 255))):
            if r[side + "cls"] == "skip":
                continue
            for y in range(300, 716):
                x = gold_x_at(r, side, y)
                if x is not None and 0 <= x < W:
                    cv2.circle(game, (int(round(x)), y), 1, col, -1)
        for yk in (340, 550):
            cv2.line(game, (0, yk), (W, yk), (200, 200, 200), 1)
        warpv = cv2.cvtColor(warped, cv2.COLOR_RGB2BGR)
        for yk in (170, 570):
            cv2.line(warpv, (0, yk), (W, yk), (200, 200, 200), 1)

        row1 = np.hstack([game, warpv, color(mw)])  # mw=warp 空间原生视差
        row2 = np.hstack([color(m336), color(mwx), color(m448)])
        g = np.vstack([row1, row2])
        cv2.putText(g, "game (green=L gold, magenta=R gold)", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "warp input (model sees)", (8 + W, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "warp disparity (warp space)", (8 + 2 * W, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "full @336 (ref)", (8, H + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "warp @336 (unwarped to game coords)", (8 + W, H + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        cv2.putText(g, "full @448 (ref)", (8 + 2 * W, H + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        out = pd.OUT / f"warp_cmp_{stem}.jpg"
        cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"{stem}: 路带 |log d−log d448| 全帧@336={d336:.3f}  "
              f"warp@336={dwarp:.3f}  → {out}")

    for k, v in times.items():
        v = sorted(v)
        print(f"infer {k}: p50={v[len(v)//2]:.1f}ms  n={len(v)}")


if __name__ == "__main__":
    main()
