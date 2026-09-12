#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小实验：MaaFW 5.12.3 的 custom recognition out_box → custom action argv.box 贯通性。

背景：treasure v4 真机日志显示识别 Succeeded，但 MaaRM_Click 收到的
rect=(0,0,0,0)（点击落在屏幕左上角）。识别引擎 _best_match 的返回框
w/h 恒 ≥4，不可能产出全零框 → 断点必在「识别回传 → 框架 → 动作取框」
链路中某一环。

实验不依赖游戏/GUI：单节点图 + 固定 1280x720 注入帧 + 固定命中框，
断言动作桥收到的 box 等于识别桥返回的 box。

三种返回形态分别探测：
    ar    AnalyzeResult(box=tuple)
    bare  直接 return tuple（binding 文档示例形态）
    np    AnalyzeResult(box=numpy.int64 元素 tuple)

退出码 0 = 全绿（框架传递正常，断点在别处）；1 = 有红（复现症状）。
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker

BOX = (100, 100, 20, 20)
FRAME = np.zeros((720, 1280, 3), dtype=np.uint8)


class StaticController(CustomController):
    """固定帧注入控制器（与 WgcapController 同接口，帧为纯色常量）。"""

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "probe-box"

    def screencap(self):
        return FRAME.copy()

    def start_app(self, intent):
        return True

    def stop_app(self, intent):
        return True

    def click(self, x, y):
        return True

    def swipe(self, x1, y1, x2, y2, duration):
        return True

    def touch_down(self, contact, x, y, pressure):
        return True

    def touch_move(self, contact, x, y, pressure):
        return True

    def touch_up(self, contact):
        return True

    def click_key(self, keycode):
        return True

    def input_text(self, text):
        return True

    def key_down(self, keycode):
        return True

    def key_up(self, keycode):
        return True


class Reco(CustomRecognition):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def analyze(self, context, argv):
        if self.mode == "bare":
            return BOX
        if self.mode == "np":
            return self.AnalyzeResult(box=tuple(np.int64(v) for v in BOX),
                                      detail={"m": "np"})
        return self.AnalyzeResult(box=BOX, detail={"m": "ar"})


class Act(CustomAction):
    received = None
    reco_detail = None

    def run(self, context, argv):
        Act.received = tuple(argv.box)
        Act.reco_detail = argv.reco_detail
        return True


def run_variant(mode):
    Act.received = None
    resource, tasker = Resource(), Tasker()
    resource.register_custom_recognition("PROBE_Reco", Reco(mode))
    resource.register_custom_action("PROBE_Click", Act())
    graph = {"probe": {
        "recognition": "Custom", "custom_recognition": "PROBE_Reco",
        "action": "Custom", "custom_action": "PROBE_Click",
        "timeout": 5000, "rate_limit": 0,
    }}
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe.json"
        f.write_text(json.dumps(graph), encoding="utf-8")
        if resource.post_pipeline(str(f)).wait().failed:
            return "post_pipeline_failed"
        tasker.bind(resource, StaticController())
        job = tasker.post_task("probe")
        job.wait()
    return Act.received


if __name__ == "__main__":
    ok = True
    for mode in ("ar", "bare", "np"):
        got = run_variant(mode)
        verdict = "PASS" if got == BOX else "FAIL"
        if got != BOX:
            ok = False
        print(f"[{mode:4s}] expect {BOX} got {got} -> {verdict}")
    sys.exit(0 if ok else 1)
