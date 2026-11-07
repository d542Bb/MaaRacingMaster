# -*- coding: utf-8 -*-
"""A0-1 补充：收摊失败后新任务的真实归宿 + 桥异常 + 入口识别失败。

step5 发现 S3 里旧回调 5s 未退出时，新 post_task 的桥根本没被调用。
这里确认它是「排队等旧任务」还是「被丢弃」，并测异常路径。
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
from maa.custom_recognition import CustomRecognition
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

EVENTS: list[str] = []
T0 = time.monotonic()


def ev(m: str) -> None:
    EVENTS.append(f"{time.monotonic()-T0:7.3f} {m}")


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
        ev("bridge BOOM 抛异常")
        raise RuntimeError("a0-injected-bridge-failure")


class MissReco(CustomRecognition):
    """恒不命中：用于造任务失败。"""

    def analyze(self, context, argv):
        ev("reco MISS called")
        return self.AnalyzeResult(box=None, detail={"reason": "a0-always-miss"})


def build(frame):
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    res.register_custom_recognition("A0_Miss", MissReco())
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


def slow_entry(pid: str, delay: float) -> dict:
    return {"A0.Slow": {
        "recognition": {"type": "DirectHit"},
        "action": {"type": "Custom", "param": {
            "custom_action": "A0_Slow",
            "custom_action_param": {"probe_id": pid, "delay": delay},
        }},
        "rate_limit": 100, "timeout": 3000,
    }}


def wait_done(job, timeout: float) -> bool:
    dl = time.monotonic() + timeout
    while time.monotonic() < dl:
        if job.done:
            return True
        time.sleep(0.02)
    return False


def s3_extended(tasker) -> None:
    print("\n=== S3b 收摊失败后新任务排队多久才跑 ===")
    job1 = tasker.post_task("A0.Slow", pipeline_override=slow_entry("p1", 5.0))
    time.sleep(0.3)
    ev("超时，发停止")
    tasker.post_stop()
    settled = wait_done(job1, 1.0)
    ev(f"收摊 settled={settled}")
    job2 = tasker.post_task("A0.Slow", pipeline_override=slow_entry("p2", 0.05))
    ev("p2 已提交")
    for _ in range(120):           # 最多等 12s
        if job2.done:
            break
        time.sleep(0.1)
    ev(f"p2 done={job2.done}（提交后 {0:.0f}s 内）")
    print(f"  p1 settled={settled}  p2 done={job2.done}")
    print(f"  p1 status={getattr(job1.status,'succeeded',None)} p2 status={getattr(job2.status,'succeeded',None)}")
    for line in EVENTS:
        print("   ", line)
    EVENTS.clear()


def s4_bridge_exception(tasker) -> None:
    print("\n=== S4 桥抛异常 ===")
    e = {"A0.Boom": {
        "recognition": {"type": "DirectHit"},
        "action": {"type": "Custom", "param": {"custom_action": "A0_Boom"}},
        "rate_limit": 100, "timeout": 2000,
    }}
    job = tasker.post_task("A0.Boom", pipeline_override=e)
    ok = wait_done(job, 5.0)
    st = getattr(job.status, "succeeded", None) if job.status else None
    print(f"  job done={ok} succeeded={st}")
    for line in EVENTS:
        print("   ", line)
    EVENTS.clear()


def s5_task_failure(tasker) -> None:
    print("\n=== S5 入口识别恒不命中（任务失败） ===")
    e = {"A0.MissEntry": {
        "recognition": {"type": "Custom", "param": {"custom_recognition": "A0_Miss"}},
        "action": {"type": "DoNothing"},
        "rate_limit": 100, "timeout": 800,
    }}
    t0 = time.monotonic()
    job = tasker.post_task("A0.MissEntry", pipeline_override=e)
    ok = wait_done(job, 5.0)
    dt = time.monotonic() - t0
    st = getattr(job.status, "succeeded", None) if job.status else None
    print(f"  job done={ok} succeeded={st} 耗时={dt:.2f}s")
    for line in EVENTS:
        print("   ", line)
    EVENTS.clear()


def main() -> None:
    frame = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    tasker = build(frame)
    s3_extended(tasker)
    s4_bridge_exception(tasker)
    s5_task_failure(tasker)


if __name__ == "__main__":
    main()
