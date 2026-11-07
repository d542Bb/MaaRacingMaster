# -*- coding: utf-8 -*-
"""诊断：`post_task` 的耗时到底花在哪。

三种调用，同一节点/同一帧：
  A 已注册节点，无 override          post_task("speedrush.驾驶页锚点")
  B 已注册节点，override 只改 action  post_task(节点, {节点: {action: ...}})
  C 未注册节点，override 新建         post_task("MaaRM_Probe.Entry", {入口: {...}})

若 C 显著慢于 A，说明开销在「override 新建节点」，与识别本身无关——
那 probe 就该改成预注册入口节点，而不是每次 override 建。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import cv2

from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.core.nav_graph import TemplateRecognizer  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
CORE_PIPELINE = REPO / "maaracing_master/core/resources/pipeline"
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"
IMAGE_DIRS = [REPO / "maaracing_master/plugins/speedrush/resources/image",
              REPO / "maaracing_master/core/resources/image"]
NAV = Path(os.environ.get("MAAR_A0_NAV")
         or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
         / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")
NODE = "speedrush.驾驶页锚点"


class FakeGraph:
    def __init__(self):
        self.image_dirs = [d for d in IMAGE_DIRS if d.is_dir()]
        self._last_frame = None

    def frame(self): return self._last_frame
    def _log(self, msg, level="INFO"): pass
    def _publish_nav(self, name, **kw): pass


class FrameController(CustomController):
    def __init__(self, frame):
        self.frame = frame
        super().__init__()

    def connect(self): return True
    def request_uuid(self): return "a0"
    def start_app(self, i): return True
    def stop_app(self, i): return True
    def screencap(self): return self.frame
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


CALLS: list[str] = []


class Noop(CustomAction):
    def run(self, context, argv):
        CALLS.append(argv.node_name)
        return True


def build(frame):
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", TemplateRecognizer(FakeGraph()))
    res.register_custom_action("A0_Noop", Noop())
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if d.is_dir():
            for f in d.glob("*.json"):
                (tmp / f.name).write_bytes(f.read_bytes())
    # 预注册的探针入口：**分两种落法**，隔离「写在哪个文件」这个变量
    entry_def = {
        "recognition": {"type": "DirectHit"},
        "action": {"type": "Custom", "param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "", "node": ""}}},
        "rate_limit": 0,
        "timeout": 1000,
    }
    # 落法一：独立文件
    (tmp / "probe_entry.json").write_text(json.dumps({
        "MaaRM_Probe.Entry": entry_def,
        "A0.ProbeEntry": dict(entry_def),
    }, ensure_ascii=False), encoding="utf-8")
    # 落法二：塞进 speedrush.json（与快节点同一文件）
    sp = tmp / "speedrush.json"
    data = json.loads(sp.read_text(encoding="utf-8"))
    data["speedrush.ProbeEntry"] = dict(entry_def)
    # 落法三：带 attach（与「驾驶页锚点」同形）
    data["speedrush.ProbeEntryAttach"] = dict(entry_def)
    data["speedrush.ProbeEntryAttach"]["attach"] = {
        "_page": "x", "_label": "y", "_owner": "z", "_anchor_only": True}
    sp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res.post_pipeline(str(tmp)).wait()
    ctrl = FrameController(frame)
    tasker = Tasker()
    tasker.bind(res, ctrl)
    nodes = list(res.node_list)
    print(f"[res] 节点数 {len(nodes)}；含探针入口: {'MaaRM_Probe.Entry' in nodes}")
    if "MaaRM_Probe.Entry" not in nodes:
        print("  可疑：预注册的入口节点不在 node_list，override 会走「节点不存在」路径")
        print("  样例节点名:", nodes[:4])
    return tasker


def timeit(fn, n=5) -> list[float]:
    out = []
    for _ in range(n):
        t0 = time.monotonic()
        fn()
        out.append((time.monotonic() - t0) * 1000)
    return out


def main() -> None:
    bgr = cv2.imread(str(NAV / "000895.jpg"), cv2.IMREAD_COLOR)
    tasker = build(bgr)

    a = lambda: tasker.post_task(NODE).wait()  # noqa: E731
    b = lambda: tasker.post_task(NODE, pipeline_override={  # noqa: E731
        NODE: {"action": {"type": "Custom", "param": {"custom_action": "A0_Noop"}}}}).wait()
    c = lambda: tasker.post_task("MaaRM_Probe.Entry", pipeline_override={  # noqa: E731
        "MaaRM_Probe.Entry": {
            "recognition": {"type": "DirectHit"},
            "action": {"type": "Custom", "param": {"custom_action": "A0_Noop"}},
            "rate_limit": 0, "timeout": 1000,
        }}).wait()

    for label, fn in (("A 已注册/无 override", a),
                      ("B 已注册/改 action", b),
                      ("C 未注册/override 新建", c)):
        CALLS.clear()
        ms = timeit(fn)
        ms_sorted = sorted(ms)
        print(f"{label:26s} 中位 {ms_sorted[len(ms)//2]:6.0f}ms  "
              f"范围 {min(ms):.0f}~{max(ms):.0f}ms  "
              f"action 执行 {len(CALLS)}/5  原始 {[round(x) for x in ms]}")

    # 再测一次：C 的入口节点已在上一步被 override 过，看是否变快（框架是否缓存）
    ms = timeit(c)
    print(f"{'C 复测（同入口再跑）':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # D：入口节点**预注册**，只 override 参数（修复方案）
    d = lambda: tasker.post_task("MaaRM_Probe.Entry", pipeline_override={  # noqa: E731
        "MaaRM_Probe.Entry": {"action": {"param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    CALLS.clear()
    ms = timeit(d)
    print(f"{'D 预注册入口/只改参数':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"action 执行 {len(CALLS)}/5")

    # E：同一入口，但 timeout 从 1000 降到 400（对齐 A 的节点属性）
    e = lambda: tasker.post_task("MaaRM_Probe.Entry", pipeline_override={  # noqa: E731
        "MaaRM_Probe.Entry": {"timeout": 400, "action": {"param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    ms = timeit(e)
    print(f"{'E 入口 timeout=400':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # F：用**已注册的模板节点**当入口，完整覆盖 action（probe 的真实用法）
    f = lambda: tasker.post_task(NODE, pipeline_override={  # noqa: E731
        NODE: {"action": {"type": "Custom", "param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    ms = timeit(f)
    print(f"{'F 模板节点当入口/覆盖 action':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # G：同一模板节点，但把 recognition 换成 DirectHit —— 隔离「识别类型」变量
    g = lambda: tasker.post_task(NODE, pipeline_override={  # noqa: E731
        NODE: {"recognition": {"type": "DirectHit"},
               "action": {"type": "Custom", "param": {
                   "custom_action": "A0_Noop",
                   "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    ms = timeit(g)
    print(f"{'G 模板节点改 DirectHit':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # H：同定义、不同节点名（避开与自定义动作名 MaaRM_Probe 的前缀冲突）
    h = lambda: tasker.post_task("A0.ProbeEntry", pipeline_override={  # noqa: E731
        "A0.ProbeEntry": {"action": {"param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    ms = timeit(h)
    print(f"{'H 换名 A0.ProbeEntry':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # I：入口节点写在 speedrush.json 里（与 A/B/F/G 同一文件）
    i = lambda: tasker.post_task("speedrush.ProbeEntry", pipeline_override={  # noqa: E731
        "speedrush.ProbeEntry": {"action": {"param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    ms = timeit(i)
    print(f"{'I 入口写进 speedrush.json':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # J：同一入口节点，但 override 给**完整 action**（含 type）
    j = lambda: tasker.post_task("speedrush.ProbeEntry", pipeline_override={  # noqa: E731
        "speedrush.ProbeEntry": {"action": {"type": "Custom", "param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    ms = timeit(j)
    print(f"{'J 完整 action 覆盖':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # A2：把最初那个 0ms 的调用放到最后再跑一次——检验是否被 C 污染了 Tasker 状态
    ms = timeit(a)
    print(f"{'A2 最初那个调用复测':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"原始 {[round(x) for x in ms]}")

    # K：入口带 attach（与「驾驶页锚点」同形）
    k = lambda: tasker.post_task("speedrush.ProbeEntryAttach", pipeline_override={  # noqa: E731
        "speedrush.ProbeEntryAttach": {"action": {"param": {
            "custom_action": "A0_Noop",
            "custom_action_param": {"probe_id": "p1", "node": NODE}}}}}).wait()
    CALLS.clear()
    ms = timeit(k)
    print(f"{'K 入口带 attach':26s} 中位 {sorted(ms)[len(ms)//2]:6.0f}ms  "
          f"action 执行 {len(CALLS)}/5  原始 {[round(x) for x in ms]}")


if __name__ == "__main__":
    main()
