# -*- coding: utf-8 -*-
"""A5 端到端：用真实 maafw 跑 `NavGraph.probe()`（不是桩）。

验证实现而不只是逻辑：
  1. 驾驶帧 → HIT，商店帧 → MISS（识别走真实 TemplateRecognizer）
  2. `stopping=True` → NOT_READY，且没有任务被提交
  3. 重复调用不残留记录（结果按 id 取，返回前清掉）

帧源与 A0 相同：`CustomController` 返回指定录制帧，`ctx.capture` 读同一张。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.core.nav_graph import NavGraph, ProbeOutcome, WgcapController  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
SPEEDRUSH_IMAGE = REPO / "maaracing_master/plugins/speedrush/resources/image"
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")


class Capture:
    """与生产 CaptureAdapter 同形的帧源（返回 RGB）。"""

    def __init__(self, frame: np.ndarray):
        self.frame = frame
        self.fid = 100

    def frame_with_age(self):
        return (self.frame, self.fid, time.time_ns(), 0.0)

    def screenshot(self):
        return self.frame


class Lifecycle:
    running = True

    def request_stop(self):
        self.running = False

    def sleep(self, seconds):
        time.sleep(seconds)
        return True


class Ctx:
    def __init__(self, capture: Capture):
        self.capture = capture
        self.click_mode = "intent"
        self.lifecycle = Lifecycle()

    def bind_tasker(self, tasker, resource):
        tasker.bind(resource, WgcapController(self.capture))


def build(frame: np.ndarray) -> tuple[NavGraph, Ctx]:
    ctx = Ctx(Capture(frame))
    graph = NavGraph(ctx)
    graph.add_plugin(SPEEDRUSH_PIPELINE, SPEEDRUSH_IMAGE)
    return graph, ctx


def set_frame(ctx: Ctx, frame: np.ndarray) -> None:
    ctx.capture.frame = frame


def main() -> None:
    drive = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    store = cv2.imread(str(NAV / "000894.jpg"), cv2.IMREAD_COLOR)
    banner = cv2.imread(str(NAV / "001412.jpg"), cv2.IMREAD_COLOR)
    drive = cv2.cvtColor(drive, cv2.COLOR_BGR2RGB)
    store = cv2.cvtColor(store, cv2.COLOR_BGR2RGB)
    banner = cv2.cvtColor(banner, cv2.COLOR_BGR2RGB)

    graph, ctx = build(drive)
    print("[load]", graph.load())

    t0 = time.monotonic()
    r1 = graph.probe("speedrush.驾驶页锚点")
    d1 = time.monotonic() - t0
    print(f"驾驶帧 / 驾驶页锚点 -> {r1.value}  ({d1*1000:.0f}ms)")

    set_frame(ctx, store)
    t0 = time.monotonic()
    r2 = graph.probe("speedrush.驾驶页锚点")
    d2 = time.monotonic() - t0
    print(f"商店帧 / 驾驶页锚点 -> {r2.value}  ({d2*1000:.0f}ms)")

    set_frame(ctx, banner)
    r3 = graph.probe("speedrush.商店横幅锚点")
    print(f"横幅帧 / 商店横幅锚点 -> {r3.value}")

    # 连续调用：记录不残留
    for _ in range(3):
        graph.probe("speedrush.驾驶页锚点")
    print(f"连续 3 次后残留记录数 = {len(graph._probe_records)}")

    # 入口门控：用一个 stopping=True 的包装验「不提交」
    real = graph._tasker

    class _Stopping:
        stopping = True

        def __init__(self, inner):
            self.inner = inner
            self.post_calls = 0

        def post_task(self, *a, **k):
            self.post_calls += 1
            return self.inner.post_task(*a, **k)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    wrapper = _Stopping(real)
    graph._tasker = wrapper
    r4 = graph.probe("speedrush.驾驶页锚点")
    print(f"stopping=True 时 -> {r4.value}，post_task 调用 {wrapper.post_calls} 次，"
          f"残留记录 = {len(graph._probe_records)}")
    graph._tasker = real

    ok = (r1 is ProbeOutcome.HIT and r2 is ProbeOutcome.MISS
          and r3 is ProbeOutcome.HIT and r4 is ProbeOutcome.NOT_READY
          and wrapper.post_calls == 0 and len(graph._probe_records) == 0)
    print("\nVERDICT:", "PASS" if ok else "FAIL")
    graph.shutdown()


if __name__ == "__main__":
    main()
