#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VRAM 压力球：用 N 个 MoGe DML 会话占住显存，可选锯齿 churn。

模拟生产里模拟器/游戏的显存占用与资产流送突发：稳压模式创建 N 个会话轮转
run（各会话独立 arena，~0.6GB/个）；churn 模式每 period 秒销毁一个再新建
（~600MB 级显存反复分配/释放，逼 WDDM 驻留翻转）。打印每步实际显存水位
（nvidia-smi）供校准。

用法：
  python pressure.py --sessions 6 --hold 150          # 稳压 ~3.6GB × 150s
  python pressure.py --sessions 6 --churn 8 --hold 150
"""
from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

REPO = Path(__file__).resolve().parents[3]
DEPTH_ONNX = REPO / "maaracing_master/plugins/speedrush/resources/models/depth/moge2_vits_static_336x598_t1032_q4f16.onnx"
MOGE_W, MOGE_H = 1280, 720


def make_session() -> ort.InferenceSession:
    avail = set(ort.get_available_providers())
    providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                             "CPUExecutionProvider") if p in avail]
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"), (1, MOGE_H, MOGE_W)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(DEPTH_ONNX), sess_options=so, providers=providers)


def vram_used() -> int:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--hold", type=float, default=150.0, help="持续秒")
    ap.add_argument("--churn", type=float, default=0.0,
                    help=">0 时每 N 秒销毁+新建一个会话（锯齿）")
    ap.add_argument("--warm-frames", type=int, default=3, help="每会话暖机 run 次数")
    args = ap.parse_args()

    blob = np.zeros((1, 3, MOGE_H, MOGE_W), np.float32)
    stop = threading.Event()
    sess_pool: list = []

    def build_one(tag: str) -> ort.InferenceSession:
        t0 = time.perf_counter()
        s = make_session()
        for _ in range(args.warm_frames):
            s.run(None, {"image": blob, "num_tokens": np.array(1032, np.int64)}
                  if "num_tokens" in {i.name for i in s.get_inputs()} else {"image": blob})
        print(f"[ball] {tag} 建成 {time.perf_counter()-t0:.1f}s "
              f"vram={vram_used()}MiB", flush=True)
        return s

    print(f"[ball] 起始 vram={vram_used()}MiB", flush=True)
    for k in range(args.sessions):
        sess_pool.append(build_one(f"s{k}"))

    def churn_loop() -> None:
        i = 0
        while not stop.is_set():
            stop.wait(args.churn)
            if stop.is_set():
                return
            t0 = time.perf_counter()
            old = sess_pool[i % len(sess_pool)]
            sess_pool[i % len(sess_pool)] = None
            del old
            new = build_one(f"churn{i}")
            sess_pool[i % len(sess_pool)] = new
            i += 1

    if args.churn > 0:
        threading.Thread(target=churn_loop, daemon=True).start()

    # 轮转 run 保持各会话驻留（DML arena 已分配即占住；run 维持活跃）
    t_end = time.perf_counter() + args.hold
    i = 0
    while time.perf_counter() < t_end:
        s = sess_pool[i % len(sess_pool)]
        if s is not None:
            try:
                s.run(None, {"image": blob, "num_tokens": np.array(1032, np.int64)}
                      if "num_tokens" in {x.name for x in s.get_inputs()}
                      else {"image": blob})
            except Exception as exc:
                print(f"[ball] run 失败: {exc!r}", flush=True)
        if i % 20 == 0:
            print(f"[ball] vram={vram_used()}MiB t={time.perf_counter()-t_end+args.hold:.0f}s",
                  flush=True)
        i += 1
    stop.set()
    print(f"[ball] 结束 vram={vram_used()}MiB", flush=True)


if __name__ == "__main__":
    main()
