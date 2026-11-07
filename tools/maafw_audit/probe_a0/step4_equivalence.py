# -*- coding: utf-8 -*-
"""A0-2：同一帧输入下 run() 路径与 probe() 路径的逐帧等价性。

两条路径都用**项目真实** TemplateRecognizer：
  R（run 语义）  post_task(目标节点, override 只改 action) → 框架识别轮 → 命中才执行标记动作
  P（probe 语义）post_task(入口, override 带 probe_id) → 桥 → context.run_recognition(节点, 同一帧)

controller 两路都喂同一张帧，帧身份由脚本保证唯一。
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
IMAGE_DIRS = [
    REPO / "maaracing_master/plugins/speedrush/resources/image",
    REPO / "maaracing_master/core/resources/image",
]
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")

PROBE_RESULTS: dict[str, dict] = {}
RUN_MARKS: list[dict] = []


class FakeGraph:
    def __init__(self) -> None:
        self.image_dirs = [d for d in IMAGE_DIRS if d.is_dir()]
        self._last_frame = None
        self.nav_events: list[dict] = []

    def frame(self):
        return self._last_frame

    def _log(self, msg: str, level: str = "INFO") -> None:
        pass

    def _publish_nav(self, name: str, **kw) -> None:
        self.nav_events.append({"name": name, **kw})


class FrameController(CustomController):
    def __init__(self) -> None:
        self.frame = np.zeros((720, 1280, 3), np.uint8)
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


class ProbeAction(CustomAction):
    def __init__(self) -> None:
        super().__init__()
        self.frame: np.ndarray | None = None

    def run(self, context, argv):
        raw = argv.custom_action_param
        p = json.loads(raw) if raw else {}
        if isinstance(p, str):
            p = json.loads(p)
        pid, node = p.get("probe_id"), p.get("node")
        detail = context.run_recognition(node, self.frame) if context is not None else None
        box = getattr(detail, "box", None) if detail is not None else None
        PROBE_RESULTS[pid] = {
            "hit": bool(detail.hit) if detail is not None else False,
            "box": [int(v) for v in box] if box is not None else None,
        }
        return True


class MarkAction(CustomAction):
    """run 路径的命中标记：只有识别命中才会走到这里。"""

    def run(self, context, argv):
        RUN_MARKS.append({"node": argv.node_name, "box": [int(v) for v in argv.box]})
        return True


def build_tasker(graph: FakeGraph, bridge: ProbeAction) -> tuple[Tasker, FrameController]:
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(graph))
    res.register_custom_action("A0_Probe", bridge)
    res.register_custom_action("A0_Mark", MarkAction())
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            (tmp / f.name).write_bytes(f.read_bytes())
    res.post_pipeline(str(tmp)).wait()
    ctrl = FrameController()
    tasker = Tasker()
    tasker.bind(res, ctrl)
    return tasker, ctrl


def run_path(tasker: Tasker, ctrl: FrameController, node: str, frame: np.ndarray) -> dict:
    """R：框架识别轮。override 只换 action，识别定义保持真源。"""
    RUN_MARKS.clear()
    ctrl.frame = frame
    entry = {node: {"action": {"type": "Custom", "param": {"custom_action": "A0_Mark"}}}}
    tj = tasker.post_task(node, pipeline_override=entry)
    det = tj.wait()
    status = getattr(det, "status", None)
    ok = bool(getattr(status, "succeeded", False)) if status is not None else None
    return {"hit": bool(RUN_MARKS), "box": RUN_MARKS[0]["box"] if RUN_MARKS else None,
            "status_ok": ok}


def probe_path(tasker: Tasker, ctrl: FrameController, bridge: ProbeAction,
               node: str, frame: np.ndarray, pid: str) -> dict:
    ctrl.frame = frame
    bridge.frame = frame
    entry = {
        "A0.Entry": {
            "recognition": {"type": "DirectHit"},
            "action": {"type": "Custom", "param": {
                "custom_action": "A0_Probe",
                "custom_action_param": {"probe_id": pid, "node": node},
            }},
            "rate_limit": 100,
            "timeout": 3000,
        }
    }
    tasker.post_task("A0.Entry", pipeline_override=entry).wait()
    return PROBE_RESULTS.pop(pid, {"hit": False, "box": None})


def main() -> None:
    argv = sys.argv[1:]
    lo, hi = (int(argv[0]), int(argv[1])) if len(argv) >= 2 else (860, 940)
    nodes = ["speedrush.驾驶页锚点", "speedrush.商店横幅锚点"]

    graph = FakeGraph()
    bridge = ProbeAction()
    tasker, ctrl = build_tasker(graph, bridge)

    rows = [json.loads(line) for line in (NAV / "frames.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    sel = [r for r in rows if lo <= r["seq"] <= hi]

    stats = {n: {"both_hit": 0, "both_miss": 0, "diff": 0} for n in nodes}
    diffs: list[str] = []
    hit_ranges: dict[str, list[int]] = {n: [] for n in nodes}

    t0 = time.monotonic()
    for r in sel:
        p = NAV / r["file"]
        if not p.is_file():
            continue
        frame = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if frame is None:
            continue
        for node in nodes:
            rr = run_path(tasker, ctrl, node, frame)
            pp = probe_path(tasker, ctrl, bridge, node, frame, f"{r['seq']}-{node}")
            same = rr["hit"] == pp["hit"] and (rr["box"] == pp["box"] if rr["hit"] else True)
            if rr["hit"] and pp["hit"]:
                stats[node]["both_hit"] += 1
                hit_ranges[node].append(r["seq"])
            elif not rr["hit"] and not pp["hit"]:
                stats[node]["both_miss"] += 1
            else:
                stats[node]["diff"] += 1
            if not same:
                diffs.append(f"seq={r['seq']} {node} run={rr} probe={pp}")

    print(f"[范围] {lo}~{hi} 帧数 {len(sel)} 耗时 {time.monotonic()-t0:.1f}s")
    for node in nodes:
        s = stats[node]
        print(f"\n{node}: both_hit={s['both_hit']} both_miss={s['both_miss']} diff={s['diff']}")
        if hit_ranges[node]:
            seqs = hit_ranges[node]
            print(f"  命中帧: {seqs[0]}~{seqs[-1]}（共 {len(seqs)}）")
    print(f"\n不一致条数: {len(diffs)}")
    for d in diffs[:10]:
        print("  ", d)


if __name__ == "__main__":
    main()
