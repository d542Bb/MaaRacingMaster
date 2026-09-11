#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 类实验：节点名能不能用中文，以及"名字写错"在各字段里的失败形态。

三条要分清的问题：
  N1 中文节点名 / 中文锚点名 / 中文 any_of 引用 / 中文 roi 引用 —— 加载与运行期是否都工作？
  N2 同一个"名字不存在"的错误，写在 next 里 vs 写在 any_of 里，框架分别怎么反应？
     （这条决定"按名引用"要不要先补机检闸）
  N3 中文模板图名在加载层是否放行（运行期取不到图是另一回事）。

用法：.venv\\Scripts\\python.exe tools/experiments/pipeline-inheritance/probe_names.py
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

SEEN: list[str] = []


class _HitReco(CustomRecognition):
    def analyze(self, context, argv):
        SEEN.append(f"reco:{argv.node_name}")
        return self.AnalyzeResult(box=(10, 10, 20, 20), detail={})


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
        return "probe-names"

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


def _sig() -> dict:
    return {"recognition": {"type": "Custom", "param": {
        "custom_recognition": "HitReco", "custom_recognition_param": {}}}}


def good_graph() -> dict:
    """N1 + N3：全中文命名的一张最小图（含中文锚点名、中文 roi 引用、中文模板名）。"""
    return {
        "大厅.待机信号": {**_sig(), "attach": {"_signal": True}},
        "大厅.待机唤醒": {
            "recognition": {"type": "Or", "param": {"any_of": ["大厅.待机信号"]}},
            "action": {"type": "Custom", "param": {"custom_action": "NoopAct"}},
            "attach": {"_action": True},
        },
        "鉴宝.大厅驻留": {
            "recognition": {"type": "Or", "param": {"any_of": ["大厅.待机唤醒"]}},  # 两层中文引用
            "next": ["大厅.待机唤醒"],
            "timeout": 1000,
            "attach": {"_dwell": True},
        },
        "鉴宝.取位置": {
            "recognition": {"type": "TemplateMatch", "param": {
                "template": ["大厅卡片.png"],              # N3 中文模板名
                "roi": "大厅.待机信号"}},                   # 中文节点名当 roi
            "action": {"type": "Click", "param": {"target": "[Anchor]回大厅"}},
            "anchor": "回大厅",
            "next": [],
            "timeout": 1000,
        },
    }


def bad_next_graph() -> dict:
    """N2 对照甲：next 里写一个不存在的名字。"""
    return {"入口": {"recognition": "DirectHit", "next": ["不存在的节点"]}}


def bad_anyof_graph() -> dict:
    """N2 对照乙：any_of 里写一个不存在的名字。"""
    return {"入口": {"recognition": {"type": "Or", "param": {"any_of": ["不存在的节点"]}},
                    "next": [], "timeout": 500}}


def anchor_in_anyof_graph() -> dict:
    """N4：any_of 子项写 [Anchor]锚点名 —— 框架按节点名取 pipeline data，锚点名不是节点名。"""
    return {
        "设锚点": {"recognition": "DirectHit", "action": "DoNothing", "anchor": "回大厅",
                  "next": ["借锚点"]},
        "借锚点": {"recognition": {"type": "Or", "param": {"any_of": ["[Anchor]回大厅"]}},
                  "next": [], "timeout": 500},
    }


def load(tmp: Path, graph: dict) -> tuple[Resource, bool]:
    for old in tmp.glob("*.json"):
        old.unlink()
    (tmp / "g.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    res = Resource()
    res.register_custom_recognition("HitReco", _HitReco())
    res.register_custom_action("NoopAct", _NoopAction())
    failed = res.post_pipeline(str(tmp)).wait().failed
    return res, failed


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-names-"))
    try:
        res, failed = load(tmp, good_graph())
        print(f"[N1/N3] 全中文命名图 post_pipeline failed={failed}")
        print(f"        node_list={sorted(res.node_list)}")
        for n in ("大厅.待机唤醒", "鉴宝.大厅驻留", "鉴宝.取位置"):
            d = res.get_node_data(n)
            print(f"        {n}: reco={json.dumps(d.get('recognition'), ensure_ascii=False)}")
            print(f"        {' ' * len(n)}  anchor={d.get('anchor')} next={d.get('next')}")

        tk = Tasker()
        tk.bind(res, _FakeController())
        SEEN.clear()
        job = tk.post_task("鉴宝.大厅驻留").wait()
        print(f"[N1]  运行期（入口=鉴宝.大厅驻留）succeeded={job.succeeded}  调用序列: {' -> '.join(SEEN)}")

        print("\n[N2] 「名字写错」的两种失败形态对照：")
        _r, f1 = load(tmp, bad_next_graph())
        print(f"     写在 next 里   → post_pipeline failed={f1}   （加载期就拦）")
        _r, f2 = load(tmp, bad_anyof_graph())
        print(f"     写在 any_of 里 → post_pipeline failed={f2}   （放行=静默）")
        if not f2:
            tk2 = Tasker()
            tk2.bind(_r, _FakeController())
            SEEN.clear()
            j2 = tk2.post_task("入口").wait()
            print(f"     any_of 那条运行期 succeeded={j2.succeeded}（错误只在这里露头）")

        print("\n[N3] 中文模板名：加载期放行≠运行期可用（模板取不到属另一通路，本脚本不判定）")

        print("\n[N4] any_of 子项写 [Anchor]锚点名：")
        res4, f4 = load(tmp, anchor_in_anyof_graph())
        print(f"     post_pipeline failed={f4}")
        if not f4:
            tk4 = Tasker()
            tk4.bind(res4, _FakeController())
            SEEN.clear()
            j4 = tk4.post_task("设锚点").wait()
            node = tk4.get_latest_node("借锚点")
            print(f"     任务 succeeded={j4.succeeded} 调用序列: {' -> '.join(SEEN) or '(空)'}  "
                  f"借锚点是否被进入={node is not None}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
