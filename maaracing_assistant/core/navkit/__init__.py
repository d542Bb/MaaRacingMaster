#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit —— 导航与寻路判断逻辑的底座（schema v3）。

目标（docs/NAVKIT_PLAN.md §0.1）：把"程序在每一步认什么、认到之后做什么、做完去哪一步"
这套判断逻辑，从**一半在 JSON、一半在 Python 常量**的分裂状态，收敛成一份
**人写、工具可编辑、运行时可执行、事后可还原**的模型。

三个子模块
----------
- `assets`   : schema v3 文档 → 内存对象，以及 v3 → MAA 节点名的唯一映射权威
- `validate` : §3.3 规则表（E01-E20 / W01-W07）的可执行形式；纸码互查（D1）
- `legacy`   : v2 只读判定、v2 → v3 迁移草稿与缺口清单、逐字段等价比对
- `compile_detect` : assets → DetectionPlan（帧循环检测真源）

约束（不可违反）
----------------
**纯标准库。** 本包及其测试不得 import cv2 / numpy / maa / vgamepad——
`tests/` 与 CI 只装 pytest，navkit 必须能在最干净的环境里跑通。
需要图像能力的功能一律留在包外（由调用方注入结果或目录）。

**不碰运行时。** S0 阶段本包只被 `scripts/` 与 `tests/` 消费；
`detector.py` / `module.py` / 控制台的接线属 S1、S4，未授权不动。

阶段：S0（本包 + 迁移草稿 + 缺口报告）已完成，S1（鉴宝接入 + 逐帧回归）待批准。
"""
from __future__ import annotations

from .assets import (
    ANCHOR_KINDS,
    ANY_STAGE,
    CORE_IMAGE_DIR,
    OWNER_GLOBAL,
    ROUTE_ACTIONS,
    SCHEMA_V3,
    SPECIAL_TRANSITION_TARGETS,
    TEMPLATE_SUFFIXES,
    Anchor,
    Arbitration,
    Assets,
    MatchPolicy,
    NavKitError,
    Route,
    RouteStep,
    StageDef,
    Transition,
    route_node_name,
)
from .legacy import (
    V2_SEGMENTS,
    Gap,
    V2Item,
    V2Report,
    diff_v2_v3,
    inspect_v2,
    migrate_v2_to_v3,
    schema_of,
)
from .compile_detect import (
    ROUND_PHASE_STAGE,
    AnchorSpec,
    DetectionPlan,
    compile_detection,
)
from .trace import FrameTrace, TraceWriter, json_safe
from .signature import anchor_signature
from .compile_route import compile_routes, compile_routes_json, generated_header
from .policy import (
    ALGO_FIELDS,
    DECISION_SOURCES,
    DEFAULT_FALLBACK_HINT,
    DEFAULT_FALLBACK_KEY,
    EFFECT_WHITELIST,
    FACT_FIELDS,
    OP_WHITELIST,
    POLICIES_SCHEMA_VER,
    STATE_FIELDS,
    CompiledDecision,
    CompiledRule,
    Condition,
    Decision,
    DecisionFacts,
    DecisionSnapshot,
    PolicyError,
    PolicyPlan,
    PolicyRule,
    Policies,
    StateSnapshot,
    compile_plan,
    parse_policies,
    validate_policy_document,
)
from .validate import (
    Issue,
    NavKitValidationError,
    Report,
    assert_valid,
    safe_load,
    validate_assets,
    validate_compiled,
    validate_merged,
)

__all__ = [
    # assets
    "SCHEMA_V3",
    "ANY_STAGE",
    "CORE_IMAGE_DIR",
    "OWNER_GLOBAL",
    "ANCHOR_KINDS",
    "ROUTE_ACTIONS",
    "TEMPLATE_SUFFIXES",
    "SPECIAL_TRANSITION_TARGETS",
    "NavKitError",
    "MatchPolicy",
    "Arbitration",
    "Anchor",
    "StageDef",
    "Transition",
    "RouteStep",
    "Route",
    "Assets",
    "route_node_name",
    # validate
    "Issue",
    "Report",
    "NavKitValidationError",
    "validate_assets",
    "validate_compiled",
    "validate_merged",
    "safe_load",
    "assert_valid",
    # legacy
    "V2_SEGMENTS",
    "V2Item",
    "V2Report",
    "Gap",
    "schema_of",
    "inspect_v2",
    "migrate_v2_to_v3",
    "diff_v2_v3",
    # compile_detect
    "ROUND_PHASE_STAGE",
    "AnchorSpec",
    "DetectionPlan",
    "compile_detection",
    # trace
    "FrameTrace",
    "TraceWriter",
    "json_safe",
    # compile_route
    "compile_routes",
    "compile_routes_json",
    "anchor_signature",
    "generated_header",
    # policy（P1）
    "POLICIES_SCHEMA_VER",
    "DEFAULT_FALLBACK_KEY",
    "DEFAULT_FALLBACK_HINT",
    "FACT_FIELDS",
    "STATE_FIELDS",
    "ALGO_FIELDS",
    "OP_WHITELIST",
    "DECISION_SOURCES",
    "EFFECT_WHITELIST",
    "PolicyError",
    "StateSnapshot",
    "DecisionFacts",
    "Decision",
    "DecisionSnapshot",
    "Condition",
    "PolicyRule",
    "Policies",
    "CompiledDecision",
    "CompiledRule",
    "PolicyPlan",
    "parse_policies",
    "compile_plan",
    "validate_policy_document",
]
