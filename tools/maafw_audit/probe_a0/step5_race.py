# -*- coding: utf-8 -*-
"""A0-1：超时 / 停止 / 迟到回调竞态。

用真实 Tasker 验三件事：
  1. post_stop 能否打断正在执行（sleep 中）的 CustomAction
  2. 收摊失败时，按 id 隔离的容器能否挡住迟到回写
  3. 新调用（新 probe_id）是否完全不受旧回调影响

容器逻辑照方案「闭合点 3」实现：pending / done / stale，迟到写只落 stale 记录。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
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

EVENTS: list[str] = []
EV_LOCK = threading.Lock()


def ev(msg: str) -> None:
    with EV_LOCK:
        EVENTS.append(f"{time.monotonic():.3f} {msg}")


class FakeGraph:
    def __init__(self):
        self.image_dirs = [d for d in IMAGE_DIRS if d.is_dir()]
        self._last_frame = None

    def frame(self):
        return self._last_frame

    def _log(self, msg, level="INFO"):
        pass

    def _publish_nav(self, name, **kw):
        pass


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


class Coord:
    """按 probe_id 隔离的结果容器（方案闭合点 3 的最小实现）。"""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.lock = threading.Lock()

    def begin(self, pid: str) -> None:
        with self.lock:
            self.records[pid] = {"state": "pending", "hit": None}

    def write(self, pid: str, hit: bool) -> str:
        with self.lock:
            r = self.records.get(pid)
            if r is None:
                ev(f"write pid={pid} -> DROPPED(no record)")
                return "dropped"
            if r["state"] == "stale":
                ev(f"write pid={pid} -> DROPPED(stale)")
                return "dropped"
            r["hit"] = hit
            r["state"] = "done"
            ev(f"write pid={pid} -> ACCEPTED hit={hit}")
            return "accepted"

    def invalidate(self, pid: str, settled: bool) -> None:
        with self.lock:
            if pid not in self.records:
                return
            if settled:
                del self.records[pid]
                ev(f"invalidate pid={pid} -> deleted")
            else:
                self.records[pid]["state"] = "stale"
                ev(f"invalidate pid={pid} -> stale")

    def read(self, pid: str) -> dict | None:
        with self.lock:
            r = self.records.get(pid)
            return dict(r) if r else None


class SlowBridge(CustomAction):
    """delay 秒后才回写，模拟卡住/迟到的回调。"""

    def __init__(self, coord: Coord):
        super().__init__()
        self.coord = coord
        self.frame = None
        self.hit_value = True

    def run(self, context, argv):
        p = json.loads(argv.custom_action_param or "{}")
        if isinstance(p, str):
            p = json.loads(p)
        pid, delay = p["probe_id"], float(p.get("delay", 0))
        ev(f"bridge enter pid={pid} delay={delay}")
        time.sleep(delay)
        ev(f"bridge wake pid={pid}")
        # 模拟迟到回写（真实桥会先 run_recognition 再写）
        self.coord.write(pid, self.hit_value)
        ev(f"bridge exit pid={pid}")
        return True


def build(coord: Coord, frame):
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    res.register_custom_action("A0_Slow", SlowBridge(coord))
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


def entry(delay: float) -> dict:
    return {
        "A0.Slow": {
            "recognition": {"type": "DirectHit"},
            "action": {"type": "Custom", "param": {
                "custom_action": "A0_Slow",
                "custom_action_param": {"probe_id": "PLACEHOLDER", "delay": delay},
            }},
            "rate_limit": 100,
            "timeout": 3000,
        }
    }


def post(tasker, pid: str, delay: float):
    e = entry(delay)
    e["A0.Slow"]["action"]["param"]["custom_action_param"]["probe_id"] = pid
    return tasker.post_task("A0.Slow", pipeline_override=e)


def wait_done(job, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.done:
            return True
        time.sleep(0.02)
    return False


def scenario(title: str, delay: float, budget: float, stop_after: float) -> None:
    print(f"\n=== {title} ===")
    ev(f"--- start: delay={delay} budget={budget} stop_after={stop_after}")
    coord = Coord()
    frame = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    tasker = build(coord, frame)

    pid = "p1"
    coord.begin(pid)
    t0 = time.monotonic()
    job = post(tasker, pid, delay)

    if wait_done(job, budget):
        ev(f"job done within budget ({time.monotonic()-t0:.2f}s)")
        r = coord.read(pid)
        print(f"  结果: {r}")
        coord.invalidate(pid, settled=True)
    else:
        ev(f"超时 ({budget}s)，发停止请求")
        tasker.post_stop()
        settled = wait_done(job, stop_after)
        ev(f"停止后 job done={settled} ({time.monotonic()-t0:.2f}s)")
        coord.invalidate(pid, settled=settled)
        print(f"  超时处理: settled={settled} 记录={coord.read(pid)}")

        # 新调用立刻开始（新 id）
        pid2 = "p2"
        coord.begin(pid2)
        job2 = post(tasker, pid2, 0.05)
        ok2 = wait_done(job2, 3.0)
        ev(f"p2 done={ok2}")
        print(f"  新调用 p2: {coord.read(pid2)}")

        # 等旧回调彻底跑完，看它写到了哪
        time.sleep(max(0.0, delay - (time.monotonic() - t0)) + 0.3)
        print(f"  旧调用 p1 迟到后记录: {coord.read(pid)}")
        print(f"  p2 最终记录: {coord.read(pid2)}")

    print("  事件:")
    for line in EVENTS:
        print("   ", line)
    EVENTS.clear()
    try:
        tasker.post_stop().wait()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    scenario("S1 正常完成", delay=0.05, budget=3.0, stop_after=1.0)
    scenario("S2 超时后停止（回调 1s）", delay=1.0, budget=0.3, stop_after=3.0)
    scenario("S3 超时后停止（回调 5s，收摊失败）", delay=5.0, budget=0.3, stop_after=1.0)


if __name__ == "__main__":
    main()
