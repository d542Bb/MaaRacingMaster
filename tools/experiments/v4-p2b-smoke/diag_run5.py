# -*- coding: utf-8 -*-
"""P2b 诊断 v5（黑帧二分实验）：验证 Tasker ↔ WgcapController binding 契约。

背景：真机三炸——入口节点每 ~0.5s 一条「Starting」且全程无任何 Succeeded
通知（识别尝试从未正常完成）、永不点击、停止卡死 + 0xC0000005。
本实验用稳定黑帧（age=0，绝不抛 FrameStaleError）+ 真实 maa binding 跑真源图
入口节点：

- 若黑帧下事件序列出现「识别❌未找到」（Succeeded + hit=False）
  → binding 契约 OK（screencap 返回值被框架接受），真机问题在帧链（WGC 侧）；
- 若黑帧下依旧只有 Starting、无 Succeeded
  → binding 契约层坏（CustomController 返回值/识别回调链），继续往 binding 查；
- 顺带观察 screencap 调用节奏与 post_stop 是否干净（对照真机崩溃）。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

from maa.context import ContextEventSink
from maa.event_sink import NotificationType

from maaracing_assistant.core.nav_graph import (
    ACTION_NAME,
    RECOGNIZER_NAME,
    ClickAction,
    TemplateRecognizer,
    WgcapController,
)
from maaracing_assistant.core.pipeline_logger import PipelineLogger

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")
ENTRY = "global.hall_peak_appraise_card.rhall_to_treasure.0"


class _BlackCapture:
    """稳定黑帧桩：age=0，fid 递增，绝不抛 FrameStaleError。"""

    def __init__(self):
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.fid = 0
        self.calls = 0

    def frame_with_age(self):
        self.calls += 1
        self.fid += 1
        return (self.frame, self.fid, 0, 0.0)


class _EventCounter(ContextEventSink):
    """全量事件计数（PipelineLogger 只打部分事件，这里看全貌）。"""

    def __init__(self):
        self.events: list[str] = []

    def _rec(self, label, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        name = getattr(detail, "name", str(detail))
        self.events.append(f"{label}/{ts}/{name}")

    def on_node_recognition(self, context, noti_type, detail):
        self._rec("recog", context, noti_type, detail)

    def on_node_action(self, context, noti_type, detail):
        self._rec("action", context, noti_type, detail)


def main() -> None:
    # 1. 合并真源一次 post（同 diag_load3）
    tmp = Path(tempfile.mkdtemp(prefix="v4-run-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)

    from maa.resource import Resource
    from maa.tasker import Tasker

    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded, "真源加载失败"
    print(f"[load] ok, nodes={len(res.node_list)}")

    # 2. 组装：黑帧 controller + 真源模板目录
    from maaracing_assistant.core.nav_graph import NavGraph

    ctx = None  # 黑帧实验不触达 graph.frame() 回退（argv.image 恒有帧）
    graph = NavGraph(ctx)
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    cap = _BlackCapture()
    ctrl = WgcapController(cap)

    class _DiagRecognizer(TemplateRecognizer):
        """诊断包装：记录框架注入帧形状与 analyze 返回（钉死 Failed 语义）。"""

        def analyze(self, context, argv):
            img = getattr(argv, "image", None)
            shape = None if img is None else getattr(img, "shape", None)
            print(f"[reco] {argv.node_name} image.shape={shape}")
            r = super().analyze(context, argv)
            print(f"[reco] {argv.node_name} -> box={getattr(r, 'box', r)} "
                  f"detail={getattr(r, 'detail', '')}")
            return r

    res.register_custom_recognition(RECOGNIZER_NAME, _DiagRecognizer(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))

    counter = _EventCounter()

    # 3. Tasker 绑定 + 起跑
    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.add_context_sink(PipelineLogger())
    tasker.add_context_sink(counter)
    job = tasker.post_task(ENTRY)
    print(f"[task] posted entry={ENTRY}")

    # 4. 跑 8s 观察事件流
    for i in range(8):
        time.sleep(1)
        recog = [e for e in counter.events if e.startswith("recog/")]
        starts = sum(1 for e in recog if "/Starting/" in e)
        succs = [e for e in recog if "/Succeeded/" in e]
        print(f"[t+{i + 1}s] screencap_calls={cap.calls} recog_starting={starts} "
              f"recog_succeeded={len(succs)} events_total={len(counter.events)}")
    # 打印事件分布（前 20 条 + 类型汇总）
    from collections import Counter
    dist = Counter(e.rsplit("/", 2)[0] + "/" + e.rsplit("/", 2)[1] for e in counter.events)
    print(f"[dist] {dict(dist)}")
    for e in counter.events[:20]:
        print(f"  {e}")

    # 5. 停止观察（对照真机 stop 卡死/崩溃）
    t0 = time.perf_counter()
    tasker.post_stop()
    print(f"[stop] post_stop 返回，耗时 {time.perf_counter() - t0:.2f}s")
    time.sleep(2)
    print(f"[stop] job done = {job.status.done}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
