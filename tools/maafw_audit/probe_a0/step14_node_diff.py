# -*- coding: utf-8 -*-
"""诊断 2：回读框架解析后的节点数据，对比快/慢两个节点的差异。

快：speedrush.驾驶页锚点（0ms）
慢：speedrush.ProbeEntry / MaaRM_Probe.Entry（~410ms）

同时打印任务详情的 node_id_list，确认慢任务里到底跑了什么。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import cv2

from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.core.nav_graph import TemplateRecognizer  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
SPEEDRUSH_IMAGE = REPO / "maaracing_master/plugins/speedrush/resources/image"
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")


class FakeGraph:
    def __init__(self):
        self.image_dirs = [SPEEDRUSH_IMAGE]
        self._last_frame = None

    def frame(self): return self._last_frame
    def _log(self, msg, level="INFO"): pass
    def _publish_nav(self, name, **kw): pass


class FC(CustomController):
    def __init__(self, f):
        self.f = f
        super().__init__()

    def connect(self): return True
    def request_uuid(self): return "a0"
    def start_app(self, i): return True
    def stop_app(self, i): return True
    def screencap(self): return self.f
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


class Noop(CustomAction):
    def run(self, context, argv): return True


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for f in SPEEDRUSH_PIPELINE.glob("*.json"):
        (tmp / f.name).write_bytes(f.read_bytes())
    entry = {
        "recognition": {"type": "DirectHit"},
        "action": {"type": "Custom", "param": {"custom_action": "A0_Noop"}},
        "rate_limit": 0,
        "timeout": 1000,
    }
    sp = tmp / "speedrush.json"
    data = json.loads(sp.read_text(encoding="utf-8"))
    data["speedrush.ProbeEntry"] = entry
    sp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    res.register_custom_action("A0_Noop", Noop())
    res.post_pipeline(str(tmp)).wait()

    print("=== 框架解析后的节点数据 ===")
    for name in ("speedrush.驾驶页锚点", "speedrush.ProbeEntry"):
        d = res.get_node_data(name)
        if d is None:
            print(name, "-> None")
            continue
        keys = sorted(k for k in d.keys())
        print(f"\n{name}: 字段 {keys}")
        for k in ("recognition", "action", "rate_limit", "timeout", "next",
                  "on_error", "max_hit", "repeat", "pre_delay", "post_delay",
                  "pre_wait_freezes", "post_wait_freezes", "enabled", "inverse",
                  "focus", "attach"):
            if k in d:
                print(f"   {k} = {json.dumps(d[k], ensure_ascii=False)[:200]}")

    bgr = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    tasker = Tasker()
    tasker.bind(res, FC(bgr))

    print("\n=== 任务详情 ===")
    for name in ("speedrush.驾驶页锚点", "speedrush.ProbeEntry"):
        t0 = time.monotonic()
        job = tasker.post_task(name).wait()
        dt = (time.monotonic() - t0) * 1000
        det = job.get(wait=True)
        print(f"{name}: {dt:.0f}ms  succeeded={getattr(job.status, 'succeeded', None)}")
        try:
            print(f"   task_detail={det}")
        except Exception as exc:  # noqa: BLE001
            print(f"   detail 读取失败 {exc}")


if __name__ == "__main__":
    main()
