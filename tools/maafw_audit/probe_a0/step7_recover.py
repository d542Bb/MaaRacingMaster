# -*- coding: utf-8 -*-
"""A0-1 补充 2：停止状态何时解除、解除后能否恢复；桥异常的判定口径。"""

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
CORE_PIPELINE = REPO / "maaracing_master/core/resources/pipeline"
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
IMAGE_DIRS = [REPO / "maaracing_master/plugins/speedrush/resources/image",
              REPO / "maaracing_master/core/resources/image"]
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")

EV: list[str] = []
T0 = time.monotonic()


def ev(m: str) -> None:
    EV.append(f"{time.monotonic()-T0:7.3f} {m}")


class FakeGraph:
    def __init__(self):
        self.image_dirs = [d for d in IMAGE_DIRS if d.is_dir()]
        self._last_frame = None

    def frame(self): return self._last_frame
    def _log(self, msg, level="INFO"): pass
    def _publish_nav(self, name, **kw): pass


class FrameController(CustomController):
    def __init__(self, frame):
        self.frame = frame
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


class SlowBridge(CustomAction):
    def run(self, context, argv):
        p = json.loads(argv.custom_action_param or "{}")
        if isinstance(p, str):
            p = json.loads(p)
        ev(f"bridge ENTER pid={p['probe_id']}")
        time.sleep(float(p.get("delay", 0)))
        ev(f"bridge EXIT pid={p['probe_id']}")
        return True


class BoomBridge(CustomAction):
    def run(self, context, argv):
        ev("bridge BOOM")
        raise RuntimeError("a0-injected")


def build(frame):
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    res.register_custom_action("A0_Slow", SlowBridge())
    res.register_custom_action("A0_Boom", BoomBridge())
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if d.is_dir():
            for f in d.glob("*.json"):
                (tmp / f.name).write_bytes(f.read_bytes())
    res.post_pipeline(str(tmp)).wait()
    ctrl = FrameController(frame)
    tasker = Tasker()
    tasker.bind(res, ctrl)
    return tasker


def slow(pid: str, delay: float) -> dict:
    return {"A0.Slow": {
        "recognition": {"type": "DirectHit"},
        "action": {"type": "Custom", "param": {
            "custom_action": "A0_Slow",
            "custom_action_param": {"probe_id": pid, "delay": delay}}},
        "rate_limit": 100, "timeout": 3000}}


def wait_done(job, timeout: float) -> bool:
    dl = time.monotonic() + timeout
    while time.monotonic() < dl:
        if job.done:
            return True
        time.sleep(0.02)
    return False


def main() -> None:
    frame = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    tasker = build(frame)

    print("=== S6 停止后 stopping 状态与恢复 ===")
    job1 = tasker.post_task("A0.Slow", pipeline_override=slow("p1", 3.0))
    time.sleep(0.3)
    ev("post_stop")
    stop_job = tasker.post_stop()
    for i in range(10):
        time.sleep(0.5)
        ev(f"+{0.5*(i+1):.1f}s stopping={tasker.stopping} job1.done={job1.done} stop_job.done={stop_job.done}")
        if job1.done and stop_job.done:
            break
    ev("收摊完成，尝试新任务 p3")
    job3 = tasker.post_task("A0.Slow", pipeline_override=slow("p3", 0.05))
    ok3 = wait_done(job3, 5.0)
    ev(f"p3 done={ok3} succeeded={getattr(job3.status,'succeeded',None)}")
    print(f"  p3 恢复: done={ok3} succeeded={getattr(job3.status,'succeeded',None)}")
    for line in EV:
        print("   ", line)
    EV.clear()

    print("\n=== S7 桥异常：job 状态与动作明细 ===")
    e = {"A0.Boom": {
        "recognition": {"type": "DirectHit"},
        "action": {"type": "Custom", "param": {"custom_action": "A0_Boom"}},
        "rate_limit": 100, "timeout": 2000}}
    job = tasker.post_task("A0.Boom", pipeline_override=e)
    wait_done(job, 5.0)
    st = job.status
    print(f"  succeeded={getattr(st,'succeeded',None)}")
    try:
        det = tasker.get_action_detail("A0.Boom")
        print(f"  action_detail={det}")
    except Exception as exc:  # noqa: BLE001
        print(f"  action_detail EXC {exc}")
    for line in EV:
        print("   ", line)
    EV.clear()


if __name__ == "__main__":
    main()
