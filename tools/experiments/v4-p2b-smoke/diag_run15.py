# -*- coding: utf-8 -*-
"""P2b 诊断 v15（落点取证）：真实画面跑真源图，探针动作记录 box/归一化坐标。

真机「点击位置完全不对」——ClickAction→graph.click→Clicker 这条链不真执行
（动作换成探针），只记录：节点名、argv.box（注入帧像素）、归一化中心、帧尺寸。
同时存证当前画面与命中框裁剪图，肉眼对照落点是否压在真实按钮上。
"""
from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2

sys.path.insert(0, ".")

from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_assistant.core.nav_graph import (
    ACTION_NAME,
    RECOGNIZER_NAME,
    NavGraph,
    TemplateRecognizer,
    WgcapController,
)
from maaracing_assistant.core.wgcap import WgcCapture

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")
ENTRY = "treasure.__boot.dwell"

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


EVENTS: list = []


class _ProbeAction(CustomAction):
    """替代 MRA_Click：不执行，只记录落点。"""

    def run(self, context, argv):
        b = tuple(argv.box)
        rx, ry, rw, rh = b
        cx, cy = (rx + rw / 2) / 1280, (ry + rh / 2) / 720
        EVENTS.append((argv.node_name, b, (round(cx, 3), round(cy, 3))))
        print(f"[click-probe] {argv.node_name} rect={b} 归一化=({cx:.3f},{cy:.3f})")
        return True


class _PolicyProbe(CustomAction):
    """替代 MRA_Policy：记录决策轮到达（图不代点后，厅类兜底应走这里）。"""

    def __init__(self):
        super().__init__()
        self.hits = 0

    def run(self, context, argv):
        self.hits += 1
        if self.hits % 10 == 1:
            print(f"[policy-probe] 第{self.hits}次决策轮到达（桥未接=图已让位）")
        return True


def _make_probe_recognizer(base_cls):
    class _ProbeRecognizer(base_cls):
        def analyze(self, context, argv):
            r = super().analyze(context, argv)
            d = getattr(r, "detail", None) or {}
            print(f"[reco] {argv.node_name:44} box={getattr(r, 'box', None)} {d}")
            return r
    return _ProbeRecognizer


def main() -> None:
    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到窗口")
        return
    tmp = Path(tempfile.mkdtemp(prefix="v4-land-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)
    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded

    graph = NavGraph(None)
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.0)
    ctrl = WgcapController(_WgcAdapter(cap))
    res.register_custom_recognition(RECOGNIZER_NAME, _make_probe_recognizer(TemplateRecognizer)(graph))
    res.register_custom_action(ACTION_NAME, _ProbeAction())
    from maaracing_assistant.plugins.treasure.policy_bridge import POLICY_ACTION_NAME
    policy_probe = _PolicyProbe()
    res.register_custom_action(POLICY_ACTION_NAME, policy_probe)

    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.post_task(ENTRY)
    time.sleep(8.0)
    tasker.post_stop()
    time.sleep(0.5)
    cap.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[verdict] policy_probe 到达 {policy_probe.hits} 次")

    if LATEST_RGB:
        rgb, fid, _ts, _age = LATEST_RGB[0]
        out = Path("tools/navkit/out/diag_run15_frame.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        print(f"[save] 当前画面: {out} (fid={fid})")
        for name, (x1, y1, x2, y2), _c in EVENTS[:6]:
            crop = rgb[max(y1 - 20, 0):y2 + 20, max(x1 - 20, 0):x2 + 20]
            cv2.imwrite(str(out).replace(".png", f"_hit_{name.split('.')[-1]}.png"),
                        cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
        print(f"[save] 命中框裁剪 x{min(len(EVENTS), 6)} → {out.parent}")
    print(f"[verdict] 探针点击 {len(EVENTS)} 次")


if __name__ == "__main__":
    main()
