# -*- coding: utf-8 -*-
"""v4 节拍离线探针：rate_limit 到底约束谁 + 一轮内是否取了两次帧。

为什么要测（口径互相打架，禁止靠猜）：
  - 真源声明 `treasure.policy_loop.rate_limit = 300`；
  - memory-ws 的 P2b 定案却记着「v4 实际帧间隔约 125ms」——300 是**下限**，
    两者不可能同时成立；
  - 协议原文：`rate_limit` = 「每轮识别 next + interrupt 最低消耗」，
    伪码 `while(!timeout) { foreach(next + interrupt); sleep_until(rate_limit); }`。
    而 policy_loop 的 `next` 为空（靠父级 `jump_back` 回弹），
    挂在这个节点上的 300 究竟约束什么，只能实测。

两条判据：
  Q1 相邻决策帧（policy run）/ 相邻截图轮的间隔由谁决定？
  Q2 框架「为选中 policy_loop 而截图的那一帧」与「决策段在 action 内自己再取的帧」
     帧号是否相同？不同 ⇒ 一轮内两个帧时刻：识别基于 A 帧、决策基于 B 帧。

两组路径分开测（关键，否则结论会说过头）：
  · 回弹闭环（policy_hits=True）：dwell.next 全 miss → 命中 jump_back 兜底位 →
    跑决策 → 回弹到 dwell.next。**这是 v4 局内每帧重判的真实形态。**
  · 驻留等待（policy_hits=False）：dwell.next 全部永不命中 → dwell 进入
    「timeout=-1 的空等轮询」。**这才是 rate_limit 伪码的设计主场。**

零真机依赖：不连游戏窗口、不读仓库真源，合成与项目同形态的最小图——
  exp.boot   next=[exp.dwell]                                  rate_limit=50, timeout=-1
  exp.dwell  next=[exp.act, {exp.policy_loop, jump_back:true}] rate_limit=RATE_DWELL, timeout=-1
  exp.act    recognition=Custom EXP_NoHit                      永不命中，逼出兜底位
  exp.policy_loop action=Custom EXP_Policy, next=[]             rate_limit=RATE_POLICY,
                pre/post_delay=0, timeout=-1, recognition=DirectHit 或 EXP_NoHit

截图侧与 core/nav_graph.py 的 WgcapController 同形（读一份"60fps 虚拟缓存"、
过期抛错、永不回退），因此 Q2 的结论可迁移到 v4 通路。

用法（仓库根 .venv，3.11）：
    .venv\\Scripts\python.exe tools\\experiments\\v4-frame-pacing\\pacing_probe.py [预算秒数]
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.resource import Resource
from maa.tasker import Tasker

RECO_NOHIT = "EXP_NoHit"
ACT_POLICY = "EXP_Policy"
VIRTUAL_FPS = 60.0
STALE_MS = 500.0
DECIDE_MS = 105.0  # 复现真机决策段一次 OCR（9-ROI ~105ms）的取帧间隔


class VirtualCapture:
    """按墙钟推导帧号的 60fps 虚拟缓存（等价 WgcCapture.get_latest_rgb 的读侧语义）。"""

    FRAME_NS = int(1e9 / VIRTUAL_FPS)

    def __init__(self) -> None:
        self._t0 = time.perf_counter_ns()
        self._frame = np.zeros((720, 1280, 3), dtype=np.uint8)  # RGB

    def frame_with_age(self):
        fid = int((time.perf_counter_ns() - self._t0) // self.FRAME_NS)
        ts = self._t0 + fid * self.FRAME_NS
        return self._frame, fid, ts, (time.perf_counter_ns() - ts) / 1e6

    def screenshot(self):
        return self.frame_with_age()[0]


class ProbeController(CustomController):
    """与 WgcapController 同形：只读中心缓存，缺帧/过期即抛，永不回退 MAA 截图。"""

    def __init__(self, capture: VirtualCapture) -> None:
        super().__init__()
        self._capture = capture
        self.last_reco_fid = -1
        self.last_reco_ts_ns = 0
        self.shot_ts: list[int] = []  # 每轮框架截图时刻（驻留等待路径的观测量）

    def screencap(self):
        frame, fid, ts, age = self._capture.frame_with_age()
        if frame is None or age > STALE_MS:
            raise RuntimeError(f"帧缺失或过期：age={age:.0f}ms")
        self.last_reco_fid = fid
        self.last_reco_ts_ns = ts
        self.shot_ts.append(time.perf_counter_ns())
        return np.ascontiguousarray(frame[:, :, ::-1])  # RGB → BGR（框架契约）

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "exp-v4-frame-pacing"

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


class NoHitRecognition(CustomRecognition):
    def analyze(self, context, argv):
        return None


class PolicyAction(CustomAction):
    """一次 run = 一帧决策：记录"为选中我而截的帧"与"决策段自己再取的帧"。"""

    def __init__(self, capture: VirtualCapture, controller: ProbeController,
                 decide_ms: float = DECIDE_MS) -> None:
        super().__init__()
        self._capture = capture
        self._ctrl = controller
        self._decide_ms = decide_ms
        self.runs: list[dict] = []

    def run(self, context, argv) -> bool:
        now_ns = time.perf_counter_ns()
        reco_fid = self._ctrl.last_reco_fid
        reco_age_ms = (now_ns - self._ctrl.last_reco_ts_ns) / 1e6 if reco_fid >= 0 else float("nan")
        fid_before = self._capture.frame_with_age()[1]
        if self._decide_ms > 0:
            time.sleep(self._decide_ms / 1000.0)  # 决策段耗时（OCR/YOLO 的时间当量）
        fid_after = self._capture.frame_with_age()[1]
        self.runs.append({
            "t_ns": now_ns,
            "reco_fid": reco_fid,
            "reco_age_ms": reco_age_ms,
            "decision_fid": fid_before,
            "fid_drift": fid_before - reco_fid,
            "work_fid_drift": fid_after - fid_before,
        })
        return True


def build_graph(dwell_rate: int, policy_rate: int, dwell_delays: int, policy_hits: bool) -> dict:
    dwell = {
        "recognition": {"type": "DirectHit", "param": {}},
        "action": {"type": "DoNothing", "param": {}},
        "next": [
            "exp.act",
            {"name": "exp.policy_loop", "jump_back": True},
        ],
        "rate_limit": dwell_rate,
        "timeout": -1,
    }
    if dwell_delays >= 0:
        dwell["pre_delay"] = dwell_delays
        dwell["post_delay"] = dwell_delays
    policy_reco = ({"type": "DirectHit", "param": {}} if policy_hits else
                   {"type": "Custom", "param": {"custom_recognition": RECO_NOHIT}})
    return {
        "exp.boot": {
            "recognition": {"type": "DirectHit", "param": {}},
            "action": {"type": "DoNothing", "param": {}},
            "next": ["exp.dwell"],
            "rate_limit": 50,
            "timeout": -1,
        },
        "exp.dwell": dwell,
        "exp.act": {
            "recognition": {"type": "Custom", "param": {"custom_recognition": RECO_NOHIT}},
            "action": {"type": "DoNothing", "param": {}},
            "next": [],
        },
        "exp.policy_loop": {
            "recognition": policy_reco,
            "action": {"type": "Custom", "param": {"custom_action": ACT_POLICY}},
            "next": [],
            "rate_limit": policy_rate,
            "pre_delay": 0,
            "post_delay": 0,
            "timeout": -1,
        },
    }


# (标签, dwell.rate_limit, policy_loop.rate_limit, dwell 的 pre/post_delay(-1=省略吃默认 200),
#  决策段耗时 ms, policy_loop 是否命中)
VARIANTS = [
    # —— 回弹闭环：兜底位每轮必命中 ——
    ("A 现状复刻（回弹闭环）", 600, 300, -1, DECIDE_MS, True),
    ("B 只降 policy_rate→50", 600, 50, 0, DECIDE_MS, True),
    ("C 只降 dwell→50", 50, 300, 0, DECIDE_MS, True),
    ("D 清掉 dwell 隐式 delay", 600, 300, 0, DECIDE_MS, True),
    # 对照：动作零耗时。若间隔塌到毫秒级 ⇒ 间隔=动作耗时，rate_limit/delay 全部旁路。
    ("E 决策段零耗时", 600, 300, 0, 0.0, True),
    ("F 零耗时 + 限速全部拉满 2000", 2000, 2000, 500, 0.0, True),
    # —— 驻留等待：dwell.next 全 miss（含兜底位也不命中），才是 rate_limit 的设计主场 ——
    ("G 全 miss 驻留 dwell=600", 600, 300, 0, DECIDE_MS, False),
    ("H 全 miss 驻留 dwell=50", 50, 300, 0, DECIDE_MS, False),
    ("I 全 miss 驻留 dwell=2000", 2000, 300, 0, DECIDE_MS, False),
]

KEEPALIVE: list = []  # 防 C 层句柄被 GC（binding __del__ 不等 worker 收摊 → 悬垂回调）


def _gap_stats(series_ns: list[int]) -> str:
    gaps = [(b - a) / 1e6 for a, b in zip(series_ns, series_ns[1:])]
    if not gaps:
        return "无样本"
    return (f"中位 {statistics.median(gaps):7.1f} 均值 {statistics.mean(gaps):7.1f} "
            f"最小 {min(gaps):7.1f} 最大 {max(gaps):7.1f}")


def run_variant(tag: str, dwell_rate: int, policy_rate: int, dwell_delays: int,
                budget_s: float, decide_ms: float, policy_hits: bool) -> None:
    capture = VirtualCapture()
    controller = ProbeController(capture)
    action = PolicyAction(capture, controller, decide_ms)
    resource = Resource()
    tasker = Tasker()

    resource.register_custom_recognition(RECO_NOHIT, NoHitRecognition())
    resource.register_custom_action(ACT_POLICY, action)

    tmp = Path(tempfile.mkdtemp(prefix="exp_v4_pace_"))
    graph = build_graph(dwell_rate, policy_rate, dwell_delays, policy_hits)
    (tmp / "exp.json").write_text(json.dumps(graph), encoding="utf-8")

    if not resource.post_pipeline(str(tmp)).wait().done:
        print(f"[{tag}] post_pipeline 失败，跳过")
        return
    tasker.bind(resource, controller)
    job = tasker.post_task("exp.boot")
    time.sleep(budget_s)
    tasker.post_stop()
    job.wait()

    path = "回弹闭环" if policy_hits else "全 miss 驻留"
    print(f"\n=== {tag} │ 路径={path} dwell.rate_limit={dwell_rate} "
          f"policy_loop.rate_limit={policy_rate} dwell.pre/post_delay="
          f"{'默认200' if dwell_delays < 0 else dwell_delays} 决策段耗时={decide_ms:.0f}ms ===")
    print(f"  框架截图次数 {len(controller.shot_ts)} → 相邻截图轮间隔 ms: {_gap_stats(controller.shot_ts)}")
    runs = action.runs
    if len(runs) < 2:
        print(f"  policy run {len(runs)} 次（该路径不触发兜底位，属预期）")
    else:
        gaps_ms = [(b["t_ns"] - a["t_ns"]) / 1e6 for a, b in zip(runs, runs[1:])]
        drifts = [r["fid_drift"] for r in runs]
        work_drifts = [r["work_fid_drift"] for r in runs]
        print(f"  policy run {len(runs)} 次 → 相邻决策帧间隔 ms: {_gap_stats([r['t_ns'] for r in runs])}")
        print(f"    1 秒内决策次数 ≈ {1000 / statistics.median(gaps_ms):.0f}")
        print(f"  Q2 决策段自取帧 vs 识别帧 漂移: 中位 {statistics.median(drifts):.1f} "
              f"最大 {max(drifts)} │ 分布 {sorted(set(drifts))}")
        print(f"     决策段工作期间漂移（{decide_ms:.0f}ms）: 中位 {statistics.median(work_drifts):.1f} "
              f"最大 {max(work_drifts)}")
        print(f"     被选中时的识别帧帧龄 ms: 中位 "
              f"{statistics.median([r['reco_age_ms'] for r in runs]):.1f}")
    KEEPALIVE.append((resource, tasker, controller, action, capture, tmp))


def main() -> None:
    budget_s = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    print(f"[probe] 虚拟帧率 {VIRTUAL_FPS:.0f}fps（周期 {1000 / VIRTUAL_FPS:.1f}ms）"
          f" 默认决策段耗时 {DECIDE_MS:.0f}ms 每组预算 {budget_s:.1f}s")
    for tag, dwell_rate, policy_rate, delays, decide_ms, policy_hits in VARIANTS:
        run_variant(tag, dwell_rate, policy_rate, delays, budget_s, decide_ms, policy_hits)
    print("\n[判据] Q1：对比 A–F（回弹闭环）与 G–I（全 miss 驻留）两组里"
          "「相邻间隔」随哪个参数变化 —— 分路径下结论，不得混说。"
          "\n       Q2：看 A–F 的「决策段自取帧 vs 识别帧 漂移」是否 >0"
          "（>0 即识别帧与决策帧不是同一帧）。")


if __name__ == "__main__":
    main()
