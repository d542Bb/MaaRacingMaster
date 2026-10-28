# -*- coding: utf-8 -*-
"""现成车道模型首眼测试（C 类，只读回放帧）：把预训练车道网络的输出叠回游戏帧。

只回答一个问题：这个在真实道路数据上训练的模型，在《巅峰极速》画面上认不认得车道。
不评价精度、不做调参、不进任何生产链。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_pretrain/probe_lane_first_look.py \
        --video video_20261021_160547_p1 --fids 400 600 900
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT = DATA / "pretrain_look"
W_MODEL = ROOT / "tools/experiments/speedrush_pretrain/weights/lanenet_multihead.onnx"


def run(sess, frame_bgr):
    h_in, w_in = 384, 1024
    rgb = cv2.cvtColor(cv2.resize(frame_bgr, (w_in, h_in)), cv2.COLOR_BGR2RGB)
    x = rgb.astype(np.float32) / 255.0
    x = (x - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
    t0 = time.perf_counter()
    out = sess.run(None, {sess.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})
    ms = (time.perf_counter() - t0) * 1e3
    return out, ms, (w_in, h_in)


def draw(frame, out, in_hw):
    vis = frame.copy()
    geo, conf = out[0][0], 1.0 / (1.0 + np.exp(-np.asarray(out[3][0])))
    fh, fw = frame.shape[:2]
    n_in = np.zeros(geo.shape[0], int)
    for i in range(geo.shape[0]):
        col = (0, 0, 255) if conf[i] > 0.5 else (120, 120, 120)
        pts = []
        for u, v in geo[i]:
            if not (0.0 <= u <= 1.0 and 0.35 <= v <= 1.0):   # 越界样点不画（负 v 会绕到天上）
                continue
            pts.append((int(u * fw), int(v * fh)))
        for p in pts:
            cv2.circle(vis, p, 2, col, -1)
        n_in[i] = len(pts)
        if len(pts) > 1:
            cv2.polylines(vis, [np.array(pts)], False, col, 2)
        cv2.putText(vis, f"L{i} p={conf[i]:.2f} n={n_in[i]}", (pts[0] if pts else (10, 40)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    return vis, conf, n_in


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="video_20261021_160547_p1")
    ap.add_argument("--fids", type=int, nargs="+", default=[400, 600, 900])
    ap.add_argument("--providers", nargs="+", default=["DmlExecutionProvider", "CPUExecutionProvider"])
    args = ap.parse_args()
    avail = [p for p in args.providers if p in ort.get_available_providers()]
    sess = ort.InferenceSession(str(W_MODEL), providers=avail)
    out_dir = OUT / args.video
    out_dir.mkdir(parents=True, exist_ok=True)
    for fid in args.fids:
        p = DEMOS / args.video / f"{fid:05d}.jpg"
        frame = cv2.imread(str(p))
        out, ms, _ = run(sess, frame)
        vis, conf, n_in = draw(frame, out, None)
        cv2.rectangle(vis, (0, 0), (frame.shape[1], 26), (0, 0, 0), -1)
        cv2.putText(vis, f"pretrained lane net (CULane/TuSimple)  {ms:.0f}ms  "
                + " ".join(f"L{i}:p={conf[i]:.2f},n={n_in[i]}" for i in range(4)), (6, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imwrite(str(out_dir / f"lane_{fid:06d}.jpg"), vis)
        print(f"fid {fid}: {ms:.0f}ms prob={[round(float(c),2) for c in conf]} "
              f"in_range_pts={[int(n) for n in n_in]}")
    print("->", out_dir)


if __name__ == "__main__":
    main()
