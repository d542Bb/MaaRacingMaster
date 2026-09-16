# -*- coding: utf-8 -*-
"""P2b 诊断 v9（boot 端到端）：真实窗口 + 真实 WGC + 真源 32 节点 + boot 入口。

三炸根修（起跑汇聚节点）的最终验证：当前画面是鉴宝大厅（卡片已领取态），
旧链头入口永不命中；boot 大 Or 应命中 → 路由 dwell → 锚点 → MRA_Click
（graph.click 打桩，不真点游戏）。期望事件流：boot 命中 → dwell 命中 →
锚点动作被调 → 停止干净。
"""
from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path


sys.path.insert(0, ".")

from maa.context import ContextEventSink
from maa.event_sink import NotificationType
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
    if h:
        return int(h)
    found = []
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    IsWindowVisible = user32.IsWindowVisible

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _l):
        if not IsWindowVisible(hwnd):
            return True
        n = GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(hwnd, buf, n + 1)
        if "巅峰" in buf.value or "极速" in buf.value:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


class _WgcAdapter:
    """真实 WgcCapture 的 frame_with_age 适配（与 CaptureAdapter 同形态）。"""

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


class _Events(ContextEventSink):
    def __init__(self):
        self.rows: list[str] = []

    def on_node_recognition(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        if ts == "Starting":
            return
        hit = getattr(detail, "hit", None)
        name = getattr(detail, "name", str(detail))
        self.rows.append(f"recog {ts} hit={hit} {name}")

    def on_node_action(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        if ts == "Starting":
            return
        name = getattr(detail, "name", str(detail))
        ok = getattr(detail, "success", None)
        self.rows.append(f"action {ts} ok={ok} {name}")


def main() -> None:
    from maaracing_assistant.core.wgcap import WgcCapture

    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到「巅峰极速」窗口")
        return
    print(f"[hwnd] {hwnd}")

    tmp = Path(tempfile.mkdtemp(prefix="v4-e2e-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)
    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded
    print(f"[load] ok nodes={len(res.node_list)}")

    graph = NavGraph(None)
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    clicks: list = []
    graph.click = lambda cx, cy, box_norm, timeout_s: (clicks.append((cx, cy, box_norm)), True)[1]

    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.0)
    ctrl = WgcapController(_WgcAdapter(cap))
    res.register_custom_recognition(RECOGNIZER_NAME, TemplateRecognizer(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))

    ev = _Events()
    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.add_context_sink(ev)
    tasker.post_task(ENTRY)
    time.sleep(6.0)
    tasker.post_stop()
    time.sleep(1.0)
    cap.stop()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"[events] 非 Starting 事件 {len(ev.rows)} 条：")
    for r in ev.rows[:30]:
        print(f"   {r}")
    print(f"[clicks] 打桩点击 {len(clicks)} 次：{clicks[:5]}")
    boot_hit = any("__boot" in r and "Succeeded" in r for r in ev.rows)
    print(f"[verdict] boot 命中={boot_hit} | dwell/锚点推进={len(ev.rows) > 2} "
          f"| 点击到达={len(clicks) > 0}")


if __name__ == "__main__":
    main()
