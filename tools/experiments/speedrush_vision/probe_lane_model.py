# -*- coding: utf-8 -*-
"""轻量车道分割模型试跑（TwinLiteNet 系）：CPU 延迟 + 域差 + 叠图。

**用途**：评估“车道线模型能否给 MoGe 补横向/航向先验”，只做离线实验，不进
控制。权重不随仓库分发；用 ``--model`` 指向本地 ONNX。

**一手来源**（2026-10-24 核查）：
- 官方 TwinLiteNet+ 代码 MIT：https://github.com/chequanghuy/TwinLiteNetPlus
  变体 Nano/Small/Medium/Large 0.03M–1.94M 参数、0.57–17.6 GFLOPs、640×384。
- 原版 ONNX（3.9 GFLOPs、360×640）MIT 推理仓库：
  https://github.com/harrylal/TwinLiteNet-onnxruntime
  本机实测约 0.8 s/帧（CPU ONNX Runtime）；白虚线可分、黄色左缘常漏、
  左侧人行道有假可行驶区。+Nano/Small 的权重在官方 Google Drive，未内置。

**本机实测**（20261024_103358_p1，1280×720→360×640）：
- CPU 约 0.8 s/帧，够离线/2–4 Hz 后台先验，不够 20 Hz 控制；
- 域差：左侧人行道被分进可行驶区，黄色左缘漏检；白色虚线可作横向先验；
- 结论：只借“双头轻量分割 + 车道先验”思路，生产仍需自有游戏帧重训/微调。
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image


def _prob(o: np.ndarray) -> np.ndarray:
    e = np.exp(o - o.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="TwinLiteNet 系 ONNX")
    ap.add_argument("--dir", required=True, help="debug 会话目录")
    ap.add_argument("--idxs", default="20,100,155,300,460")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    run = Path(args.dir)
    out = Path(args.out) if args.out else run / "lane_model"
    out.mkdir(parents=True, exist_ok=True)
    sess = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    _, _, h, w = inp.shape
    print("model", args.model, "input", inp.shape, "providers", sess.get_providers())
    for s in args.idxs.split(","):
        idx = int(s)
        raw = run / "raw" / f"{idx + 1:04d}_raw.jpg"
        img = cv2.imread(str(raw))
        if img is None:
            print("missing", raw)
            continue
        x = cv2.resize(img, (w, h))
        x = cv2.cvtColor(x, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None]
        t = time.perf_counter()
        outs = sess.run(None, {inp.name: x})
        lat = (time.perf_counter() - t) * 1000.0
        by_name = {o.name: v for o, v in zip(sess.get_outputs(), outs)}
        da = _prob(by_name["da"])[0, 1]
        ll = _prob(by_name["ll"])[0, 1]
        lane = cv2.resize((ll > 0.5).astype(np.uint8) * 255, (img.shape[1], img.shape[0]),
                          interpolation=cv2.INTER_NEAREST)
        drive = cv2.resize((da > 0.5).astype(np.uint8) * 255, (img.shape[1], img.shape[0]),
                           interpolation=cv2.INTER_NEAREST)
        vis = img.copy()
        m = drive > 0
        vis[m] = (0.65 * vis[m] + 0.35 * np.array([0, 180, 0])).astype(np.uint8)
        vis[lane > 0] = (0, 0, 255)
        p = out / f"twin_{idx:04d}.jpg"
        Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)).save(p, quality=90)
        print(f"idx {idx:4d} latency {lat:6.1f} ms  lane_px {int((lane > 0).sum()):6d}  "
              f"drive {float((drive > 0).mean()):.3f}  -> {p.name}")
    print("out", out)


if __name__ == "__main__":
    main()
