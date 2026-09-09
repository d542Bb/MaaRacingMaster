# -*- coding: utf-8 -*-
"""P2b 诊断 v21（八炸端到端·真点击）：采集修复后完整导航链验证。

与 run18 的区别：graph.click 走生产真实路径（Clicker real 模式 SendInput，
需游戏前台），policy_loop 用探针（到达计数、不点击——不会开始匹配、无消耗）。

期望事件流：boot → 游戏大厅 dwell → 链头真点击巅峰卡 → confirm(goto) →
rhall_to_treasure.1 真点击前往 → confirm(session cards) → 鉴宝厅 dwell →
policy 探针循环。判据：鉴宝厅 dwell Succeeded + 探针到达>0。
"""
from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import cv2

sys.path.insert(0, ".")

from maa.context import ContextEventSink
from maa.custom_action import CustomAction
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
from maaracing_assistant.core.wgcap import WgcCapture

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")
ENTRY = "treasure.__boot.dwell"
RUN_SECONDS = 30.0

user32 = ctypes.WinDLL("user32", use_last_error=True)

LATEST_RGB: list = []


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    return int(h) if h else 0


class _Ctx:
    """生产 NavGraph.click 所需的最小 ctx（real 鼠标模式）。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.click_mode = "real"
        self.intent_mode = False
        self.lifecycle = type("L", (), {"running": True})()
        self.capture = type("Cap", (), {"screenshot": staticmethod(
            lambda: __import__("numpy").zeros((720, 1280, 3),
                                              dtype=__import__("numpy").uint8))})()


class _WgcAdapter:
    def __init__(self, cap):
        self._cap = cap

    def frame_with_age(self):
        if self._cap.is_running:
            try:
                rgb = self._cap.get_latest_rgb()
                LATEST_RGB.clear()
                LATEST_RGB.append(rgb)
                return rgb
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
        name = getattr(detail, "name", str(detail))
        if ts == "Failed" and name.endswith(".dwell"):
            return  # dwell 驻留重试噪声
        self.rows.append(f"recog {ts} {name}")
        print(f"   [ev] recog {ts} {name}")

    def on_node_action(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        if ts == "Starting":
            return
        name = getattr(detail, "name", str(detail))
        ok = getattr(detail, "success", None)
        self.rows.append(f"action {ts} ok={ok} {name}")
        print(f"   [ev] action {ts} ok={ok} {name}")


class _PolicyProbe(CustomAction):
    def __init__(self):
        super().__init__()
        self.hits = 0

    def run(self, context, argv):
        self.hits += 1
        if self.hits % 15 == 1:
            print(f"[policy-probe] 第{self.hits}次决策轮到达（不点击）")
        return True


def main() -> None:
    from maaracing_assistant.plugins.treasure.policy_bridge import POLICY_ACTION_NAME

    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到「巅峰极速」窗口")
        return
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.6)
    tmp = Path(tempfile.mkdtemp(prefix="v4-e2e21-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)
    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded
    print(f"[load] ok nodes={len(res.node_list)}")

    graph = NavGraph(_Ctx(hwnd))
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.0)
    ctrl = WgcapController(_WgcAdapter(cap))
    res.register_custom_recognition(RECOGNIZER_NAME, TemplateRecognizer(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))
    probe = _PolicyProbe()
    res.register_custom_action(POLICY_ACTION_NAME, probe)

    ev = _Events()
    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.add_context_sink(ev)
    tasker.post_task(ENTRY)
    t0 = time.perf_counter()
    sess_hit_at = None
    for _ in range(int(RUN_SECONDS * 4)):
        time.sleep(0.25)
        if any("鉴宝大厅" in r and "recog Succeeded" in r for r in ev.rows):
            sess_hit_at = time.perf_counter() - t0
            break
    tasker.post_stop()
    time.sleep(1.0)
    cap.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        graph._clicker.shutdown()
    except Exception:
        pass

    print(f"[verdict] 鉴宝厅 dwell 到达={sess_hit_at is not None}"
          f"{'（%.1fs）' % sess_hit_at if sess_hit_at else ''} | policy 探针 {probe.hits} 次")
    if LATEST_RGB:
        rgb, fid, _ts, _age = LATEST_RGB[0]
        out = Path("tools/navkit/out/diag_run21_frame.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        print(f"[save] 结束画面: {out} (fid={fid})")


if __name__ == "__main__":
    main()
