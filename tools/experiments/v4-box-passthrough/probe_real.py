#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验2：用真实帧（用户大厅截图客户区 1280x720）+ 真实节点参数定位断链环节。

A 段（无框架）：直接调 TemplateRecognizer.analyze，打印返回 box。
B 段（框架级）：真实识别桥 + 探针动作桥跑单节点图（真源参数原样，去掉 next），
   打印动作桥收到的 argv.box。

判定：
    A 返回非零框 + B 收到 (0,0,0,0) → 断点在框架传递环节（真实图形态相关）；
    A 返回 (0,0,…) 或 None          → 断点在识别侧（模板/ROI/帧）。
"""
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.custom_action import CustomAction
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_master.core.nav_graph import (
    RECOGNIZER_NAME, ACTION_NAME, TemplateRecognizer)

# 截图来源：命令行第 1 个参数，或本目录 fixtures/hall_shot.png
SHOT = (Path(sys.argv[1]) if len(sys.argv) > 1
        else Path(__file__).resolve().parent / "fixtures" / "hall_shot.png")
TPL_DIR = REPO / "maaracing_master" / "plugins" / "treasure" / "resources" / "image"
NODE_JSON = REPO / "maaracing_master" / "plugins" / "treasure" / "resources" / "pipeline" / "treasure.entry.json"
NODE_NAME = "treasure.hall_peak_appraise_card"

# 截图 1281x759 → WGC 客户区裁剪 offset=(1,38) size=(1280,720)（与真机日志一致）
_bgr = cv2.imread(str(SHOT), cv2.IMREAD_COLOR)
FRAME_BGR = _bgr[38:38 + 720, 1:1 + 1280]
assert FRAME_BGR.shape == (720, 1280, 3), FRAME_BGR.shape

node = json.loads(NODE_JSON.read_text(encoding="utf-8"))[NODE_NAME]
reco_param = node["recognition"]["param"]["custom_recognition_param"]


class FakeGraph:
    image_dirs = [TPL_DIR]

    def frame(self):
        return FRAME_BGR.copy()

    def frame_size(self):
        return (1280, 720)


class Argv:
    def __init__(self, param):
        self.node_name = NODE_NAME
        self.custom_recognition_param = json.dumps(param)
        self.image = FRAME_BGR.copy()
        self.roi = None


def phase_a():
    reco = TemplateRecognizer(FakeGraph())
    res = reco.analyze(None, Argv(reco_param))
    print(f"[A] analyze 返回 box={res.box} detail={res.detail}")
    return res.box


class StaticController(CustomController):
    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "probe-real"

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


class ProbeAction(CustomAction):
    received = None

    def run(self, context, argv):
        ProbeAction.received = tuple(argv.box)
        rd = argv.reco_detail
        print(f"[B] action 收到 box={tuple(argv.box)} "
              f"reco_detail={getattr(rd, 'name', None) or vars(rd) if rd else None}")
        return True


def phase_b():
    resource, tasker = Resource(), Tasker()
    resource.register_custom_recognition(RECOGNIZER_NAME, TemplateRecognizer(FakeGraph()))
    resource.register_custom_action(ACTION_NAME, ProbeAction())
    single = {NODE_NAME: {
        "recognition": node["recognition"],
        "action": node["action"],
        "timeout": 5000, "rate_limit": 0,
    }}
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe.json"
        f.write_text(json.dumps(single, ensure_ascii=False), encoding="utf-8")
        if resource.post_pipeline(str(f)).wait().failed:
            print("[B] post_pipeline 失败")
            return None
        tasker.bind(resource, StaticController())
        job = tasker.post_task(NODE_NAME)
        job.wait()
    return ProbeAction.received


if __name__ == "__main__":
    box_a = phase_a()
    box_b = phase_b()
    print(f"\n结论: A={box_a} B={box_b}")
