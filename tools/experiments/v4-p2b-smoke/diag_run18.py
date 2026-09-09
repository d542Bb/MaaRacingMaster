# -*- coding: utf-8 -*-
"""P2b 诊断 v18b（八炸修复端到端·取证加强版）：真实窗口验证导航链。

对齐 run9 跑通模式：自建 _Events sink（不依赖 logging 配置）+ ClickAction
graph.click 打桩 + 识别命中探针（打印命中 box，静默 miss）。
判据（事件流 20s 窗口）：
- boot Succeeded → 游戏大厅 dwell → 链头/锚点推进 → 鉴宝大厅(选择场次).dwell
- 不应出现「活动页面.dwell」在点击 goto 之后的二次误判（八炸截胡路径）
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
RUN_SECONDS = 20.0

user32 = ctypes.WinDLL("user32", use_last_error=True)

LATEST_RGB: list = []


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    return int(h) if h else 0


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
        hit = getattr(detail, "hit", None)
        name = getattr(detail, "name", str(detail))
        self.rows.append(f"recog {ts} hit={hit} {name}")
        print(f"   [ev] recog {ts} hit={hit} {name}")

    def on_node_action(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        if ts == "Starting":
            return
        name = getattr(detail, "name", str(detail))
        ok = getattr(detail, "success", None)
        self.rows.append(f"action {ts} ok={ok} {name}")
        print(f"   [ev] action {ts} ok={ok} {name}")


def _make_hit_probe(base_cls):
    class _HitProbe(base_cls):
        def analyze(self, context, argv):
            r = super().analyze(context, argv)
            if getattr(r, "box", None):
                print(f"   [hit] {argv.node_name} box={tuple(r.box)}")
            return r
    return _HitProbe


def main() -> None:
    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到「巅峰极速」窗口")
        return
    print(f"[hwnd] {hwnd}")
    tmp = Path(tempfile.mkdtemp(prefix="v4-e2e8b-"))
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
    res.register_custom_recognition(RECOGNIZER_NAME, _make_hit_probe(TemplateRecognizer)(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))

    ev = _Events()
    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.add_context_sink(ev)
    tasker.post_task(ENTRY)
    time.sleep(RUN_SECONDS)
    tasker.post_stop()
    time.sleep(1.0)
    cap.stop()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"[clicks] 打桩点击 {len(clicks)} 次：{clicks[:6]}")
    sess_hit = any("鉴宝大厅" in r and "Succeeded" in r for r in ev.rows)
    act_dwell = [r for r in ev.rows if "活动页面.dwell" in r and "Succeeded" in r]
    print(f"[verdict] 到达鉴宝厅 dwell={sess_hit} | 活动页面 dwell 命中 {len(act_dwell)} 次")
    if LATEST_RGB:
        rgb, fid, _ts, _age = LATEST_RGB[0]
        out = Path("tools/navkit/out/diag_run18_frame.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        print(f"[save] 当前画面: {out} (fid={fid})")


if __name__ == "__main__":
    main()
