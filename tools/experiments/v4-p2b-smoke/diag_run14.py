# -*- coding: utf-8 -*-
"""P2b 诊断 v14（box 契约实验·修订档）：MaaFW rect 契约 = (x, y, w, h) 实证。

初版教训：用例 box=(100,200,900,500) 的 w/h 槽数值恰好合法（x+w=1000≤1280、
y+h=700≤720）未触发裁剪，得出「原样透传」的半错结论。真机 (999,591,1103,633)
越界被 clip 成 (999,591,281,129) 才暴露契约违规——框架按 (x,y,w,h) 归一，
x+w 超帧宽即裁剪。本档两节点定案：
  probe_a：契约形态 (999,591,104,42)（卡片真实 rect）→ 应原样 + 中心 (0.821,0.850)
  probe_b：越界形态 (999,591,400,400) → 应被 clip 到 (999,591,281,129)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.resource import Resource
from maa.tasker import Tasker

WANT = {"probe_a": (999, 591, 104, 42), "probe_b": (999, 591, 400, 400)}
OBSERVED: list = []


class _FixedBox(CustomRecognition):
    def analyze(self, context, argv):
        return self.AnalyzeResult(box=WANT[argv.node_name], detail={"probe": True})


class _ProbeAction(CustomAction):
    def run(self, context, argv):
        b = tuple(argv.box)
        rx, ry, rw, rh = b
        cx, cy = (rx + rw / 2) / 1280, (ry + rh / 2) / 720
        OBSERVED.append((argv.node_name, b, (round(cx, 3), round(cy, 3))))
        return True


class _BlackCap:
    def frame_with_age(self):
        f = np.zeros((720, 1280, 3), dtype=np.uint8)
        return (f, 1, 0, 0.0)


def main() -> None:
    from maaracing_assistant.core.nav_graph import WgcapController

    res = Resource()
    graph_json = {
        "probe_a": {
            "recognition": "Custom", "custom_recognition": "PROBE_Reco",
            "action": "Custom", "custom_action": "PROBE_Act",
            "next": ["probe_b"],
        },
        "probe_b": {
            "recognition": "Custom", "custom_recognition": "PROBE_Reco",
            "action": "Custom", "custom_action": "PROBE_Act",
        },
    }
    tmp = Path(tempfile.mkdtemp(prefix="v4-box-"))
    (tmp / "p.json").write_text(json.dumps(graph_json), encoding="utf-8")
    assert res.post_pipeline(str(tmp)).wait().succeeded

    res.register_custom_recognition("PROBE_Reco", _FixedBox())
    res.register_custom_action("PROBE_Act", _ProbeAction())

    ctrl = WgcapController(_BlackCap())
    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.post_task("probe_a").wait()

    print(f"[want] a={WANT['probe_a']}（契约） b={WANT['probe_b']}（越界）")
    for name, got, center in OBSERVED:
        print(f"[observed] {name}: argv.box={got} 中心归一化={center}")
    a = next((o for o in OBSERVED if o[0] == "probe_a"), None)
    b = next((o for o in OBSERVED if o[0] == "probe_b"), None)
    if a and a[1] == WANT["probe_a"]:
        print("[verdict] 契约形态 (x,y,w,h) 原样到达，中心落点 = 卡片中心 ✓")
    if b:
        clipped = b[1] == (999, 591, 281, 129)
        print(f"[verdict] 越界 w/h 被框架 clip 到帧边界: {clipped}"
              f"（实证 rect 语义=(x,y,w,h)，x2/y2 混填会在越界时错位）")


if __name__ == "__main__":
    main()
