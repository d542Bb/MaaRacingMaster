# -*- coding: utf-8 -*-
"""「阶段 → 信号/锚点」归属真源统一的行为锁定（2026-09-15）。

看两件事：
1. 平行常量禁复活——`_STAGE_PERCEPTION` / `_STAGE_OCR_KEYS` / `_GLOBAL_ANCHORS`
   等 v3 遗留回退清单已从 module.py 退役（ADR-0002 真源单一；模块类定义期
   fail-closed，plan 缺失时插件根本不会加载，回退分支是结构性死路）。
2. 观察线程每帧扫描集 = policy `definitions[*].active` ∪ `global_anchors`——
   历史 bug 实证：`_judge_stage_into_slot` 曾并入代码常量 `_GLOBAL_ANCHORS`
   （缺 `hall_chat_left`）而非 plan 真源，policy 声明的「待机→global」旁路失效，
   「命中 hall_chat_left → 待机」除开机首帧全量扫描外从任何阶段都触发不了。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

# module 经 core.window_utils 顶层 import maa.toolkit，CI 轻依赖环境下收集期 ERROR
# 会中断整个 pytest 会话；按仓内既有口径整文件优雅跳过。
try:
    from maaracing_master.plugins.treasure import module as tm
    from maaracing_master.plugins.treasure.module import TreasureModule

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)
    tm = None
    TreasureModule = None

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)

_FRAME = np.zeros((720, 1280, 3), dtype=np.uint8)

_RETIRED_CONSTANTS = (
    "_STAGE_PERCEPTION", "_STAGE_OCR_KEYS", "_GLOBAL_ANCHORS",
    "_ROUND_PERCEPTION_ROIS", "_BID_OCR_KEYS", "_SETTLE_OCR_KEYS",
)


def _nav_plan():
    from maaracing_master.plugins.treasure import nav_source

    nav = nav_source()
    assert nav is not None, "policy.json 数据面必须可装配（类定义期同口径）"
    return nav.plan


class _DetectRaises:
    """桩 detector：记录 detect 收到的扫描集后抛异常——观察方法在 try/except 中
    捕获并直接 return，恰好锁定「进入检测前」的 active_rois 组装。"""

    def __init__(self, plan):
        self.plan = plan
        self.seen_active_rois = None

    def detect(self, frame_rgb, active_rois=None):
        self.seen_active_rois = active_rois
        raise RuntimeError("sentinel：只锁扫描集组装")


def test_parallel_constants_retired():
    """v3 平行回退清单不得复活——归属真源只有 policy definitions 一处。"""
    for name in _RETIRED_CONSTANTS:
        assert not hasattr(tm, name), f"module.py 出现平行真源 {name}（违反 ADR-0002）"


class _FakeSelf:
    """最小观察桩：只喂 _judge_stage_into_slot 进检测前读到的字段 + 方法绑定。"""

    def __init__(self, plan, stage="游戏大厅", bid_phase="wait_result"):
        self._detector = _DetectRaises(plan)
        self._obs_slot = None
        self._current_stage = stage
        self._bid_phase = bid_phase

    def _active_stage_rois(self, stage):
        return TreasureModule._active_stage_rois(self, stage)


def test_observe_scan_set_is_policy_union():
    """观察线程每帧扫描集 = 阶段 active ∪ plan.global_anchors（真源直读）。"""
    plan = _nav_plan()
    fake = _FakeSelf(plan)
    TreasureModule._judge_stage_into_slot(fake, _FRAME)
    seen = fake._detector.seen_active_rois
    assert seen is not None, "观察线程未把扫描集传给 detector"
    assert seen == set(plan.active_for("游戏大厅")) | set(plan.global_anchors)
    # 漂移回归的两侧钉死：hall_controller_popup 属「游戏大厅」active；
    # hall_chat_left 属 global_anchors（「→待机」判定信号，必须每帧并入）。
    assert "hall_controller_popup" in seen
    assert "hall_chat_left" in seen


def test_active_stage_rois_reads_plan_only():
    """_active_stage_rois 只经 plan：登记阶段返回 policy 集，未登记返回 None（全量兜底）。"""
    plan = _nav_plan()
    fake = SimpleNamespace(_detector=SimpleNamespace(plan=plan), _bid_phase="wait_result")
    got = TreasureModule._active_stage_rois(fake, "游戏大厅")
    assert got == plan.active_for("游戏大厅")
    assert TreasureModule._active_stage_rois(fake, "不存在的阶段") is None
    # 出价面板交互期动态收窄不回归（仅 smart_bid_btn）
    fake2 = SimpleNamespace(_detector=SimpleNamespace(plan=plan), _bid_phase="bidding")
    assert TreasureModule._active_stage_rois(fake2, "第3回合出价") == frozenset({"smart_bid_btn"})
