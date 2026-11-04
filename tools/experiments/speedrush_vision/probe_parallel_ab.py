# -*- coding: utf-8 -*-
"""scan∥grid 串行 vs 并行 背靠背 A/B（2026-10-09，只读，未改产线）。

问：observe_debug 的并行接线（depth_geo 2026-10-09）相对串行基线，
同机同帧同 repeat 下端到端省多少？空载探针的 1.52~1.60× 是否在
真实管线（含 infer_points/证据包组装）中复现？

串行基线 = 手工串行 infer_points → reading → grid(coef 共享)，与 HEAD
observe_debug 的计算内容逐位一致（并行版 grid 自拟合平面，同函数同输入
确定性同结果，已由 441 帧等价确认）。
并行 = DepthRoadObserver.observe_debug（产线函数本体）。

两路都含 infer_points（同一段），配对差 = 后处理串行 vs 并行的差。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_parallel_ab.py \
        --frames 10 --stride 100 --rounds 3
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DepthRoadObserver, drivable_grid_from_points, infer_points, load_session,
    reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402


def demo_frames(limit: int, stride: int) -> list[Path]:
    root = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
            / "speedrush" / "demos")
    out: list[Path] = []
    for d in sorted(x for x in root.glob("*") if (x / "frames").is_dir()):
        out.extend(sorted((d / "frames").glob("*.jpg"))[::stride][:limit])
    return out[:limit]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))] if xs else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--stride", type=int, default=100)
    ap.add_argument("--rounds", type=int, default=3, help="A/B 交替轮数")
    a = ap.parse_args()

    frames = demo_frames(a.frames, a.stride)
    if not frames:
        print("无 demo 帧可用")
        return 2
    rgbs = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in frames]
    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    obs = DepthRoadObserver(sess, cal)
    ego = obs._ego_mask

    # 预热（DML 编译 + 线程池），不入账
    obs.observe_debug(rgbs[0])

    ser: list[float] = []
    par: list[float] = []
    for r in range(a.rounds):
        for rgb in rgbs:
            # 串行（HEAD 基线口径：coef 共享，grid 不重复拟合）
            t0 = time.perf_counter()
            pts, fx, fy, _ = infer_points(sess, rgb, with_evidence=True)
            rd = reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
            drivable_grid_from_points(pts, fx, fy, ego, None, coef=rd.coef)
            ser.append((time.perf_counter() - t0) * 1e3)
            # 并行（产线 observe_debug 本体）
            t0 = time.perf_counter()
            obs.observe_debug(rgb)
            par.append((time.perf_counter() - t0) * 1e3)
        print(f"轮 {r + 1}/{a.rounds} 完成")

    n = len(ser)
    diffs = [s - p for s, p in zip(ser, par)]
    faster = sum(1 for d in diffs if d > 0)
    print(f"\n=== 串行 vs 并行（n={n}，{a.rounds} 轮交替）===")
    for label, xs in (("串行（HEAD 口径）", ser), ("并行（observe_debug）", par)):
        print(f"  {label:22s} p50={pct(xs, 50):7.2f}  p95={pct(xs, 95):7.2f}  "
              f"mean={sum(xs) / n:7.2f}")
    print(f"  配对差(串行−并行) p50={pct(diffs, 50):7.2f}  mean={sum(diffs) / n:7.2f}  "
          f"更快帧占比 {faster / n * 100:.0f}%")
    s50, p50 = pct(ser, 50), pct(par, 50)
    print(f"  后处理段加速比 ≈ {s50 / p50:.2f}x（含 infer_points 公共段，"
          f"真实后处理加速比高于此值）")
    print("  注：空载口径；与游戏并发的实机收益须实机复核。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
