# -*- coding: utf-8 -*-
"""treasure 统一接入（P2b 接入点）等价性单测：断点换算 → StageTracker。

treasure start() 的断点解析已收敛到统一底座 StageTracker，但保留「先 in STAGE_ORDER
判断、非法/None 回退 0」的旧观测语义（对照 racing P4 的做法）。本测试用 treasure 真实
STAGE_ORDER 做 before/after 比对，满足「同输入下观测一致」不变量。

treasure 的 STAGE_ORDER 从模块类读取（不 import module.py，避免拉入 cv2/maa 重依赖）。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.stage_tracker import StageTracker

# 与 TreasureModule.STAGE_ORDER 保持一致（模块类中定义，此处镜像用于纯逻辑对照）
STAGE_ORDER = [
    "游戏大厅", "活动页面", "鉴宝大厅(选择场次)", "匹配中", "选择鉴宝师",
    "第1回合出价", "第2回合出价", "第3回合出价", "第4回合出价", "第5回合出价",
    "中标结算", "领取分红", "结算弹窗",
]


def _legacy_skip(start_from: str | None):
    if start_from and start_from in STAGE_ORDER:
        return STAGE_ORDER.index(start_from)
    return 0


def _migrated_skip(start_from: str | None):
    tracker = StageTracker(STAGE_ORDER)
    if start_from and start_from in STAGE_ORDER:
        return tracker.resolve_start_from(start_from)
    return 0


class TestTreasureBreakpointEquivalence:
    @pytest.mark.parametrize("stage", STAGE_ORDER)
    def test_each_valid_stage_same(self, stage):
        assert _migrated_skip(stage) == _legacy_skip(stage)

    def test_none_and_empty_same_fallback_zero(self):
        assert _migrated_skip(None) == _legacy_skip(None) == 0
        assert _migrated_skip("") == _legacy_skip("") == 0

    def test_invalid_stage_falls_back_zero(self):
        # 迁移前非法 start_from 走 else 回退 0，不抛错（★红线，迁移版靠先 in 判断保留）
        assert _migrated_skip("不存在的阶段") == 0

    def test_resolve_from_alone_raises_on_invalid(self):
        tracker = StageTracker(STAGE_ORDER)
        with pytest.raises(Exception):
            tracker.resolve_start_from("不存在的阶段")

    def test_mid_stage_index(self):
        assert _legacy_skip("中标结算") == STAGE_ORDER.index("中标结算") == 10