# -*- coding: utf-8 -*-
"""断点换算 → StageTracker 的等价性单测（core/stage_tracker 契约）。

统一底座 StageTracker 必须保留「先 in STAGE_ORDER 判断、非法/None 回退 0」的旧观测语义。
本测试用中性阶段清单做 before/after 比对，满足「同输入下观测一致」不变量——样本与任何
具体活动无关，故不随活动退役而失效（原样本取自 treasure，见 plugins/treasure/RETIRED.md）。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.stage_tracker import StageTracker

# 中性阶段清单：本文件锁的是 StageTracker 的断点换算语义（core 契约），与任何具体活动的
# 阶段名无关。原先直读 treasure 的 policy.json 真源做样本，随其退役（见
# plugins/treasure/RETIRED.md）改为中性样本——core 的契约锁不应依赖退役模块的资源。
STAGE_ORDER = ["s1", "s2", "s3", "s4", "s5"]


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
        # 取中间样本，覆盖非首非尾的真实中间态
        mid = STAGE_ORDER[len(STAGE_ORDER) // 2]
        assert _legacy_skip(mid) == STAGE_ORDER.index(mid)