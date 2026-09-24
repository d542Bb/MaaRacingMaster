# -*- coding: utf-8 -*-
"""单帧「游戏 | fp32×三档 | q4f16×三档」视差对比（维护者目测裁决用，2026-09-24）。

口径：两模型同走折叠会话（free-dim 固定三 dim；折叠数值差 ~3e-6，见折叠验证节，
不影响目测）+ 同一 preprocess + infer_map 回 1280×720。六张视差图共享 log p2~p98
色标——跨格颜色可直接互比。顶行叠加图 = 游戏帧上 45% 透明度叠对应视差。

用法：python tools/experiments/speedrush_vision/probe_fp_vs_q.py [--frame 000906]
      [--res 336,448,518]
输出：depth_review/fp_vs_q_<frame>.jpg
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    infer_map, preprocess)
import probe_depth as pd  # noqa: E402

import os

FP32 = (Path(os.environ.get("APPDATA", "."))
        / "MaaRacingMaster/data/speedrush/depth_review/weights/da2_small.onnx")


def folded(weights: Path, short: int) -> ort.InferenceSession:
    probe = preprocess(np.zeros((720, 1280, 3), np.uint8), short)
    so = ort.SessionOptions()
    for n, v in zip(("batch_size", "height", "width"),
                    (probe.shape[0], probe.shape[2], probe.shape[3])):
        so.add_free_dimension_override_by_name(n, int(v))
    return ort.InferenceSession(str(weights), sess_options=so,
                                providers=["DmlExecutionProvider"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default="000906")
    ap.add_argument("--res", default="336,448,518")
    args = ap.parse_args()
    if not FP32.exists():
        print("fp32 权重不在（depth_review/weights/da2_small.onnx）"); return
    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}
    p = Path(labels[args.frame]["path"]) if args.frame in labels else None
    if p is None or not p.exists():
        print(f"帧 {args.frame} 不在金标集/不存在"); return
    rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
    res = [int(s) for s in args.res.split(",")]
    maps = {}
    for tag, weights in (("fp32", FP32), ("q4f16", DEPTH_MODEL_FILE)):
        for short in res:
            maps[(tag, short)] = infer_map(folded(weights, short), rgb, short)
    logs = np.log(np.clip(np.concatenate(
        [m[340:715].ravel() for m in maps.values()]), 1e-3, None))
    lo, hi = np.percentile(logs, [2, 98])

    def color(m: np.ndarray) -> np.ndarray:
        return cv2.applyColorMap(
            (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6), 0, 1)
             * 255).astype(np.uint8), cv2.COLORMAP_JET)

    def overlay(m: np.ndarray) -> np.ndarray:
        return cv2.addWeighted(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), 0.55,
                               color(m), 0.45, 0)

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    row0 = np.hstack([bgr, overlay(maps[("fp32", res[-1])]),
                      overlay(maps[("q4f16", res[0])])])
    rows = [row0]
    for tag in ("fp32", "q4f16"):
        rows.append(np.hstack([color(maps[(tag, s)]) for s in res]))
    g = np.vstack(rows)
    labels_row0 = ("game", f"game + fp32@{res[-1]} overlay", f"game + q4f16@{res[0]} overlay")
    for j, t in enumerate(labels_row0):
        cv2.putText(g, t, (8 + j * 1280, 20), cv2.FONT_HERSHEY_SIMPLEX, .6,
                    (255, 255, 255), 2)
    for r, tag in ((1, "fp32"), (2, "q4f16")):
        for j, s in enumerate(res):
            cv2.putText(g, f"{tag} @{s}", (8 + j * 1280, r * 720 + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
    out = pd.OUT / f"fp_vs_q_{args.frame}.jpg"
    cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"已出：{out}（共享 log 色标 p2={lo:.2f} p98={hi:.2f}）")


if __name__ == "__main__":
    main()
