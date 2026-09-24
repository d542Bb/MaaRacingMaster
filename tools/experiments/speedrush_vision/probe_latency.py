# -*- coding: utf-8 -*-
"""控制拍全链路时延分解（常驻脚本，2026-09-24 性能分析）。

存在的理由就是时延悬案的直接教训：**一次性 bench 的口径必然断链**，全链分解
从此固化在这里，任何接线改动后用同一把尺复测。

口径（全部生产实现，零逻辑复制——内部热点用 cProfile 归因，不改产码）：
- 帧源：demos/*/frames/*.jpg 跨会话等距抽样（--sessions N --stride S）。
- 阶段：preprocess（1280×720→短边 DEFAULT_SHORT）→ sess.run（**折叠会话**，
  生产 load_session）→ resize 回原帧 → reading_from_map（后处理全链）；
  另测 detect_boundary（黄线层，骨架化后仍每拍跑的记账成本）。
- 会话外环节（capture 20Hz、tracker/agg/engine/planner ≈3ms）不在此测：
  前者由 9-22 实机 trace 的 dt p50=0.050s 锚定，后者由实机 trace 的
  控制链 P50 − dgeo_ms 差值锚定（见 README「实机复核」节）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_latency.py stages [--sessions 3] [--stride 37]
    python tools/experiments/speedrush_vision/probe_latency.py deep   [--frames 8]
"""
from __future__ import annotations

import argparse
import cProfile
import glob
import pstats
import statistics as st
import sys
import time
from io import StringIO
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.boundary import detect_boundary  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DEFAULT_SHORT, load_session, preprocess, reading_from_map,
    DepthRoadObserver)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402


def _p(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def _demo_frames(sessions: int, stride: int) -> list[Path]:
    import os
    demo_root = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush" / "demos"
    out: list[Path] = []
    dirs = sorted(d for d in demo_root.glob("*") if (d / "frames").is_dir())
    for d in dirs[:sessions]:
        fs = sorted((d / "frames").glob("*.jpg"))[::stride]
        out.extend(fs[:20])
    return out


def cmd_stages(args: argparse.Namespace) -> None:
    frames = _demo_frames(args.sessions, args.stride)
    if not frames:
        print("无 demo 帧可用（demos/*/frames/*.jpg）")
        return
    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    ego = DepthRoadObserver._load_ego_mask()
    stages: dict[str, list[float]] = {k: [] for k in
                                      ("preprocess", "infer", "resize_back",
                                       "postprocess", "boundary")}
    sides: list[int] = []
    warm = cv2.cvtColor(cv2.imread(str(frames[0])), cv2.COLOR_BGR2RGB)
    xw = preprocess(warm, DEFAULT_SHORT)
    sess.run(None, {"pixel_values": xw})  # 内核预热一次，不入账
    for i, p in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter(); x = preprocess(rgb, DEFAULT_SHORT)
        stages["preprocess"].append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter(); raw = sess.run(None, {"pixel_values": x})[0][0]
        stages["infer"].append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter()
        m = cv2.resize(np.nan_to_num(raw.astype(np.float32)),
                       (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
        stages["resize_back"].append((time.perf_counter() - t0) * 1e3)
        t0 = time.perf_counter(); rd = reading_from_map(m, cal, ego)
        stages["postprocess"].append((time.perf_counter() - t0) * 1e3)
        sides.append(rd.sides)
        t0 = time.perf_counter(); detect_boundary(rgb)
        stages["boundary"].append((time.perf_counter() - t0) * 1e3)
    print(f"帧数 n={len(frames)}（折叠会话，生产形状 1280×720→@{DEFAULT_SHORT}）")
    print(f"{'阶段':<14}{'p50':>8}{'p95':>8}{'max':>8}")
    for k, xs in stages.items():
        if not xs:
            continue
        print(f"{k:<16}{_p(xs, .5):>7.1f}{_p(xs, .95):>7.1f}{max(xs):>7.1f}")
    import collections
    print("sides 分布:", dict(collections.Counter(sides)))


def cmd_deep(args: argparse.Namespace) -> None:
    """后处理热点归因：cProfile 跑一批真帧的 reading_from_map。"""
    frames = _demo_frames(args.sessions, args.stride)[:args.frames]
    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    ego = DepthRoadObserver._load_ego_mask()
    maps = []
    for p in frames:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        raw = sess.run(None, {"pixel_values": preprocess(rgb, DEFAULT_SHORT)})[0][0]
        maps.append(cv2.resize(np.nan_to_num(raw.astype(np.float32)),
                               (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR))
    pr = cProfile.Profile()
    pr.enable()
    for m in maps:
        reading_from_map(m, cal, ego)
    pr.disable()
    s = StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(18)
    print(s.getvalue())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("stages"); a1.add_argument("--sessions", type=int, default=3)
    a1.add_argument("--stride", type=int, default=37)
    a1.set_defaults(fn=cmd_stages)
    a2 = sub.add_parser("deep"); a2.add_argument("--sessions", type=int, default=3)
    a2.add_argument("--stride", type=int, default=37); a2.add_argument("--frames", type=int, default=8)
    a2.set_defaults(fn=cmd_deep)
    args = ap.parse_args()
    args.fn(args)
