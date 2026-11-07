# -*- coding: utf-8 -*-
"""导航段存盘策略的端到端对照：继承 vs 显式覆盖。

验证两件事在**真实链路**上成立（不是桩）：
  A. 不传 `save` → 内层 nav 段继承栈底 scope 的 `self`（零存盘帧，T1 行为不变）
  B. 传 `save="on"` → 该段显式落图，且索引里 `payload.save` 为 `on`

另打印 nav 段的 `save_fps`，确认统一取 `NAV_SAVE_FPS`（3Hz）。
段寿命只有一次识别（十几毫秒），落盘张数受采样节拍影响，只作参考不作判据。
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
from maaracing_master.core import nav_graph as ng  # noqa: E402
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


def _fresh(out: Path) -> None:
    if out.exists():
        for p in sorted(out.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
    out.mkdir(parents=True, exist_ok=True)


def _run_case(tag: str, save: str | None) -> dict:
    out = Path(tempfile.gettempdir()) / f"a0_nav_save_{tag}"
    _fresh(out)

    bgr = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    dbg = NavigationDebugger(out)
    dbg.enabled = True
    dbg.begin_run(f"save_{tag}")

    ctx = Ctx(Capture(rgb), dbg)
    graph = NavGraph(ctx)
    graph.add_plugin(SPEEDRUSH_PIPELINE, SPEEDRUSH_IMAGE)
    assert graph.load()

    scope = dbg.open_scope("speedrush", "speedrush", save="self")
    kwargs = {} if save is None else {"save": save}
    ok = graph.run("speedrush.驾驶页锚点", "speedrush.驾驶页锚点", **kwargs)
    dbg.close_segment(scope, "completed")
    dbg._wait_drain(3.0)

    jpgs = list(out.rglob("*.jpg"))
    index = [json.loads(x) for x in (out / "index.jsonl").read_text(
        encoding="utf-8").splitlines()]
    nav_start = [x for x in index
                 if x.get("owner") == "nav" and x.get("type") == "segment_start"]
    seg_save = [x["payload"]["save"] for x in nav_start if x.get("payload")]
    dbg.end_run()
    graph.shutdown()
    return {"ok": ok, "jpgs": len(jpgs), "save": seg_save, "fps": ng.NAV_SAVE_FPS}


def main() -> None:
    inherited = _run_case("inherit", None)
    explicit = _run_case("on", "on")

    print(f"NAV_SAVE_FPS = {inherited['fps']}")
    print(f"A 继承（不传 save）: run={inherited['ok']} 段 save={inherited['save']} "
          f"落盘 {inherited['jpgs']} 张")
    print(f"B 显式 save=on   : run={explicit['ok']} 段 save={explicit['save']} "
          f"落盘 {explicit['jpgs']} 张")

    a_ok = inherited["save"] == ["self"] and inherited["jpgs"] == 0
    b_ok = explicit["save"] == ["on"]
    print("\nVERDICT:", "PASS" if (a_ok and b_ok) else "FAIL",
          "（A 必须零帧；B 只看段声明，张数受段寿命影响）")


if __name__ == "__main__":
    main()
