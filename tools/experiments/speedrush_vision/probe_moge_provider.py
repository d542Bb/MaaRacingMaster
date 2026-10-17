# -*- coding: utf-8 -*-
"""MoGe 会话 provider 实况 + forward 时延对照（DML vs CPU）。

问题：产线 `load_session` 按 Dml→CUDA→CPU 可用序注册 provider，但 ORT 会
静默把不支持的节点退回 CPU——**注册序 ≠ 实际执行单元**。本探针给两条硬
证据：①会话注册序与 EP 清单；②同权重同输入下 DML 与 CPU-only 的 forward
时延比（产线 trace forward P50 ≈45ms；若 CPU-only 同量级，说明产线其实在
CPU 上跑）。

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_moge_provider.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import moge_post            # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (          # noqa: E402
    MOGE_IN_H, MOGE_IN_W, load_session)
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE      # noqa: E402

print(f"ort {ort.__version__}")
print(f"available = {ort.get_available_providers()}")
print(f"weights   = {DEPTH_MODEL_FILE.name}")

sess = load_session(DEPTH_MODEL_FILE)
print(f"\nload_session 注册序 = {sess.get_providers()}")

rng = np.random.default_rng(0)
rgb = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
blob = moge_post.preprocess(rgb, MOGE_IN_W, MOGE_IN_H)


def _bench(s, n=5):
    s.run(None, {"image": blob})            # 预热（DML 首跑含图编译）
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        s.run(None, {"image": blob})
        ts.append((time.perf_counter() - t) * 1000.0)
    ts.sort()
    return ts[len(ts) // 2], ts[0], ts[-1]


p50, lo, hi = _bench(sess)
print(f"产线会话 forward P50={p50:.1f}ms（min {lo:.1f} / max {hi:.1f}）")

if "DmlExecutionProvider" in sess.get_providers():
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"),
                         (1, MOGE_IN_H, MOGE_IN_W)):
        so.add_free_dimension_override_by_name(name, int(dim))
    cpu = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=so,
                               providers=["CPUExecutionProvider"])
    c50, clo, chi = _bench(cpu, n=3)
    print(f"CPU-only 会话 forward P50={c50:.1f}ms（min {clo:.1f} / max {chi:.1f}）")
    print(f"时延比 CPU/DML = {c50 / max(p50, 1e-6):.1f}× → "
          + ("产线在 GPU(DML) 上跑" if c50 / p50 > 2.0
             else "产线可能落在 CPU（比值过小）"))
else:
    print("未注册 DML（本机不可用）——产线只能在 CPU 上跑")
