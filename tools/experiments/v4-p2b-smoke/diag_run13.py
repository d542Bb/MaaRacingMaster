# -*- coding: utf-8 -*-
"""P2b 诊断 v13（崩溃二分）：0xC0000005 触发条件最小化。

run12 已证：主流程全绿（post_stop 0s、job.wait 秒回），崩在解释器 teardown/GC。
binding __del__ 直接 MaaTaskerDestroy/MaaControllerDestroy，不等 C++ worker 线程
（官方优雅释放是 AsyncRelease，binding 未暴露）。本实验四档定位最小触发：
  A 起跑+post_stop+wait，不 cap.stop()
  B 不起跑（bind 后什么都不做）
  C 起跑但不 stop
  D 起跑+stop+cap.stop+del 全部（复现 run12）
每档独立子进程跑，退出码即结论（0=干净，3221225477=崩）。
"""
from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_assistant.core.nav_graph import (
    ACTION_NAME,
    RECOGNIZER_NAME,
    ClickAction,
    NavGraph,
    TemplateRecognizer,
    WgcapController,
)

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")
ENTRY = "treasure.__boot.dwell"

user32 = ctypes.WinDLL("user32", use_last_error=True)


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    return int(h) if h else 0


class _WgcAdapter:
    def __init__(self, cap):
        self._cap = cap

    def frame_with_age(self):
        if self._cap.is_running:
            try:
                return self._cap.get_latest_rgb()
            except Exception:  # noqa: BLE001
                pass
        import math
        return (None, 0, 0, math.inf)


def build(mode: str):
    from maaracing_assistant.core.wgcap import WgcCapture

    hwnd = find_hwnd()
    tmp = Path(tempfile.mkdtemp(prefix="v4-bisect-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)
    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded

    graph = NavGraph(None)
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    graph.frame = lambda: np.zeros((720, 1280, 3), dtype=np.uint8)
    graph.click = lambda cx, cy, box_norm, timeout_s: True

    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(0.8)
    ctrl = WgcapController(_WgcAdapter(cap))
    res.register_custom_recognition(RECOGNIZER_NAME, TemplateRecognizer(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))
    tasker = Tasker()
    tasker.bind(res, ctrl)

    job = None
    if mode != "B":
        job = tasker.post_task(ENTRY)
        time.sleep(3.0)
        if mode in ("A", "D", "E", "F"):
            tasker.post_stop()
            job.wait()
    else:
        time.sleep(0.5)
    if mode in ("D", "F"):
        cap.stop()
    elif mode == "E":
        cap.stop()  # 同 F，重复一次确认稳定性
        time.sleep(0.3)
    else:
        cap._stopped = True  # 只停标志，不销毁原生 capture（隔离变量）
        time.sleep(0.3)
    shutil.rmtree(tmp, ignore_errors=True)
    return res, tasker, ctrl, graph, cap, job


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "D"
    objs = build(mode)
    print(f"[{mode}] build 完成，退出前 {len(objs)} 对象存活")
    if mode == "E":
        # 显式断引用，模拟生产模块结束 GC
        del objs
        print("[E] 已 del，等待 GC")
        import gc
        gc.collect()
        gc.collect()
        print("[E] GC 完成仍活")


if __name__ == "__main__":
    main()
