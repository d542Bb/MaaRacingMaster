# -*- coding: utf-8 -*-
"""连续帧「游戏 | 视差 | 相对高度」单图条带（维护者指定交付，2026-09-24）。

口径（全部生产件，零实验变体——数据目录模型副本已清退）：
- 推理：plugins/speedrush 的 depth_small_q4f16.onnx，经生产 load_session（折叠）
  + infer_map（@336→回 1280×720），与车上跑的是同一份代码同一份权重。
- 视差面板：log(M) 归一（跨全部帧共享 p2/p98，三帧同色标可直接对比）→ JET。
  log 归一是"画均匀"的正解：视差 ∝ 1/距离，线性色标会把远处挤成一坨蓝、
  近处糊成一片红（此前 fixed scale [0,9] 的饱和形态），等比对数才配人眼。
- 高度面板：h = r/(1+r) ≈ Δd/d（相对同排路面的抬升比例；r 由生产 _rel_and_blocks
  的 q20 基线给出）。蓝=低于路面、白=路面、红=高于路面，共享 ±10% 量程；
  检测带外（地平线上/近场）无定义 → 黑。自车列带按 ego_mask 一并涂黑（挖除区）。

用法：python tools/experiments/speedrush_vision/probe_halo_strip.py [--session p1]
      [--start 906] [--count 3]
输出：depth_review/halo_strip_<start>_<end>.jpg
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DEFAULT_SHORT, DIAG_Y1, Y0, _rel_and_blocks, infer_map, load_session)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

import probe_depth as pd  # noqa: E402

CAL = load_calib()
EGO = None  # 延迟加载（见 main）


def _diverging_lut(n=256):
    """蓝(低)→白(基准)→红(高) 均匀发散色标。"""
    lut = np.zeros((n, 3), np.uint8)
    for i in range(n):
        t = i / (n - 1) * 2 - 1  # -1..1
        if t < 0:
            k = -t
            lut[i] = (255, int(255 * (1 - k) + 255 * k), int(255 * (1 - k)))  # 白→青蓝
            lut[i] = (int(255 - 155 * k), int(255 - 105 * k), 255)            # 白→蓝
        else:
            lut[i] = (255, int(255 - 205 * t), int(255 - 205 * t))            # 白→红
    return lut.reshape(1, n, 3)


def main() -> None:
    global EGO
    from maaracing_master.plugins.speedrush.depth_geo import DepthRoadObserver
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="p1", help="demo 会话名尾缀（p1/p2）")
    ap.add_argument("--start", type=int, default=906)
    ap.add_argument("--count", type=int, default=3)
    args = ap.parse_args()

    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}
    sess = load_session(DEPTH_MODEL_FILE)
    EGO = DepthRoadObserver._load_ego_mask()

    frames = []
    for i in range(args.count):
        stem = f"{args.start + i:06d}"
        hit = next((k for k in labels if k.endswith(stem)), None)
        p = Path(labels[hit]["path"]) if hit else None
        if p is None or not p.exists():
            p = next(iter(sorted((Path(labels[list(labels)[0]]["path"]).parent.parent
                                   / "frames").glob(f"*{stem}.jpg"))), None)
        if p is None:
            print(f"帧 {stem} 不存在，跳过")
            continue
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        m = infer_map(sess, rgb, DEFAULT_SHORT)
        rmap, _blocks = _rel_and_blocks(m, EGO)
        frames.append((stem, rgb, m, rmap))
    if not frames:
        print("无帧可画")
        return

    logs = np.log(np.clip(np.concatenate(
        [f[2][Y0:DIAG_Y1].ravel() for f in frames]), 1e-3, None))
    lo, hi = np.percentile(logs, [2, 98])
    lut_div = _diverging_lut()

    rows = []
    head_h = 26
    for stem, rgb, m, rmap in frames:
        disp = cv2.applyColorMap(
            (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6), 0, 1)
             * 255).astype(np.uint8), cv2.COLORMAP_JET)
        h = np.where(np.isfinite(rmap), rmap / (1 + np.clip(rmap, -0.99, None)), np.nan)
        hv = np.clip(np.nan_to_num(h, nan=0.0), -0.10, 0.10)  # ±10% 共享量程
        idx = ((hv + 0.10) / 0.20 * 255).astype(np.uint8)
        height = lut_div[0][idx].astype(np.uint8)
        height[~np.isfinite(rmap)] = 0                       # 带外=黑
        if EGO is not None:
            height[EGO] = 0                                  # 挖除区=黑
        row = np.hstack([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), disp, height])
        for j, txt in enumerate(("game", f"disparity log-scale p2-p98 [{lo:.2f},{hi:.2f}]",
                                 "height vs road ±10% (black=N/A/ego-cut)")):
            cv2.putText(row, f"{stem} {txt}", (8 + j * 1280, 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        rows.append(row)
    g = np.vstack(rows)
    out = pd.OUT / f"halo_strip_{frames[0][0]}_{frames[-1][0]}.jpg"
    cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"已出：{out}（{g.shape[1]}×{g.shape[0]}，三帧×三联，生产权重+生产读法）")


if __name__ == "__main__":
    main()
