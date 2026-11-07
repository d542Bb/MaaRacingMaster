# -*- coding: utf-8 -*-
"""T1 端到端：模块声明 save="self" 时，真实 run() 开的 nav 段不产存盘帧。

组合验证 A4（内层段继承）+ A1b（save=self 零存盘）在真实链路上成立：
  NavigationDebugger.open_scope(save="self")  ← 控制器按模块 DEBUG_SAVE 开
    → NavGraph.run → _debug_scope → open_segment("nav", save="self")
      → 喂帧 → 期望：index 有段边界，磁盘无 jpg
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.core.debug import NavigationDebugger  # noqa: E402
from maaracing_master.core.nav_graph import NavGraph, WgcapController  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
SPEEDRUSH_IMAGE = REPO / "maaracing_master/plugins/speedrush/resources/image"
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")


class Capture:
    def __init__(self, frame):
        self.frame = frame

    def frame_with_age(self):
        return (self.frame, 1, time.time_ns(), 0.0)

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
    def __init__(self, capture, debug):
        self.capture = capture
        self.debug = debug
        self.click_mode = "intent"
        self.lifecycle = Lifecycle()

    def bind_tasker(self, tasker, resource):
        tasker.bind(resource, WgcapController(self.capture))


def main() -> None:
    out = Path(tempfile.gettempdir()) / "a0_t1_debug"
    if out.exists():
        for p in sorted(out.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
    out.mkdir(parents=True, exist_ok=True)

    bgr = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    dbg = NavigationDebugger(out)
    dbg.enabled = True
    dbg.begin_run("t1_test")

    ctx = Ctx(Capture(rgb), dbg)
    graph = NavGraph(ctx)
    graph.add_plugin(SPEEDRUSH_PIPELINE, SPEEDRUSH_IMAGE)
    assert graph.load()

    # 控制器按模块 DEBUG_SAVE="self" 开栈底 scope（契约 I7）
    scope = dbg.open_scope("speedrush", "speedrush", save="self")
    ok = graph.run("speedrush.驾驶页锚点", "speedrush.驾驶页锚点")
    dbg.close_segment(scope, "completed")
    dbg._wait_drain(3.0)

    jpgs = list(out.rglob("*.jpg"))
    index = [json.loads(x) for x in (out / "index.jsonl").read_text(
        encoding="utf-8").splitlines()]
    nav_segs = [x for x in index if x.get("owner") == "nav"]

    print(f"run 结果: {ok}")
    print(f"nav 段事件: {len(nav_segs)} 条")
    print(f"落盘 jpg: {len(jpgs)} 个")
    print(f"索引段: {[(x['type'], x.get('owner'), x.get('event')) for x in index[:6]]}")

    passed = ok and not jpgs and nav_segs
    print("\nVERDICT:", "PASS（nav 段只留边界、零存盘帧）" if passed else "FAIL")
    dbg.end_run()
    graph.shutdown()


if __name__ == "__main__":
    main()
