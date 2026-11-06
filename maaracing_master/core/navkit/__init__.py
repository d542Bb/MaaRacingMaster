#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit —— 导航判断逻辑的运行时底座（v4 数据面）。

真源 = 插件 `resources/policy/*.policy.json`（感知规格 + 决策规则 + 执行资产）
与 `resources/pipeline/*.json`（图节点）；本包负责前者到内存对象的加载。

子模块
------
- `v4_source` : policy.json → NavSource（DetectionPlan + spec + Policies 装配）——**已过时**
- `policy`    : P1 决策引擎（rules → PolicyPlan，输入由 v4_source 供）——**已过时**
- `step`      : 统一步循环的重试状态机（计数 / 时间窗 / 耗尽 / 停止，域无关）

`v4_source` / `policy` 随策略表于 2026-11-06 标过时：无现役消费者，仅供归档鉴宝
与开发校验链参考。新模块接入导航只使用节点 JSON + `step`。

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
from .step import Command, StepExhaustedError, StepMachine, Verdict
from .policy import (
    ENGINE_CONTRACT_SCHEMA_VER,
    OP_WHITELIST,
    POLICIES_SCHEMA_VER,
    CompiledDecision,
    CompiledRule,
    Condition,
    Decision,
    DecisionFacts,
    DecisionSnapshot,
    EngineContract,
    PolicyError,
    PolicyPlan,
    PolicyRule,
    Policies,
    StateSnapshot,
    compile_plan,
    parse_engine_contract,
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
    # step（统一步循环重试状态机；域无关，插件喂 verdict / 收 command）
    "StepExhaustedError",
    "Verdict",
    "Command",
    "StepMachine",
    # policy（P1；EngineContract = 引擎的域合同，白名单/推导/副作用全来自数据面）
    "POLICIES_SCHEMA_VER",
    "ENGINE_CONTRACT_SCHEMA_VER",
    "OP_WHITELIST",
    "EngineContract",
    "parse_engine_contract",
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
