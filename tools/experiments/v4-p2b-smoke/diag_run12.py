# -*- coding: utf-8 -*-
"""P2b 诊断 v12（stop 语义实验）：常驻图（timeout=-1）下 post_stop + job.wait 行为。

真机「点击停止会卡住」头号嫌疑 = NavKitV4.stop 的 job.wait()：boot/dwell 图
timeout=-1 无限驻留，若 MaaFw 的 PostStop 语义是等待当前任务自然结束，wait
永不返回 → 停止卡死。本实验复刻生产形态（真实窗口 + boot 循环 + 点击打桩），
post_stop 后用线程化 job.wait 限时 10s 探测：挂起=复现，并记录 job.status；
正常返回=嫌疑排除，转查租约/GC。
"""
from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

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


def main() -> None:
    from maaracing_assistant.core.wgcap import WgcCapture

    log_f = Path("tools/navkit/out/diag_run12.log").open("a", encoding="utf-8")

    def say(msg: str) -> None:
        print(msg)
        log_f.write(msg + "\n")
        log_f.flush()

    hwnd = find_hwnd()
    if not hwnd:
        say("[abort] 找不到窗口")
        return
    tmp = Path(tempfile.mkdtemp(prefix="v4-stop-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)
    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded
    say("[ok] resource loaded")

    graph = NavGraph(None)
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    # 打桩：模拟生产 frame_size 修复后的真实点击形态，但不碰输入设备
    graph.frame = lambda: __import__("numpy").zeros((720, 1280, 3), dtype=__import__("numpy").uint8)
    graph.click = lambda cx, cy, box_norm, timeout_s: True

    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.0)
    ctrl = WgcapController(_WgcAdapter(cap))
    res.register_custom_recognition(RECOGNIZER_NAME, TemplateRecognizer(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))
    say("[ok] bridges registered")

    tasker = Tasker()
    tasker.bind(res, ctrl)
    job = tasker.post_task(ENTRY)
    say("[ok] task posted，跑 3s")
    time.sleep(3.0)
    say("[ok] 3s 跑完（若此处前已崩=起跑期崩溃）")

    t0 = time.perf_counter()
    tasker.post_stop()
    say(f"[stop] post_stop 返回，耗时 {time.perf_counter() - t0:.3f}s")

    done = threading.Event()

    def _wait():
        job.wait()
        done.set()

    th = threading.Thread(target=_wait, daemon=True)
    th.start()
    th.join(timeout=10.0)
    dt = time.perf_counter() - t0
    if done.is_set():
        say(f"[stop] job.wait 正常返回，累计 {dt:.2f}s | status={job.status}")
    else:
        say(f"[stop] *** job.wait 挂起 >10s（复现真机卡死）| status={job.status}")
        for i in range(5):
            time.sleep(2)
            s = job.status
            say(f"[poll+{(i + 1) * 2}s] status={s} done={s.done}")
            if s.done:
                break
    say("[ok] 主流程结束，进入清理（崩溃若在此=GC 期）")
    cap.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    say("[done] 全部干净")


if __name__ == "__main__":
    main()
