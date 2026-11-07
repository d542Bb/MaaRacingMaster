# -*- coding: utf-8 -*-
"""A0-1 补充 3：通道契约量化 + probe 单次开销。

analyze 内无条件 BGR→RGB，故传帧必须是 BGR。这里在**边界帧**上验证：
如果误传 RGB，命中序列会不会变（会不会误判/漏判）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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
IMAGE_DIRS = [REPO / "maaracing_master/plugins/speedrush/resources/image",
              REPO / "maaracing_master/core/resources/image"]
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")

RES: dict[str, dict] = {}


class FakeGraph:
    def __init__(self):
        self.image_dirs = [d for d in IMAGE_DIRS if d.is_dir()]
        self._last_frame = None

    def frame(self): return self._last_frame
    def _log(self, msg, level="INFO"): pass
    def _publish_nav(self, name, **kw): pass


class FrameController(CustomController):
    def __init__(self):
        self.frame = np.zeros((720, 1280, 3), np.uint8)
        super().__init__()

    def connect(self): return True
    def request_uuid(self): return "a0"
    def start_app(self, i): return True
    def stop_app(self, i): return True
    def screencap(self): return self.frame
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


class ProbeAction(CustomAction):
    def __init__(self):
        super().__init__()
        self.frame = None

    def run(self, context, argv):
        p = json.loads(argv.custom_action_param or "{}")
        if isinstance(p, str):
            p = json.loads(p)
        d = context.run_recognition(p["node"], self.frame)
        box = getattr(d, "box", None) if d is not None else None
        RES[p["probe_id"]] = {
            "hit": bool(d.hit) if d is not None else False,
            "box": [int(v) for v in box] if box is not None else None,
        }
        return True


def main() -> None:
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    bridge = ProbeAction()
    res.register_custom_action("A0_Probe", bridge)
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if d.is_dir():
            for f in d.glob("*.json"):
                (tmp / f.name).write_bytes(f.read_bytes())
    res.post_pipeline(str(tmp)).wait()
    ctrl = FrameController()
    tasker = Tasker()
    tasker.bind(res, ctrl)

    rows = [json.loads(line) for line in (NAV / "frames.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    # 边界帧段：驾驶页锚点 892~897，商店横幅 1408~1424
    segs = [(892, 897, "speedrush.驾驶页锚点"), (1408, 1424, "speedrush.商店横幅锚点")]

    diffs = 0
    total = 0
    times: list[float] = []
    for lo, hi, node in segs:
        print(f"\n=== {node}  {lo}~{hi} ===")
        print("seq   BGR(hit,box)              RGB(hit,box)              same")
        for r in rows:
            if not lo <= r["seq"] <= hi:
                continue
            p = NAV / r["file"]
            if not p.is_file():
                continue
            bgr = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            out = {}
            for tag, frame in (("bgr", bgr), ("rgb", rgb)):
                ctrl.frame = frame
                bridge.frame = frame
                pid = f"{r['seq']}-{tag}"
                e = {"A0.Entry": {
                    "recognition": {"type": "DirectHit"},
                    "action": {"type": "Custom", "param": {
                        "custom_action": "A0_Probe",
                        "custom_action_param": {"probe_id": pid, "node": node}}},
                    "rate_limit": 100, "timeout": 3000}}
                t0 = time.monotonic()
                tasker.post_task("A0.Entry", pipeline_override=e).wait()
                times.append(time.monotonic() - t0)
                out[tag] = RES.pop(pid, {})
            same = out["bgr"]["hit"] == out["rgb"]["hit"] and out["bgr"]["box"] == out["rgb"]["box"]
            total += 1
            if not same:
                diffs += 1
            print(f"{r['seq']:<5} {str(out['bgr']):<26} {str(out['rgb']):<26} {same}")

    times.sort()
    print(f"\n对比 {total} 帧，判定不一致 {diffs} 帧")
    print(f"probe 单次耗时: 中位 {times[len(times)//2]*1000:.0f}ms  "
          f"p90 {times[int(len(times)*0.9)]*1000:.0f}ms  max {times[-1]*1000:.0f}ms")


if __name__ == "__main__":
    main()
