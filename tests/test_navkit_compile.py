#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 检测计划编译器单测 + 鉴宝真实资产的契约测试（纯标准库）。

两件事：

1. **编译器行为**（§9.2 `test_navkit_compile.py` 的规格）：
   阶段顺序/全局锚点/感知清单/OCR 清单/锚点规格如何从 v3 文档落到 DetectionPlan，
   以及唯一匹配口径（G3）如何被派生。

2. **真实资产契约**（`treasure_assets.json`）：
   这份文件是 S1 的检测真源，它的形状一旦漂移（阶段名、锚点归属、检测优先级）
   就会直接改变运行时判定。这里把"它必须长成什么样"钉死：
     - 阶段顺序 == `STAGE_ORDER`（GUI 断点契约，§0.5 不可改名重排）
     - 检测优先级顺序 == v2 `_ROI_STAGE` 的 priority 降序（逐帧等价的前提）
     - 无担保的 point 目标数 == 0（D2 担保制）

   注意：**不 import module.py**（会拉入 maa/cv2/opencv 重依赖），阶段顺序以
   字面量形式在此复述——若 module 侧改名，这里会红，正是我们想要的效果。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from maaracing_assistant.core.navkit import (
    ROUND_PHASE_STAGE,
    AnchorSpec,
    Assets,
    DetectionPlan,
    compile_detection,
    validate_assets,
)

MODULE = "treasure"

ASSETS_PATH = (
    Path(__file__).resolve().parents[1]
    / "maaracing_assistant" / "plugins" / "treasure"
    / "resources" / "config" / "treasure_assets.json"
)

# §0.5 GUI 断点契约：阶段名与顺序不得改变（与 module.STAGE_ORDER 逐项一致）
EXPECTED_STAGE_ORDER = [
    "游戏大厅", "活动页面", "鉴宝大厅(选择场次)", "匹配中", "选择鉴宝师",
    "第1回合出价", "第2回合出价", "第3回合出价", "第4回合出价", "第5回合出价",
    "中标结算", "领取分红", "结算弹窗",
]

# v2 `detector._ROI_STAGE` 的 (锚点, priority)，按扫描顺序（priority 降序、同级保持字典序）。
# v3 的 order = 1000 - priority，检测优先级 = 1000 - order，故顺序必须逐项一致。
EXPECTED_SCAN_ORDER = [
    "daily_high_banner",        # 110
    "egg_reward_title",         # 105
    "settle_title",             # 100
    "result_banner",            # 90
    "smart_bid_btn",            # 80
    "round_big_banner",         # 70
    "appraiser_title",          # 60
    "is_matching_btn",          # 60
    "hall_peak_appraise_card",  # 50
    "goto_appraise_btn",        # 50
    "hall_session_cards",       # 50
]


# ------------------------------------------------------------------
# 构造辅助
# ------------------------------------------------------------------


def minimal_doc() -> dict[str, Any]:
    """能编译出 DetectionPlan 的最小合法 v3 文档。"""
    return {
        "_schema_ver": 3,
        "_module": MODULE,
        "reference_size": [1280, 720],
        "match": {
            "scales": [0.7, 1.0, 1.3],
            "threshold": 0.75,
            "margin_default": 0.01,
        },
        "pages": {"hall": {"label": "大厅"}, "bidding": {"label": "出价"}},
        "anchors": {
            "hall_card": {
                "kind": "template", "owner": MODULE, "page": "hall", "label": "大厅卡片",
                "rect": [0.1, 0.1, 0.3, 0.3], "templates": ["hall_card.png"], "order": 10,
            },
            "panel_title": {
                "kind": "template", "owner": MODULE, "page": "bidding", "label": "面板标题",
                "rect": [0.0, 0.0, 0.5, 0.2], "templates": ["panel.png"], "order": 20,
                "arbitration": {"margin": 0.03, "round_from_template": True},
            },
            "bid_btn": {
                "kind": "point", "owner": MODULE, "page": "bidding", "label": "出价按钮",
                "rect": [0.4, 0.4, 0.6, 0.6], "guarded_by": "panel_title", "order": 30,
            },
            "amount": {
                "kind": "ocr", "owner": MODULE, "page": "bidding", "label": "读数",
                "rect": [0.1, 0.1, 0.2, 0.2], "order": 40,
            },
        },
        "stages": {
            "order": ["大厅", "出价"],
            "global_anchors": ["hall_card"],
            "definitions": {
                "大厅": {"page": "hall", "anchors": ["hall_card"], "ocr": []},
                "出价": {
                    "page": "bidding",
                    "anchors": ["panel_title", "bid_btn"],
                    "ocr": ["amount"],
                    "dynamic_narrow": {"by": "code:_active_stage_rois"},
                },
            },
        },
        "transitions": [
            {"stage": "*", "on": "hall_card", "to": "大厅"},
            {"stage": "*", "on": "panel_title", "to": "$round"},
            {"stage": "大厅", "on": "hall_card", "to": "大厅"},
        ],
        "routes": {},
    }


def plan_of(doc: dict[str, Any] | None = None) -> DetectionPlan:
    return compile_detection(Assets.from_document(doc or minimal_doc(), module=MODULE))


# ------------------------------------------------------------------
# 编译器
# ------------------------------------------------------------------


def test_plan_basics():
    plan = plan_of()
    assert plan.stage_order == ("大厅", "出价")
    assert plan.global_anchors == ("hall_card",)
    assert plan.active["出价"] == frozenset({"panel_title", "bid_btn"})
    assert plan.ocr_keys["出价"] == frozenset({"amount"})
    assert plan.scales == (0.7, 1.0, 1.3)
    assert plan.default_threshold == 0.75
    assert plan.margin_default == 0.01
    assert plan.detect_anchors == ("hall_card", "panel_title")


    def test_spec_carries_everything_detector_needs():
        spec = plan_of().spec["panel_title"]
        assert spec.templates == ("panel.png",)
        assert spec.rect == [0.0, 0.0, 0.5, 0.2]
        assert spec.arbitration["margin"] == 0.03
        assert spec.arbitration["round_from_template"] is True
        assert spec.kind == "template"
        assert spec.threshold is None, "未覆盖时应为 None，由 detector 回落到 plan.default_threshold"


def test_arbitration_defaults_to_empty():
    assert plan_of().spec["hall_card"].arbitration == {}


def test_ocr_anchor_has_no_templates_but_has_rect():
    spec = plan_of().spec["amount"]
    assert spec.kind == "ocr"
    assert spec.templates == ()


def test_active_for_unknown_stage_is_none():
    """未登记阶段 → None（运行时回退全量检测，既有安全兜底，W05 允许）。"""
    assert plan_of().active_for("幽灵阶段") is None
    assert plan_of().active_for(None) is None
    assert plan_of().ocr_for("幽灵阶段") is None


def test_stage_of_derived_from_transitions():
    """锚点 → 命中后归属阶段，来自 transitions（v2 `_ROI_STAGE.stage` 的替代）。"""
    plan = plan_of()
    assert plan.stage_stage["hall_card"] == "大厅"
    # $round / same 归一为回合阶段哨兵，由 detector 实例化具体「第N回合出价」
    assert plan.stage_stage["panel_title"] == ROUND_PHASE_STAGE


def test_dynamic_narrow_is_exposed_as_code_pointer():
    """上不了纸的动态裁剪只留指针（E16），plan 负责透传、不解释。"""
    assert plan_of().dynamic_narrow["出价"] == "code:_active_stage_rois"


def test_priority_inverse_of_order():
    """检测优先级 = 1000 - order（v2 `priority` 的等义换算）。"""
    plan = plan_of()
    assert plan.spec["hall_card"].stage_priority > plan.spec["panel_title"].stage_priority
    assert plan.spec["panel_title"].stage_priority > plan.spec["bid_btn"].stage_priority


def test_compile_is_deterministic():
    """同输入两次编译产出等值对象（§6 确定性）。"""
    a, b = plan_of(), plan_of()
    assert a == b


# ------------------------------------------------------------------
# 真实资产契约
# ------------------------------------------------------------------


@pytest.mark.skipif(not ASSETS_PATH.exists(), reason=f"缺少资产文件：{ASSETS_PATH}")
class TestTreasureAssets:
    """`treasure_assets.json` 的形状契约——漂移即失败。"""

    @staticmethod
    @pytest.fixture()
    def doc() -> dict[str, Any]:
        return json.loads(ASSETS_PATH.read_text(encoding="utf-8"))

    @staticmethod
    @pytest.fixture()
    def assets(doc) -> Assets:
        return Assets.from_document(doc, module=MODULE)

    @staticmethod
    @pytest.fixture()
    def plan(assets) -> DetectionPlan:
        return compile_detection(assets)

    def test_validates_clean(self, assets):
        report = validate_assets(assets)
        assert report.ok, report.text()

    def test_stage_order_matches_gui_contract(self, plan):
        assert list(plan.stage_order) == EXPECTED_STAGE_ORDER

    def test_scan_priority_preserves_v2_order(self, plan):
        """逐帧等价的前提：扫描顺序必须与 v2 `_ROI_STAGE` 的 priority 降序一致。"""
        scannable = {
            name: s for name, s in plan.spec.items()
            if s.kind == "template" and s.templates and name in EXPECTED_SCAN_ORDER
        }
        by_priority = sorted(scannable, key=lambda n: -scannable[n].stage_priority)
        assert by_priority == EXPECTED_SCAN_ORDER

    def test_global_anchors_are_the_two_hall_anchors(self, plan):
        """不变量 I-1：大厅锚点恒全量，漏并入会导致阶段永久冻结。"""
        assert set(plan.global_anchors) == {"hall_peak_appraise_card", "hall_session_cards"}

    def test_every_point_target_is_guarded(self, doc):
        """D2 担保制：没有模板图的点击目标必须登记担保人，一个都不能漏。"""
        unguarded = [
            name for name, a in doc["anchors"].items()
            if a["kind"] == "point" and not a.get("guarded_by")
        ]
        assert unguarded == []

    def test_guardians_are_template_anchors(self, doc):
        """E13：面板内件不能由另一个面板内件担保，担保链必须以模板锚点收口。"""
        for name, a in doc["anchors"].items():
            guard = a.get("guarded_by")
            if guard:
                assert doc["anchors"][guard]["kind"] == "template", f"{name} 的担保人不合法"

    def test_match_policy_is_the_single_source(self, doc):
        """G3：尺度表 13 档 / 默认阈值 0.75，与历史五处定义同值。"""
        assert doc["match"]["scales"] == [0.70, 0.75, 0.80, 0.85, 0.90, 0.95,
                                          1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30]
        assert doc["match"]["threshold"] == 0.75

    def test_round_banner_arbitration_preserved(self, doc):
        """回合横幅的 margin 0.03 + 从模板解析回合号是判定正确性的关键参数。"""
        arb = doc["anchors"]["round_big_banner"]["arbitration"]
        assert arb["margin"] == 0.03
        assert arb["round_from_template"] is True

    def test_win_banner_threshold_override_preserved(self, doc):
        """中标横幅带彩条特效，0.60 的放宽阈值是实测结论，不得丢。"""
        arb = doc["anchors"]["result_banner"]["arbitration"]
        assert arb["template_thresholds"]["result_auction_win_banner"] == 0.60

    def test_renamed_anchor_exists(self, doc):
        """跨段同名的处置：stage 版判定、actions 版改名后专供点击。"""
        assert "session_start_match_btn" in doc["anchors"]        # 判定（有模板）
        assert "session_start_match_click" in doc["anchors"]      # 点击（纯坐标）
        assert doc["anchors"]["session_start_match_btn"]["templates"] == [
            "session_start_match_btn.png"
        ]
        assert doc["anchors"]["session_start_match_click"]["kind"] == "point"
