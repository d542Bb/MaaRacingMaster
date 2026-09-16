# -*- coding: utf-8 -*-
"""P2b 诊断 v6（通道序探针）：真实彩色帧过「WgcapController→框架→analyze」往返。

黑帧实验（diag_run5）证明了 binding 契约、节奏、stop 全部正常，但黑帧三通道
相同，对「通道序反转」免疫——v4 帧链比 v3 直读多两次反转（screencap RGB→BGR
入框架、analyze BGR→RGB 出框架），序保真从未被真实彩色帧验证过。

探针设计（同一 Tasker，分两轮换帧）：
  轮 1 纯色帧 RGB(10,20,30)：analyze 打印 argv.image[0,0] 三通道值——
      序保真 → (30,20,10)（BGR）；若框架再反转 → (10,20,30)。
  轮 2 真帧（v3 大厅 raw 首帧）：生产 image_dirs + 入口节点真参数走
      TemplateRecognizer.analyze——命中则序链自洽（真机问题另有其因）；
      score 崩塌则序问题实锤。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, ".")

from maa.context import ContextEventSink
from maa.event_sink import NotificationType
from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_assistant.core.nav_graph import (
    ACTION_NAME,
    RECOGNIZER_NAME,
    ClickAction,
    NavGraph,
    TemplateRecognizer,
    WgcapController,
)

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")
ENTRY = "global.hall_peak_appraise_card.rhall_to_treasure.0"
DEBUG_ROOT = Path.home() / "AppData/Roaming/MaaRacingAssistant/debug/treasure"


def find_hall_frame() -> Path:
    """v3 会话首帧（启动即大厅）。"""
    for s in sorted(DEBUG_ROOT.iterdir(), reverse=True):
        raw = s / "raw"
        if not raw.is_dir():
            continue
        for p in sorted(raw.glob("*_raw.jpg")):
            return p
    raise FileNotFoundError("找不到任何 *_raw.jpg")


class _SwappableCapture:
    """可控帧桩：运行时可切换内容（纯色 → 真帧）。"""

    def __init__(self, frame: np.ndarray):
        self.frame = frame
        self.fid = 0

    def swap(self, frame: np.ndarray) -> None:
        self.frame = frame

    def frame_with_age(self):
        self.fid += 1
        return (self.frame, self.fid, 0, 0.0)


PROBE: dict = {}


class _ProbeRecognizer(TemplateRecognizer):
    """探针包装：记录框架注入帧的通道值与匹配结果。"""

    def analyze(self, context, argv):
        img = argv.image
        px = None if img is None or img.size == 0 else img[0, 0].tolist()
        PROBE.setdefault("frames", []).append((argv.node_name, img.shape if img is not None else None, px))
        r = super().analyze(context, argv)
        PROBE.setdefault("results", []).append((argv.node_name, getattr(r, "box", None), getattr(r, "detail", None)))
        return r


class _Counter(ContextEventSink):
    def __init__(self):
        self.n_start = 0
        self.n_succ = 0
        self.n_fail = 0

    def on_node_recognition(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        if ts == "Starting":
            self.n_start += 1
        elif ts == "Succeeded":
            self.n_succ += 1
        else:
            self.n_fail += 1


def main() -> None:
    hall = find_hall_frame()
    print(f"[frame] 真帧素材: {hall}")
    hall_bgr = cv2.imread(str(hall), cv2.IMREAD_COLOR)
    hall_rgb = cv2.cvtColor(hall_bgr, cv2.COLOR_BGR2RGB)  # get_latest_rgb 契约
    solid_rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
    solid_rgb[:, :] = (10, 20, 30)  # 三通道互异：序反转可见

    tmp = Path(tempfile.mkdtemp(prefix="v4-probe-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)

    res = Resource()
    assert res.post_pipeline(str(tmp)).wait().succeeded
    print(f"[load] ok, nodes={len(res.node_list)}")

    graph = NavGraph(None)
    graph.image_dirs.extend([
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ])
    cap = _SwappableCapture(solid_rgb)
    ctrl = WgcapController(cap)
    res.register_custom_recognition(RECOGNIZER_NAME, _ProbeRecognizer(graph))
    res.register_custom_action(ACTION_NAME, ClickAction(graph))

    counter = _Counter()
    tasker = Tasker()
    tasker.bind(res, ctrl)
    tasker.add_context_sink(counter)
    tasker.post_task(ENTRY)

    # 轮 1：纯色帧 2s
    time.sleep(2.0)
    # 轮 2：切真帧再 2s
    cap.swap(hall_rgb)
    time.sleep(2.0)
    tasker.post_stop()
    time.sleep(0.5)
    shutil.rmtree(tmp, ignore_errors=True)

    frames = PROBE.get("frames", [])
    results = PROBE.get("results", [])
    print(f"[probe] 识别调用 {len(frames)} 次 | 事件 start/succ/fail = "
          f"{counter.n_start}/{counter.n_succ}/{counter.n_fail}")
    print("[probe] 注入帧通道值（按时间序，前 3 + 后 3）:")
    for row in frames[:3] + frames[-3:]:
        print(f"   shape={row[1]} argv.image[0,0]={row[2]}")
    print("[probe] analyze 结果（前 3 + 后 3）:")
    for row in results[:3] + results[-3:]:
        print(f"   box={row[1]} detail={row[2]}")

    solid = [r for r in frames if r[2] == [30, 20, 10]]
    flipped = [r for r in frames if r[2] == [10, 20, 30]]
    print(f"[verdict] argv.image 序保真(BGR 30,20,10)={len(solid)} 次 | "
          f"被框架再反转(10,20,30)={len(flipped)} 次")
    hit = [r for r in results if r[1] is not None]
    print(f"[verdict] 真帧 analyze 命中 {len(hit)} 次 / 共 {len(results)} 次")
    if hit:
        print(f"[verdict] 命中样例: box={hit[0][1]} detail={hit[0][2]}")


if __name__ == "__main__":
    main()
