# -*- coding: utf-8 -*-
"""深度 observe 各阶段的 CPU 成本：墙钟耗时 vs 实占核数（离线 demo 帧）。

动机：实机 census（probe_runtime_census.py）只能拿到 sidecar **进程级** CPU——
MRA 以管理员权限启动，sidecar 继承 elevated，普通权限进程 OpenThread 一律
``err=5``，psutil 的 ``threads()`` 还会静默返回空列表（不报错，极易误判成逻辑
bug）。所以"这条链到底吃几个核"只能在本进程内直接量：

    核数 = 进程 CPU 时间增量（user+system，含全部线程） ÷ 墙钟

本探针跑的就是产线同一条链（``infer_points`` → ``reading_from_points`` →
``drivable_grid_from_points``），零逻辑复制，改接线后用同一把尺复测。

用法（仓库根，.venv）：
    .venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_observe_cost.py
    ... --frames 12 --repeat 3          # 帧数与每帧重复次数
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import psutil

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


class Meter:
    """进程 CPU 时间 / 墙钟 的成对记账。"""

    def __init__(self) -> None:
        self.p = psutil.Process()
        self.n = psutil.cpu_count(logical=True) or 1

    def run(self, label: str, fn, repeat: int) -> float:
        c0 = sum(self.p.cpu_times()[:2])
        t0 = time.perf_counter()
        for _ in range(repeat):
            fn()
        wall = time.perf_counter() - t0
        cpu = sum(self.p.cpu_times()[:2]) - c0
        print(f"  {label:22} 墙钟 {wall / repeat * 1e3:7.1f}ms  "
              f"CPU {cpu / repeat * 1e3:7.1f}ms  核数 {cpu / wall:5.2f}"
              f"  (占整机 {cpu / wall / self.n * 100:4.1f}%)")
        return wall / repeat * 1e3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=int, default=8, help="取多少帧")
    ap.add_argument("--stride", type=int, default=37, help="跨会话抽样步长")
    ap.add_argument("--repeat", type=int, default=3, help="每帧重复次数")
    a = ap.parse_args()

    frames = demo_frames(a.frames, a.stride)
    if not frames:
        print("无 demo 帧可用")
        return 2
    m = Meter()
    print(f"帧数 n={len(frames)}  每帧重复 {a.repeat}  逻辑核 {m.n}")
    print(f"cv2 线程 {cv2.getNumThreads()}  "
          f"OMP_NUM_THREADS={os.environ.get('OMP_NUM_THREADS')}")

    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    ego = DepthRoadObserver._load_ego_mask()
    rgbs = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in frames]
    # 预热（内核编译 + 缓存），不入账
    pts, fx, fy, _ = infer_points(sess, rgbs[0], with_evidence=True)
    reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)

    print("\n阶段 CPU 成本：")
    for i, rgb in enumerate(rgbs[:2]):
        box: dict = {}
        m.run(f"infer_points 帧{i}", lambda r=rgb: box.update(
            zip(("pts", "fx", "fy"), infer_points(sess, r, with_evidence=True)[:3])),
            a.repeat)
        pts, fx, fy = box["pts"], box["fx"], box["fy"]
        m.run(f"reading_from_points 帧{i}",
              lambda: box.update(rd=reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)),
              a.repeat)
        m.run(f"drivable_grid 帧{i}",
              lambda: drivable_grid_from_points(pts, fx, fy, ego, None,
                                                coef=box["rd"].coef),
              a.repeat)
        m.run(f"observe 全链 帧{i}",
              lambda: _full(sess, rgb, cal, ego), a.repeat)
        print()

    # 对照：关掉 cv2 多线程看后处理是否变慢（判定多线程是否有实际收益）
    print("对照（cv2.setNumThreads(1) 后重测 帧0）：")
    cv2.setNumThreads(1)
    pts, fx, fy, _ = infer_points(sess, rgbs[0], with_evidence=True)
    m.run("reading_from_points 单线程",
          lambda: reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy), a.repeat)
    m.run("drivable_grid 单线程",
          lambda: drivable_grid_from_points(pts, fx, fy, ego, None, coef=None), a.repeat)
    return 0


def _full(sess, rgb, cal, ego):
    pts, fx, fy, _ = infer_points(sess, rgb, with_evidence=True)
    rd = reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
    drivable_grid_from_points(pts, fx, fy, ego, None, coef=rd.coef)
    return rd


if __name__ == "__main__":
    sys.exit(main())
