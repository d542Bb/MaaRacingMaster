#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit —— 导航判断逻辑的运行时底座（v4 数据面）。

真源 = 插件 `resources/policy/*.policy.json`（感知规格 + 决策规则 + 执行资产）
与 `resources/pipeline/*.json`（图节点）；本包负责前者到内存对象的加载。

子模块（P4b 后现状；assets/compile_detect/validate/legacy/compile_route/signature
已随 v3 全家退役，退役对拍见 tools/experiments/v4-p4b-source/）
----------
- `v4_source` : policy.json → NavSource（DetectionPlan + spec + Policies 装配）
- `policy`    : P1 决策引擎（rules → PolicyPlan，输入由 v4_source 供）
- `trace`     : 决策落盘记录器（P4d 拍板去留）

约束（不可违反）
----------------
**纯标准库。** 本包及其测试不得 import cv2 / numpy / maa / vgamepad——
`tests/` 与 CI 只装 pytest，navkit 必须能在最干净的环境里跑通。
需要图像能力的功能一律留在包外（由调用方注入结果或目录）。
"""
from __future__ import annotations

from .v4_source import (
    ROUND_PHASE_STAGE,
    Anchor,
    AnchorSpec,
    DetectionPlan,
    NavSource,
    Rect,
    load_nav_source,
)
from .trace import FrameTrace, TraceWriter, json_safe
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
__all__ = [
    # v4_source（policy.json 数据面：感知规格 + DetectionPlan）
    "ROUND_PHASE_STAGE",
    "Anchor",
    "AnchorSpec",
    "DetectionPlan",
    "NavSource",
    "Rect",
    "load_nav_source",
    # trace
    "FrameTrace",
    "TraceWriter",
    "json_safe",
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
