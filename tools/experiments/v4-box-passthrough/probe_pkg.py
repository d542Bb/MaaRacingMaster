#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验C：打包版 runtime（numpy 缺 ctypeslib）下，识别回调抛异常时框架行为。

复现真机症状的最后一环：binding 在调用用户 analyze 之前先执行
ImageBuffer(c_image).get()（custom_recognition.py:121），打包版 numpy 缺
ctypeslib → 回调在用户代码之前崩溃。观察：
  1) 框架把该节点判为识别成功还是失败；
  2) 若动作仍执行，argv.box 是什么。
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

BOX = (999, 591, 104, 42)
FRAME_BGR = np.full((720, 1280, 3), 128, dtype=np.uint8)


class StaticController(CustomController):
    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "probe-pkg"

    def screencap(self):
        return FRAME_BGR.copy()

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
    def analyze(self, context, argv):
        print("[reco] analyze 被调用（ImageBuffer.get 未崩溃）")
        return self.AnalyzeResult(box=BOX, detail={"probe": 1})


class Act(CustomAction):
    received = "NOT_CALLED"

    def run(self, context, argv):
        Act.received = tuple(argv.box)
        print(f"[act] 收到 box={tuple(argv.box)}")
        return True


def main():
    resource, tasker = Resource(), Tasker()
    resource.register_custom_recognition("PROBE_Reco", Reco())
    resource.register_custom_action("PROBE_Click", Act())
    graph = {"probe": {
        "recognition": "Custom", "custom_recognition": "PROBE_Reco",
        "action": "Custom", "custom_action": "PROBE_Click",
        "timeout": 3000, "rate_limit": 0,
    }}
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe.json"
        f.write_text(json.dumps(graph), encoding="utf-8")
        if resource.post_pipeline(str(f)).wait().failed:
            print("post_pipeline 失败")
            return 2
        tasker.bind(resource, StaticController())
        job = tasker.post_task("probe")
        job.wait()
        print(f"task succeeded={job.succeeded} action_box={Act.received}")
        print("症状复现" if Act.received == (0, 0, 0, 0) else
              ("动作未执行" if Act.received == "NOT_CALLED" else "box 正常"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
