#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 类实验（运行期）：Or/And 按节点名引用，运行时到底跑的是谁的识别。

静态侧已由 probe_refreuse.py 证实：any_of 里的节点名**原样保留**（解析期不展开），
所以复用发生在运行期。运行期有两种可能语义，差别很大：
  (a) 取被引用节点的**识别定义**，对当前帧重跑一遍 —— 这才是"公共锚点"能成立的依据；
  (b) 取被引用节点**上次跑出来的结果**（像 roi 字符串那样依赖执行顺序）—— 那公共锚点
      必须先被执行过才可用，形态完全不同。
本脚本用假控制器 + 会自报家门的 Custom 识别把这条钉死，并顺带看：
  R6 加载期不存在的 any_of 名字是否放行 → 运行期表现（静默跳过 or 失败）。

用法：.venv\\Scripts\\python.exe tools/experiments/pipeline-inheritance/probe_runtime_ref.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.custom_action import CustomAction  # noqa: E402
from maa.custom_recognition import CustomRecognition  # noqa: E402
from maa.controller import CustomController  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

SEEN: list[str] = []  # 自报家门的识别调用序列


class _HitReco(CustomRecognition):
    """永远命中，并把"框架用哪个节点名来调我"记下来。"""

    def analyze(self, context, argv):
        SEEN.append(f"reco:{argv.node_name}")
        return self.AnalyzeResult(box=(10, 10, 20, 20), detail={"by": argv.node_name})


class _MissReco(CustomRecognition):
    def analyze(self, context, argv):
        SEEN.append(f"miss:{argv.node_name}")
        return self.AnalyzeResult(box=None, detail={})


class _NoopAction(CustomAction):
    def run(self, context, argv) -> bool:
        SEEN.append(f"act:{argv.node_name}")
        return True


class _FakeController(CustomController):
    def __init__(self):
        super().__init__()
        self._frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def screencap(self):
        return self._frame

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "probe-runtime-ref"

    def start_app(self, intent):
        return True

    def stop_app(self):
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


def _custom(name: str) -> dict:
    return {"type": "Custom", "param": {"custom_recognition": name,
                                        "custom_recognition_param": {}}}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-rt-"))
    try:
        graph = {
            # 公共锚点：只有识别定义，无出口、无动作
            "common.signal": {"recognition": _custom("HitReco"), "attach": {"_signal": True}},
            # 模块 dwell：识别 = 按名引用公共锚点（不复制识别块）
            "mod_a.dwell": {
                "recognition": {"type": "Or", "param": {"any_of": ["common.signal"]}},
                "action": {"type": "Custom", "param": {"custom_action": "NoopAct"}},
                "next": ["mod_b.bogus"],
                "timeout": 1000,
                "attach": {"_dwell": True},
            },
            # R6：any_of 引用一个不存在的节点名
            "mod_b.bogus": {
                "recognition": {"type": "Or", "param": {"any_of": ["nope.does_not_exist"]}},
                "next": [],
                "timeout": 1000,
            },
            # R7：嵌套按名引用——被引用的节点自己的识别又是一个 Or 名字引用。
            #     boot 若要改成"引用各 dwell 节点"，走的就是这条传递链（dwell 自身识别多为 Or）。
            "mid.or": {"recognition": {"type": "Or", "param": {"any_of": ["common.signal"]}},
                       "next": [], "timeout": 1000},
            "top.or": {"recognition": {"type": "Or", "param": {"any_of": ["mid.or"]}},
                       "next": [], "timeout": 1000,
                       "attach": {"_dwell": True}},
        }
        (tmp / "g.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

        res = Resource()
        res.register_custom_recognition("HitReco", _HitReco())
        res.register_custom_recognition("MissReco", _MissReco())
        res.register_custom_action("NoopAct", _NoopAction())
        print("post_pipeline failed =", res.post_pipeline(str(tmp)).wait().failed)

        tk = Tasker()
        tk.bind(res, _FakeController())
        SEEN.clear()
        job = tk.post_task("mod_a.dwell").wait()
        print(f"task done={job.status.done} succeeded={job.succeeded}")
        print("调用序列:", " -> ".join(SEEN))
        for n in ("mod_a.dwell", "mod_b.bogus", "common.signal"):
            node = tk.get_latest_node(n)
            if node is None:
                print(f"  get_latest_node({n}) = None")
            else:
                reco = node.recognition
                print(f"  get_latest_node({n}) = completed={node.completed} "
                      f"rec_hit={None if reco is None else reco.hit} "
                      f"act_ok={None if node.action is None else node.action.success}")

        print("\n### R7 嵌套按名引用（top.or → mid.or → common.signal）")
        SEEN.clear()
        job2 = tk.post_task("top.or").wait()
        print(f"  task succeeded={job2.succeeded}  调用序列: {' -> '.join(SEEN) or '(空)'}")
        node = tk.get_latest_node("top.or")
        print("  top.or entered =", node is not None and node.completed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
