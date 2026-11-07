# -*- coding: utf-8 -*-
"""A0-1：B 链路最小验证（DbgController 单帧作截图源）。

验的是方案「闭合点 2」的每一段：
  post_task(入口, pipeline_override 带 custom_action_param)
    → CustomAction.run(context, argv)
    → context.run_recognition(节点名, 传入帧)
    → detail.hit
    → 结果按 probe_id 回传

不碰生产代码，只报告事实。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from maa.controller import DbgController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

REPO = Path(__file__).resolve().parents[3]
CORE_PIPELINE = REPO / "maaracing_master/core/resources/pipeline"
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"

RESULTS: dict[str, dict] = {}
TRACE: list[str] = []


class ProbeAction(CustomAction):
    """只读桥：取帧一次 → run_recognition → 写回本 probe_id。"""

    def __init__(self, frame: np.ndarray | None = None) -> None:
        super().__init__()
        self._frame = frame

    def run(self, context, argv):
        p = json.loads(argv.custom_action_param or "{}")
        pid, node = p.get("probe_id"), p.get("node")
        TRACE.append(f"bridge: pid={pid} node={node} ctx={type(context).__name__}")
        if context is None:
            RESULTS[pid] = {"error": "context is None"}
            return True
        try:
            detail = context.run_recognition(node, self._frame)
            hit = bool(detail.hit) if detail is not None else False
            box = getattr(detail, "box", None) if detail is not None else None
            RESULTS[pid] = {"hit": hit, "box": list(box) if box else None,
                            "detail_type": type(detail).__name__}
            TRACE.append(f"bridge: pid={pid} hit={hit} box={box}")
        except Exception as exc:  # noqa: BLE001
            RESULTS[pid] = {"error": f"{type(exc).__name__}: {exc}"}
            TRACE.append(f"bridge: pid={pid} EXC {exc}")
        return True


def build_resource(frame: np.ndarray) -> Resource:
    res = Resource()
    res.register_custom_action("A0_Probe", ProbeAction(frame))
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            (tmp / f.name).write_bytes(f.read_bytes())
    job = res.post_pipeline(str(tmp)).wait()
    print(f"[res] nodes={len(res.node_list)} post_ok={job.succeeded}")
    return res


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: step1_boot.py <frame.jpg> [node]")
        return
    frame_path = Path(sys.argv[1])
    node = sys.argv[2] if len(sys.argv) > 2 else "speedrush.驾驶页锚点"

    bgr = cv2.imread(str(frame_path), cv2.IMREAD_COLOR)
    print(f"[frame] {frame_path.name} shape={bgr.shape}")

    res = build_resource(bgr)
    ctrl = DbgController(str(frame_path))
    tasker = Tasker()
    tasker.bind(res, ctrl)
    print(f"[tasker] bind ok={tasker.inited}")

    entry = {
        "A0.Entry": {
            "recognition": {"type": "DirectHit"},
            "action": {
                "type": "Custom",
                "param": {
                    "custom_action": "A0_Probe",
                    "custom_action_param": json.dumps(
                        {"probe_id": "p1", "node": node}, ensure_ascii=False
                    ),
                },
            },
            "rate_limit": 100,
            "timeout": 3000,
        }
    }
    t0 = time.monotonic()
    tj = tasker.post_task("A0.Entry", pipeline_override=entry)
    res_detail = tj.wait()
    dt = time.monotonic() - t0

    print(f"[task] result={res_detail} elapsed={dt:.3f}s")
    for line in TRACE:
        print(" ", line)
    print("[result]", json.dumps(RESULTS, ensure_ascii=False))
    if "p1" in RESULTS and "hit" in RESULTS["p1"]:
        print(f"\nVERDICT: 链路跑通 hit={RESULTS['p1']['hit']} box={RESULTS['p1']['box']}")
    else:
        print("\nVERDICT: 链路未跑通")


if __name__ == "__main__":
    main()
