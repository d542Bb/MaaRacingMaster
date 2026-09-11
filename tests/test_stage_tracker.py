# -*- coding: utf-8 -*-
"""StageTracker（统一计划 P1b）单测。

覆盖：构造、current_stage 记录、set_stage 合法/非法校验、resolve_start_from 断点
解析（None / 合法 / 非法）、与 ROIConfig.stage_order 的衔接、next_stage 纯查询。
只依赖 core.stage_tracker + core.roi_config，不拉入重依赖。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.roi_config import ROIConfig
from maaracing_master.core.stage_tracker import InvalidStageError, StageTracker

_ORDER = ["大厅", "选择", "第1回合", "第2回合"]


# ---------------- 构造 ----------------

class TestConstruction:
    def test_of_sequence(self):
        t = StageTracker(_ORDER)
        assert t.order == ("大厅", "选择", "第1回合", "第2回合")
        assert t.current_stage is None

    def test_empty_order_rejected(self):
        with pytest.raises(ValueError):
            StageTracker([])

    def test_from_roi_config(self):
        schema = {
            "_schema_ver": 1,
            "reference_size": [1280, 720],
            "rois": {},
            "stages": {
                "order": ["大厅", "选择"],
                "global_anchors": [],
                "definitions": {"大厅": {"active_rois": []}, "选择": {"active_rois": []}},
            },
        }
        cfg = ROIConfig.from_dict(schema)
        t = StageTracker.from_roi_config(cfg)
        assert t.order == ("大厅", "选择")


# ---------------- current_stage 记录 ----------------

class TestCurrentStage:
    def test_lifecycle(self):
        t = StageTracker(_ORDER)
        assert t.current_stage is None
        t.set_stage("大厅")
        assert t.current_stage == "大厅"
        t.set_stage("选择")
        assert t.current_stage == "选择"

    def test_set_stage_null_resets(self):
        t = StageTracker(_ORDER)
        t.set_stage("大厅")
        t.set_stage(None)
        assert t.current_stage is None

    def test_set_stage_repeat_any_order_allowed(self):
        # 非线性的 set_stage 是允许的（只记录，不做转移合理性判断）
        t = StageTracker(_ORDER)
        t.set_stage("第2回合")
        assert t.current_stage == "第2回合"
        t.set_stage("大厅")
        assert t.current_stage == "大厅"

    def test_set_stage_invalid_rejected(self):
        t = StageTracker(_ORDER)
        with pytest.raises(InvalidStageError):
            t.set_stage("不存在的阶段")

    def test_current_stage_setter_alias(self):
        t = StageTracker(_ORDER)
        t.current_stage = "大厅"
        assert t.current_stage == "大厅"


# ---------------- resolve_start_from 断点解析 ----------------

class TestResolveStartFrom:
    def test_none_returns_zero(self):
        assert StageTracker(_ORDER).resolve_start_from(None) == 0
        assert StageTracker(_ORDER).resolve_start_from() == 0

    def test_from_start_of_order(self):
        # 断点=第一个阶段 → 从头开始
        assert StageTracker(_ORDER).resolve_start_from("大厅") == 0

    def test_from_middle(self):
        # 语义与 racing 的 STAGE_ORDER.index(start_from) 一致：返回 skip 索引
        assert StageTracker(_ORDER).resolve_start_from("选择") == 1
        assert StageTracker(_ORDER).resolve_start_from("第1回合") == 2

    def test_from_last(self):
        assert StageTracker(_ORDER).resolve_start_from("第2回合") == 3

    def test_invalid_rejected(self):
        with pytest.raises(InvalidStageError):
            StageTracker(_ORDER).resolve_start_from("不存在")

    def test_resolve_is_side_effect_free(self):
        # 解析不改变 current_stage
        t = StageTracker(_ORDER)
        t.resolve_start_from("第2回合")
        assert t.current_stage is None


# ---------------- next_stage 纯查询 ----------------

class TestNextStage:
    def test_returns_next(self):
        t = StageTracker(_ORDER)
        t.set_stage("选择")
        assert t.next_stage() == "第1回合"

    def test_last_stage_returns_none(self):
        t = StageTracker(_ORDER)
        t.set_stage("第2回合")
        assert t.next_stage() is None

    def test_no_current_returns_none(self):
        assert StageTracker(_ORDER).next_stage() is None

    def test_intermediate_index(self):
        assert StageTracker(_ORDER).stage_index("第1回合") == 2

    def test_stage_index_invalid(self):
        with pytest.raises(InvalidStageError):
            StageTracker(_ORDER).stage_index("nope")