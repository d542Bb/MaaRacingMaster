# -*- coding: utf-8 -*-
"""treasure 统一接入（P2b 接入点）等价性单测：断点换算 → StageTracker。

treasure start() 的断点解析已收敛到统一底座 StageTracker，但保留「先 in STAGE_ORDER
判断、非法/None 回退 0」的旧观测语义（对照 racing P4 的做法）。本测试用 treasure 真实
STAGE_ORDER 做 before/after 比对，满足「同输入下观测一致」不变量。

treasure 的 STAGE_ORDER 从模块类读取（不 import module.py，避免拉入 cv2/maa 重依赖）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maaracing_master.core.stage_tracker import StageTracker

REPO = Path(__file__).resolve().parents[1]

# 阶段清单真源 = policy.json perception.stages.order（TreasureModule.STAGE_ORDER 由它派生）。
# 直读盘上真源而非镜像常量：手抄镜像正是 a1a6424 那类漂移的发生地；也不 import module.py，
# 避免拉入 cv2/maa 重依赖。
STAGE_ORDER = json.loads(
    (REPO / "maaracing_master" / "plugins" / "treasure" / "resources"
     / "policy" / "treasure.policy.json").read_text(encoding="utf-8")
)["perception"]["stages"]["order"]


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
        assert _legacy_skip("中标结算") == STAGE_ORDER.index("中标结算")
        # 流程首尾形态锁：开头按真实启动流程排（进游戏先待机 → 手柄指引弹窗 → 才到大厅），
        # 不锁具体索引数字——写死 10 就是上一次加页面时差点绊倒的那类断言。
        assert STAGE_ORDER[:3] == ["待机", "控制器指引弹窗", "游戏大厅"]
        assert STAGE_ORDER[-1] == "结算弹窗"