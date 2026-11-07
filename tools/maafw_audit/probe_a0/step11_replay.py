# -*- coding: utf-8 -*-
"""A6 离线重放：用真实录制帧跑 `probe()`，看效果与主循环占用。

按用户裁决：跳转只影响成功/失败，不追得分率。所以这里不测得分，只如实给出：
  1. 每帧的命中结果与单次耗时（命中序列是否跟画面切换一致）
  2. 一次锚点复查占掉多少个控制拍（主循环 20Hz，复查 0.5s 一次）
  3. 与 `run()` 路径的耗时对比（同帧、同节点）

回放的是会话 20261106_185832 的 nav 帧：880~900 覆盖 p1→p2 的商店页→驾驶页切换，
1405~1425 覆盖第二场的进商店横幅。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.core.nav_graph import (  # noqa: E402
    NavGraph,
    ProbeOutcome,
    WgcapController,
)

REPO = Path(__file__).resolve().parents[3]
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
SPEEDRUSH_IMAGE = REPO / "maaracing_master/plugins/speedrush/resources/image"
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")

TICK_S = 0.05          # 驾驶主循环 20Hz
ANCHOR_S = 0.5         # 锚点复查间隔（DRIVE_ANCHOR_CHECK_S）


class Capture:
    def __init__(self, frame: np.ndarray):
        self.frame = frame
        self.fid = 0

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
    def __init__(self, capture):
        self.capture = capture
        self.click_mode = "intent"
        self.lifecycle = Lifecycle()

    def bind_tasker(self, tasker, resource):
        tasker.bind(resource, WgcapController(self.capture))


def build(frame: np.ndarray):
    ctx = Ctx(Capture(frame))
    graph = NavGraph(ctx)
    graph.add_plugin(SPEEDRUSH_PIPELINE, SPEEDRUSH_IMAGE)
    return graph, ctx


def load_rows(lo: int, hi: int) -> list[dict]:
    rows = [json.loads(line) for line in (NAV / "frames.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if lo <= r["seq"] <= hi and (NAV / r["file"]).is_file()]


def replay(graph, ctx, rows, node: str, label: str) -> dict:
    """逐帧回放：每帧做一次 probe，模拟「锚点复查正好落在这帧上」。"""
    hits: list[int] = []
    times: list[float] = []
    for r in rows:
        bgr = cv2.imread(str(NAV / r["file"]), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        ctx.capture.frame = rgb
        ctx.capture.fid = r["seq"]
        t0 = time.monotonic()
        out = graph.probe(node)
        times.append(time.monotonic() - t0)
        if out is ProbeOutcome.HIT:
            hits.append(r["seq"])

    times_sorted = sorted(times)
    med = times_sorted[len(times_sorted) // 2]
    print(f"\n=== {label}（{len(rows)} 帧）===")
    print(f"  命中帧: {hits[:12]}{' …' if len(hits) > 12 else ''}（共 {len(hits)}）")
    print(f"  单次耗时: 中位 {med*1000:.0f}ms  最小 {times_sorted[0]*1000:.0f}ms  "
          f"最大 {times_sorted[-1]*1000:.0f}ms")
    print(f"  占控制拍: {med/TICK_S:.1f} 拍/次（主循环 {1/TICK_S:.0f}Hz，"
          f"复查间隔 {ANCHOR_S}s = {ANCHOR_S/TICK_S:.0f} 拍）")
    print(f"  复查周期被占: {med/ANCHOR_S*100:.0f}%")
    return {"hits": hits, "med": med, "times": times}


def main() -> None:
    first = load_rows(880, 900)
    frame0 = cv2.cvtColor(cv2.imread(str(NAV / first[0]["file"]), cv2.IMREAD_COLOR),
                          cv2.COLOR_BGR2RGB)
    graph, ctx = build(frame0)
    assert graph.load()

    # 1) p1→p2 切换段：驾驶页锚点
    seg1 = replay(graph, ctx, first, "speedrush.驾驶页锚点", "880~900 驾驶页锚点")

    # 2) 同一段用 run() 对照耗时（run 会开 nav 段，正是要替换的路径）
    run_times = []
    for r in first:
        rgb = cv2.cvtColor(cv2.imread(str(NAV / r["file"]), cv2.IMREAD_COLOR),
                           cv2.COLOR_BGR2RGB)
        ctx.capture.frame = rgb
        ctx.capture.fid = r["seq"]
        t0 = time.monotonic()
        graph.run("speedrush.驾驶页锚点", "speedrush.驾驶页锚点")
        run_times.append(time.monotonic() - t0)
    run_times.sort()
    print(f"\n=== 同段 run() 对照 ===\n"
          f"  中位 {run_times[len(run_times)//2]*1000:.0f}ms  "
          f"最大 {run_times[-1]*1000:.0f}ms")

    # 3) 第二场：进商店横幅
    second = load_rows(1405, 1425)
    seg2 = replay(graph, ctx, second, "speedrush.商店横幅锚点", "1405~1425 商店横幅锚点")

    print("\n=== 小结 ===")
    print(f"  驾驶页锚点命中起点: {seg1['hits'][0] if seg1['hits'] else '无'}"
          f"（画面在 895 帧切到驾驶页）")
    print(f"  商店横幅命中帧: {seg2['hits']}")
    print(f"  probe 中位 {seg1['med']*1000:.0f}ms，占 {seg1['med']/TICK_S:.1f} 拍"
          f"（0.5s 复查周期 = {ANCHOR_S/TICK_S:.0f} 拍）")
    print("  注：run() 对照偏慢是因为它要开调试段并排空（离线无 UI 时更明显），"
          "不代表实机；probe 不开段，这也是本契约要的语义差别。")
    graph.shutdown()


if __name__ == "__main__":
    main()
