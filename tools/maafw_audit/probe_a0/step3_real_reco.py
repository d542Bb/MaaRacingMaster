# -*- coding: utf-8 -*-
"""A0-1/A0-2：用项目**真实** TemplateRecognizer 跑 B 链路。

与 step2 的差别：注册的是 core/nav_graph.py 的 TemplateRecognizer（真源节点里
MaaRM_Template 的实现），graph 依赖用最小替身补齐，因此识别结果与 run() 路径同源。

验：链路、传帧生效、通道契约、两个门控锚点在录制帧上的命中序列。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.core.nav_graph import TemplateRecognizer  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
CORE_PIPELINE = REPO / "maaracing_master/core/resources/pipeline"
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
IMAGE_DIRS = [
    REPO / "maaracing_master/plugins/speedrush/resources/image",
    REPO / "maaracing_master/core/resources/image",
]
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")


class FakeGraph:
    """TemplateRecognizer 需要的最小宿主面（只读识别用）。"""

    def __init__(self) -> None:
        self.image_dirs = [d for d in IMAGE_DIRS if d.is_dir()]
        self._last_frame = None
        self.logs: list[str] = []
        self.nav_events: list[dict] = []

    def frame(self):
        return self._last_frame

    def _log(self, msg: str, level: str = "INFO") -> None:
        self.logs.append(f"[{level}] {msg}")

    def _publish_nav(self, name: str, **kw) -> None:
        self.nav_events.append({"name": name, **kw})


class FrameController(CustomController):
    def __init__(self, frame: np.ndarray) -> None:
        self.frame = frame
        self.caps = 0
        super().__init__()

    def connect(self): return True
    def request_uuid(self): return "a0"
    def start_app(self, i): return True
    def stop_app(self, i): return True

    def screencap(self):
        self.caps += 1
        return self.frame

    def click(self, x, y): return True
    def swipe(self, *a): return True
    def touch_down(self, *a): return True
    def touch_move(self, *a): return True
    def touch_up(self, c): return True
    def click_key(self, k): return True
    def input_text(self, t): return True
    def key_down(self, k): return True
    def key_up(self, k): return True
    def scroll(self, dx, dy): return True
    def relative_move(self, dx, dy): return True
    def shell(self, c, t): return None
    def inactive(self): return True


RESULTS: dict[str, dict] = {}


class ProbeAction(CustomAction):
    """只读桥：一次取帧 → run_recognition → 按 probe_id 回传。"""

    def __init__(self) -> None:
        super().__init__()
        self.frame: np.ndarray | None = None

    def run(self, context, argv):
        raw = argv.custom_action_param
        p = json.loads(raw) if raw else {}
        if isinstance(p, str):
            p = json.loads(p)
        pid, node = p.get("probe_id"), p.get("node")
        if context is None:
            RESULTS[pid] = {"error": "context is None"}
            return True
        try:
            detail = context.run_recognition(node, self.frame)
            hit = bool(detail.hit) if detail is not None else False
            box = getattr(detail, "box", None) if detail is not None else None
            RESULTS[pid] = {
                "hit": hit,
                "box": [int(v) for v in box] if box is not None else None,
                "best": getattr(detail, "best_result", None) is not None if detail else False,
            }
        except Exception as exc:  # noqa: BLE001
            RESULTS[pid] = {"error": f"{type(exc).__name__}: {exc}"}
        return True


def make_tasker(bridge: ProbeAction, cap_frame: np.ndarray, graph: FakeGraph):
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(graph))
    res.register_custom_action("A0_Probe", bridge)
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            (tmp / f.name).write_bytes(f.read_bytes())
    res.post_pipeline(str(tmp)).wait()
    tasker = Tasker()
    tasker.bind(res, FrameController(cap_frame))
    return tasker


def probe_once(node: str, cap_frame: np.ndarray, bridge_frame: np.ndarray) -> dict:
    bridge = ProbeAction()
    bridge.frame = bridge_frame
    graph = FakeGraph()
    tasker = make_tasker(bridge, cap_frame, graph)
    entry = {
        "A0.Entry": {
            "recognition": {"type": "DirectHit"},
            "action": {"type": "Custom", "param": {
                "custom_action": "A0_Probe",
                "custom_action_param": {"probe_id": "p1", "node": node},
            }},
            "rate_limit": 100,
            "timeout": 3000,
        }
    }
    tasker.post_task("A0.Entry", pipeline_override=entry).wait()
    r = RESULTS.pop("p1", {})
    r["nav_events"] = graph.nav_events
    r["logs"] = graph.logs[-3:]
    return r


def main() -> None:
    drive = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    store = cv2.imread(str(NAV / "000894.jpg"), cv2.IMREAD_COLOR)
    print(f"[frame] drive={drive.shape} store={store.shape}")
    print(f"[dirs] {[d.name for d in IMAGE_DIRS if d.is_dir()]}")

    cases = [
        ("A 驾驶帧/驾驶页锚点", "speedrush.驾驶页锚点", drive, drive),
        ("B 商店帧/驾驶页锚点", "speedrush.驾驶页锚点", store, store),
        ("C cap=商店 桥传驾驶帧", "speedrush.驾驶页锚点", store, drive),
        ("D cap=驾驶 桥传商店帧", "speedrush.驾驶页锚点", drive, store),
        ("E 商店帧/商店横幅锚点", "speedrush.商店横幅锚点", store, store),
        ("F 驾驶帧/商店横幅锚点", "speedrush.商店横幅锚点", drive, drive),
        ("G 驾驶帧RGB(错通道)", "speedrush.驾驶页锚点", drive,
         cv2.cvtColor(drive, cv2.COLOR_BGR2RGB)),
    ]
    for tag, node, cap, passed in cases:
        r = probe_once(node, cap, passed)
        print(f"\n=== {tag} ===")
        print(f"  hit={r.get('hit')} box={r.get('box')} err={r.get('error')}")
        print(f"  nav={r.get('nav_events')}")
        if r.get("logs"):
            print(f"  log={r['logs']}")


if __name__ == "__main__":
    main()
