# -*- coding: utf-8 -*-
"""A0-1 补充 4：结果提交的有效性检查（tombstone）。

step5 已证明「无条件赋值」下迟到回写会被接受。这里验证修正版：
  桥提交前在锁内检查记录仍为 pending；超时先置 stale；收摊失败保留 tombstone
  不删；只有确认框架空闲（stopping=False）才清理墓碑。

场景对照：
  N  无检查（现状）：迟到回写 ACCEPTED
  C  有检查 + tombstone：迟到回写 DROPPED；收摊失败返回 framework_not_ready；
     框架恢复后新调用正常
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


class Records:
    """带有效性检查的结果容器（方案闭合点 3 的最小实现）。"""

    def __init__(self, check: bool):
        self.check = check
        self.recs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def begin(self, pid: str) -> None:
        with self.lock:
            self.recs[pid] = {"status": "pending", "hit": None}

    def commit(self, pid: str, hit: bool) -> str:
        with self.lock:
            r = self.recs.get(pid)
            if self.check:
                if r is None or r["status"] != "pending":
                    ev(f"commit pid={pid} -> DROPPED (status={None if r is None else r['status']})")
                    return "dropped"
            elif r is None:
                # 现状：无条件赋值，记录不存在就重建
                self.recs[pid] = {"status": "completed", "hit": hit}
                ev(f"commit pid={pid} -> ACCEPTED(recreated)")
                return "accepted"
            r["hit"] = hit
            r["status"] = "completed"
            ev(f"commit pid={pid} -> ACCEPTED")
            return "accepted"

    def invalidate(self, pid: str) -> None:
        with self.lock:
            r = self.recs.get(pid)
            if r is not None:
                r["status"] = "stale"
                ev(f"invalidate pid={pid} -> stale (tombstone kept)")

    def drop(self, pid: str) -> None:
        with self.lock:
            if self.recs.pop(pid, None) is not None:
                ev(f"drop pid={pid}")

    def sweep_tombstones(self, stopping: bool) -> None:
        """只在确认框架空闲时清理墓碑。"""
        with self.lock:
            if stopping:
                ev("sweep skipped (still stopping)")
                return
            dead = [k for k, v in self.recs.items() if v["status"] == "stale"]
            for k in dead:
                del self.recs[k]
            if dead:
                ev(f"sweep removed {dead}")

    def read(self, pid: str) -> dict | None:
        with self.lock:
            r = self.recs.get(pid)
            return dict(r) if r else None


class SlowBridge(CustomAction):
    def __init__(self, recs: Records):
        super().__init__()
        self.recs = recs

    def run(self, context, argv):
        p = json.loads(argv.custom_action_param or "{}")
        if isinstance(p, str):
            p = json.loads(p)
        pid, delay = p["probe_id"], float(p.get("delay", 0))
        ev(f"bridge ENTER pid={pid}")
        time.sleep(delay)
        ev(f"bridge WAKE pid={pid}")
        self.recs.commit(pid, True)
        ev(f"bridge EXIT pid={pid}")
        return True


def build(recs: Records, frame):
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    res.register_custom_action("A0_Slow", SlowBridge(recs))
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


def run_case(label: str, check: bool) -> None:
    print(f"\n=== {label} ===")
    ev(f"--- start check={check}")
    recs = Records(check=check)
    frame = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    tasker = build(recs, frame)

    pid = "p1"
    recs.begin(pid)
    job = tasker.post_task("A0.Slow", pipeline_override=slow(pid, 3.0))

    if wait_done(job, 0.3):
        ev("job done within budget")
    else:
        ev("超时 -> 置 stale（先失效）")
        recs.invalidate(pid)
        tasker.post_stop()
        settled = wait_done(job, 1.0)
        ev(f"收摊 settled={settled}")
        if settled:
            recs.drop(pid)
            print(f"  结局: 收摊成功，记录已删 -> 无结果; rec={recs.read(pid)}")
        else:
            print(f"  结局: framework_not_ready; 墓碑保留 -> {recs.read(pid)}")
            # 框架未就绪时不得提交新任务（实测：提交也会被忽略）
            ev("框架未就绪，本次不提交新任务")
            # 等旧回调退出
            for _ in range(80):
                if not tasker.stopping:
                    break
                time.sleep(0.1)
            ev(f"框架恢复 stopping={tasker.stopping}")
            recs.sweep_tombstones(tasker.stopping)
            print(f"  恢复后墓碑: {recs.read(pid)}")

            pid2 = "p2"
            recs.begin(pid2)
            job2 = tasker.post_task("A0.Slow", pipeline_override=slow(pid2, 0.05))
            ok2 = wait_done(job2, 3.0)
            print(f"  新调用 p2: done={ok2} rec={recs.read(pid2)}")

    for line in EV:
        print("   ", line)
    EV.clear()
    try:
        tasker.post_stop().wait()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    run_case("N 无检查（现状）：迟到回写能否重建记录", check=False)
    run_case("C 有检查 + tombstone", check=True)


if __name__ == "__main__":
    main()
