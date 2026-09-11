#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 决策策略层（P1，决策规则数据化；域中性——本模块零业务词汇）。

把「最终意图路由」（stage → action 查表 + 意图触达条件）从 Python 上纸到
policy.json：`policy` 段声明「当前事实满足条件 → 输出哪个意图」，
`engine_contract` 段供给本引擎消费的域白名单、推导与副作用形
（EngineContract——接入新模块 = 写新数据面，不改本文件）。
上游算事实的匹配/策略算法、下游状态副作用的更新逻辑一律留各 plugin。

本模块保持纯标准库，与 v4_source/trace 同级，职责边界：

- 契约（P0-6 / P0-7）：`EngineContract`（引擎的域合同）、`StateSnapshot`
  （白名单投影）、`DecisionFacts`（冻结快照，PolicyPlan.decide 全程只读）、
  `DecisionSnapshot`（trace 落盘的决策契约）。
- 数据与执行：`Policies`（schema 解析）、`PolicyPlan`（启动编译的不可变索引，
  运行时禁止 json lookup / 表达式解析，规则匹配复杂度 O(#rules)）。
- 校验：P01-P09（结构性错误硬阻断，语义告警可配置）；真源自洽互洽
  由 `tools/navkit/check_truth.py`（CI 闸门）承担。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
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

POLICIES_SCHEMA_VER = 1
ENGINE_CONTRACT_SCHEMA_VER = 1

# 条件算子白名单（P05）：只有六个原语，禁止表达式/函数/嵌套——引擎机制本体。
OP_WHITELIST: frozenset[str] = frozenset({"eq", "neq", "gt", "gte", "lt", "lte"})

_DERIVED_OPS: frozenset[str] = frozenset({"elapsed", "frame_mod"})


class PolicyError(ValueError):
    """policies 结构错误（编译期即抛）。携带 code（P01-P05）与 path。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"[{code}] {path}: {message}")
        self.code = code
        self.path = path
        self.message = message


# ------------------------------------------------------------------
# 引擎契约（engine_contract 段）：域的全部白名单/推导/副作用来自数据，
# core 不携带任何业务词汇——「机制归引擎，策略归数据」的机制侧闭环。
# ------------------------------------------------------------------


@dataclass(frozen=True)
class EngineContract:
    """policy.json `engine_contract` 段的内存形。

    - `facts/state_fields/algo_fields/decision_sources/effects/wait_keys`：
      规则条件、状态投影、算法产出、动态 key 源、effect、保留等待 key 的合法集；
    - `fallback_key/fallback_hint`：无任何规则命中时的终值决策；
    - `auto_effects`：决策 key → 自动附加的 effect（表驱动，无代码分支）；
    - `passthrough_effects`：规则显式声明 effect 即附加的合法集；
    - `effect_required_conditions`：effect → 声明该 effect 的规则必须覆盖的
      条件字段集（语义护栏，P05）；
    - `derived`：冻结期推导注入（op ∈ {elapsed, frame_mod}，算子白名单受控）；
    - `derived_sources`：推导前体键——允许经 state 流入、冻结后不出现在 facts；
    - `tuning_keys`：policy.tuning 的段 → 键白名单（未知键 P01 防拼写漂移）。
    """

    schema_ver: int
    facts: frozenset[str]
    state_fields: frozenset[str]
    algo_fields: frozenset[str]
    decision_sources: frozenset[str]
    effects: frozenset[str]
    wait_keys: frozenset[str]
    fallback_key: str
    fallback_hint: str
    auto_effects: Mapping[str, str]
    passthrough_effects: frozenset[str]
    effect_required_conditions: Mapping[str, frozenset[str]]
    derived: tuple[Mapping[str, Any], ...]
    derived_sources: frozenset[str]
    tuning_keys: Mapping[str, frozenset[str]]


def parse_engine_contract(raw: Any) -> EngineContract:
    """`engine_contract` 文档段 → EngineContract。结构性错误抛 PolicyError（fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise PolicyError("P01", "engine_contract",
                          f"engine_contract 须为 object，收到 {type(raw).__name__}")
    ver = raw.get("_schema_ver", ENGINE_CONTRACT_SCHEMA_VER)
    if ver != ENGINE_CONTRACT_SCHEMA_VER:
        raise PolicyError("P01", "engine_contract._schema_ver",
                          f"需为 {ENGINE_CONTRACT_SCHEMA_VER}，收到 {ver!r}")

    def _names(key: str, *, allow_empty: bool = False) -> frozenset[str]:
        v = raw.get(key)
        if not isinstance(v, Sequence) or isinstance(v, (str, bytes)):
            raise PolicyError("P01", f"engine_contract.{key}", f"{key} 须为字符串数组")
        if not v and not allow_empty:
            raise PolicyError("P01", f"engine_contract.{key}", f"{key} 须为非空数组")
        if any(not isinstance(x, str) or not x for x in v):
            raise PolicyError("P01", f"engine_contract.{key}", "元素须为非空字符串")
        return frozenset(v)

    facts = _names("facts")
    state_fields = _names("state_fields")
    algo_fields = _names("algo_fields")
    decision_sources = _names("decision_sources", allow_empty=True)
    effects = _names("effects")
    wait_keys = _names("wait_keys")

    fallback = raw.get("fallback")
    if not isinstance(fallback, Mapping) or not isinstance(fallback.get("key"), str) \
            or not isinstance(fallback.get("hint"), str):
        raise PolicyError("P01", "engine_contract.fallback", "fallback 须为 {key, hint} 字符串对象")

    auto_raw = raw.get("auto_effects", {})
    if not isinstance(auto_raw, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in auto_raw.items()):
        raise PolicyError("P01", "engine_contract.auto_effects", "须为字符串到字符串的 object")
    if sorted(set(auto_raw.values()) - effects):
        raise PolicyError("P01", "engine_contract.auto_effects",
                          f"目标 effect 不在 effects 白名单：{sorted(set(auto_raw.values()) - effects)}")

    passthrough = _names("passthrough_effects") if raw.get("passthrough_effects") else frozenset()
    if sorted(passthrough - effects):
        raise PolicyError("P01", "engine_contract.passthrough_effects",
                          f"effect 不在 effects 白名单：{sorted(passthrough - effects)}")

    erc_raw = raw.get("effect_required_conditions", {})
    if not isinstance(erc_raw, Mapping):
        raise PolicyError("P01", "engine_contract.effect_required_conditions", "须为 object")
    effect_required_conditions: dict[str, frozenset[str]] = {}
    for eff, req in erc_raw.items():
        if eff not in effects or not isinstance(req, Sequence) or isinstance(req, (str, bytes)) \
                or any(not isinstance(x, str) or not x for x in req):
            raise PolicyError("P01", f"engine_contract.effect_required_conditions.{eff}",
                              "键须为 effects 成员、值须为非空字符串数组")
        missing = sorted(set(req) - facts)
        if missing:
            raise PolicyError("P01", f"engine_contract.effect_required_conditions.{eff}",
                              f"条件字段不在 facts 白名单：{missing}")
        effect_required_conditions[eff] = frozenset(req)

    derived_raw = raw.get("derived", [])
    if not isinstance(derived_raw, Sequence) or isinstance(derived_raw, (str, bytes)):
        raise PolicyError("P01", "engine_contract.derived", "derived 须为数组")
    derived: list[Mapping[str, Any]] = []
    derived_sources: set[str] = set()
    for i, d in enumerate(derived_raw):
        path = f"engine_contract.derived[{i}]"
        if not isinstance(d, Mapping) or not isinstance(d.get("field"), str):
            raise PolicyError("P01", path, "须为含 field 的 object")
        if d.get("op") == "elapsed":
            if not isinstance(d.get("from"), str) or not d["from"]:
                raise PolicyError("P01", f"{path}.from", "elapsed 需要非空 from 前体键")
            derived_sources.add(d["from"])
        elif d.get("op") == "frame_mod":
            if isinstance(d.get("k"), bool) or not isinstance(d.get("k"), (int, float)):
                raise PolicyError("P01", f"{path}.k", "frame_mod 需要数值 k")
        else:
            raise PolicyError("P01", f"{path}.op",
                              f"推导算子须为 {sorted(_DERIVED_OPS)}，收到 {d.get('op')!r}")
        if d["field"] not in facts:
            raise PolicyError("P01", f"{path}.field",
                              f"派生目标 {d['field']!r} 不在 facts 白名单")
        derived.append(dict(d))

    tuning_raw = raw.get("tuning_keys")
    if not isinstance(tuning_raw, Mapping) or not tuning_raw:
        raise PolicyError("P01", "engine_contract.tuning_keys", "须为段到键数组的非空 object")
    tuning_keys: dict[str, frozenset[str]] = {}
    for sec, v in tuning_raw.items():
        if not isinstance(sec, str) or not isinstance(v, Sequence) or isinstance(v, (str, bytes)) \
                or any(not isinstance(x, str) or not x for x in v):
            raise PolicyError("P01", f"engine_contract.tuning_keys.{sec}", "段须为字符串数组")
        tuning_keys[sec] = frozenset(v)

    return EngineContract(
        schema_ver=int(ver),
        facts=facts,
        state_fields=state_fields,
        algo_fields=algo_fields,
        decision_sources=decision_sources,
        effects=effects,
        wait_keys=wait_keys,
        fallback_key=fallback["key"],
        fallback_hint=fallback["hint"],
        auto_effects=dict(auto_raw),
        passthrough_effects=passthrough,
        effect_required_conditions=effect_required_conditions,
        derived=tuple(derived),
        derived_sources=frozenset(derived_sources),
        tuning_keys=tuning_keys,
    )


# ------------------------------------------------------------------
# 契约：StateSnapshot / DecisionFacts / Decision / DecisionSnapshot
# ------------------------------------------------------------------


@dataclass(frozen=True)
class StateSnapshot:
    """运行时状态投射（P0-7）：封闭白名单，构造期校验字段 ⊆ 给定 `fields`。

    `fields` 来自 EngineContract（数据面供给）；本类只做投影不做算术，
    派生（elapsed / skip_cycle）由 `DecisionFacts.freeze` 在捕获期计算。
    """

    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def projection(cls, raw: Mapping[str, Any], *, fields: frozenset[str]) -> "StateSnapshot":
        """白名单投影：`raw` 必须 ⊆ `fields`，未知字段即抛（fail-closed）。"""
        unknown = set(raw) - set(fields)
        if unknown:
            raise PolicyError(
                "P04",
                "state",
                f"状态字段不在 StateSnapshot 白名单：{sorted(unknown)}",
            )
        return cls(values=dict(raw))


@dataclass(frozen=True)
class DecisionFacts:
    """决策输入事实（P0-6）：上游全部生产完后统一冻结，PolicyEngine 只读。

    `freeze` 输入：
    - `state_snapshot`：StateSnapshot 投影（直接从它读取运行时状态，禁止直读
      module / RuntimeState 可变对象）；
    - `outputs`：本帧上游算法产出（`algo_fields` 白名单，如 stage / 各 *_decision）；
    - `frame_counter`：帧号（契约 `derived` 推导 elapsed / mod 类事实的输入）。
    """

    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def freeze(
        cls,
        *,
        state_snapshot: StateSnapshot,
        outputs: Mapping[str, Any],
        frame_counter: int,
        contract: EngineContract,
    ) -> "DecisionFacts":
        unknown = set(outputs) - set(contract.algo_fields)
        if unknown:
            raise PolicyError(
                "P04",
                "outputs",
                f"算法产出字段不在 algo_fields 白名单：{sorted(unknown)}",
            )
        vals: dict[str, Any] = dict(outputs)
        allowed_state = contract.state_fields & (contract.facts | contract.derived_sources)
        for key, value in state_snapshot.values.items():
            if key not in allowed_state:
                raise PolicyError(
                    "P04",
                    f"state.{key}",
                    f"状态字段未声明可流入决策事实：{key!r}",
                )
            vals[key] = value
        for d in contract.derived:
            if d["op"] == "elapsed":
                since = vals.get(d["from"])
                vals[d["field"]] = None if since is None else frame_counter - since
            else:
                vals[d["field"]] = frame_counter % d["k"]
        vals["frame_counter"] = frame_counter
        for source_key in contract.derived_sources:
            vals.pop(source_key, None)
        leftover = set(vals) - set(contract.facts)
        if leftover:
            raise PolicyError(
                "P04",
                "facts",
                f"冻结后出现白名单外字段：{sorted(leftover)}",
            )
        return cls(values=dict(vals))

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def projection(self) -> Mapping[str, Any]:
        """json-safe 全量投影（trace 落盘与等价比较用）。"""
        return dict(self.values)


@dataclass(frozen=True)
class Decision:
    """策略输出的单帧决策。

    - `key`：意图 key；`hint`：展示文案
    - `source`：动态来源（bidding_decision 等）或 None（静态 key）
    - `payload`：附加载荷（如 {"center": (x, y)}），json-safe
    - `fatal`：终止指令（非 None 时 module 抛 ClickRetryExhaustedError 收尾）
    - `side_effects`：引擎副作用符号（决策后由调用方 applicator 应用）
    """

    key: str
    hint: str
    source: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    fatal: str | None = None
    side_effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionSnapshot:
    """trace 的决策契约（离线等价回放第一层）。

    由本帧 immutable `facts_projection` 与 policy 输出 `decision {key,source,payload}`
    组成；**不存在 `decision.state`**——全部输入事实都在 facts 里。
    """

    facts_projection: Mapping[str, Any]
    decision: Mapping[str, Any]
    fatal: str | None = None

    @classmethod
    def from_decision(cls, facts: DecisionFacts, decision: Decision) -> "DecisionSnapshot":
        return cls(
            facts_projection=facts.projection(),
            decision={
                "key": decision.key,
                "source": decision.source,
                "payload": dict(decision.payload),
                "side_effects": list(decision.side_effects),
            },
            fatal=decision.fatal,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts_projection": dict(self.facts_projection),
            "decision": dict(self.decision),
            "fatal": self.fatal,
        }


# ------------------------------------------------------------------
# 条件（受控原语，禁止 DSL 化）
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """单条件：`field` op `value`。省略等值的宽松写法（`"cooldown": 0`）
    视为 `eq`。值允许标量 / None / 数值比较对象 `{"gte": 0}`。"""

    field: str
    op: str
    value: Any

    def match(self, actual: Any) -> bool:
        if self.op == "eq":
            if self.value is None:
                return actual is None
            return actual is not None and actual == self.value
        if self.op == "neq":
            if self.value is None:
                return actual is not None
            return actual != self.value
        # 数值比较：两边都须为数字；None 或缺省一律不命中（fail-closed）。
        if actual is None or isinstance(actual, bool) or self.value is None:
            return False
        if isinstance(self.value, bool):
            return False
        try:
            if self.op == "gt":
                return actual > self.value
            if self.op == "gte":
                return actual >= self.value
            if self.op == "lt":
                return actual < self.value
            if self.op == "lte":
                return actual <= self.value
        except TypeError:
            return False
        return False


# ------------------------------------------------------------------
# schema 数据
# ------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """单条规则（schema 解析产物，编译期不可变）。"""

    id: str
    when: tuple[Condition, ...]
    key: str | None
    hint: str | None
    source: str | None
    fallback_key: str | None
    center: Any
    fatal: str | None
    effect: str | None
    order: int
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Policies:
    """policy.json 的 `policy` 段（一等公民，由 NavSource 装配持有）。

    `contract` 为同文件的 `engine_contract` 段——规则校验与运行时决策
    所需的域白名单/推导/副作用形，随 Policies 贯穿 compile/validate/decide。
    """

    schema_ver: int
    stage_map: Mapping[str, str]
    rules: tuple[PolicyRule, ...]
    tuning: Mapping[str, Any]
    contract: EngineContract


def parse_policies(raw: Any, contract: EngineContract) -> Policies:
    """policies 文档 → 内存对象。结构性错误抛 `PolicyError`（P01-P05）。"""
    if not isinstance(raw, Mapping):
        raise PolicyError("P01", "policies", f"policies 须为 object，收到 {type(raw).__name__}")
    ver = raw.get("_schema_ver")
    if ver != POLICIES_SCHEMA_VER:
        raise PolicyError(
            "P01",
            "policies._schema_ver",
            f"需为 {POLICIES_SCHEMA_VER}，收到 {ver!r}",
        )

    stage_map_raw = raw.get("stage_map")
    if not isinstance(stage_map_raw, Mapping) or not stage_map_raw:
        raise PolicyError("P01", "policies.stage_map", "stage_map 须为非空 object（稳定 ID → 运行时阶段名）")
    stage_map: dict[str, str] = {}
    for sid, sname in stage_map_raw.items():
        if not isinstance(sid, str) or not sid:
            raise PolicyError("P01", "policies.stage_map", f"稳定 ID 须为非空字符串，收到 {sid!r}")
        if not isinstance(sname, str) or not sname:
            raise PolicyError("P01", f"policies.stage_map.{sid}", f"运行时阶段名须为非空字符串，收到 {sname!r}")
        stage_map[sid] = sname

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, (str, bytes)):
        raise PolicyError("P01", "policies.rules", "rules 须为数组")

    rules: list[PolicyRule] = []
    for i, item in enumerate(rules_raw):
        if not isinstance(item, Mapping):
            raise PolicyError("P01", f"policies.rules[{i}]", "须为 object")
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise PolicyError("P01", f"policies.rules[{i}].id", "id 须为非空字符串")
        when_raw = item.get("when")
        if not isinstance(when_raw, Mapping):
            raise PolicyError("P01", f"policies.rules[{i}].when", "when 须为 object")
        conditions = _parse_when(when_raw, f"policies.rules[{i}].when", stage_map, contract)
        decision_raw = item.get("decision")
        if not isinstance(decision_raw, Mapping):
            raise PolicyError("P01", f"policies.rules[{i}].decision", "decision 须为 object")
        key, source, fallback_key, hint, center, fatal, effect = _parse_decision(
            decision_raw, f"policies.rules[{i}].decision", contract
        )
        rules.append(
            PolicyRule(
                id=rule_id,
                when=conditions,
                key=key,
                hint=hint,
                source=source,
                fallback_key=fallback_key,
                center=center,
                fatal=fatal,
                effect=effect,
                order=i,
                raw=dict(item),
            )
        )

    tuning_raw = raw.get("tuning")
    if not isinstance(tuning_raw, Mapping):
        raise PolicyError("P01", "policies.tuning", "tuning 须为 object")

    return Policies(
        schema_ver=int(ver),
        stage_map=stage_map,
        rules=tuple(rules),
        tuning=dict(tuning_raw),
        contract=contract,
    )


def _parse_when(
    when_raw: Mapping[str, Any], path: str, stage_map: Mapping[str, str],
    contract: EngineContract,
) -> tuple[Condition, ...]:
    out: list[Condition] = []
    for field_name, spec in when_raw.items():
        if not isinstance(field_name, str) or not field_name:
            raise PolicyError("P04", f"{path}.{field_name!r}", "条件字段名须为非空字符串")
        if field_name not in contract.facts:
            raise PolicyError(
                "P04",
                f"{path}.{field_name}",
                f"条件字段不在 DecisionFacts 白名单（已知：{sorted(contract.facts)}）",
            )
        if field_name == "stage":
            if isinstance(spec, str):
                if spec not in stage_map:
                    raise PolicyError(
                        "P04",
                        f"{path}.stage",
                        f"稳定 ID {spec!r} 未在 policies.stage_map 中声明",
                    )
                out.append(Condition("stage", "eq", spec))
                continue
            if isinstance(spec, Mapping):
                for op, val in spec.items():
                    if not isinstance(val, str):
                        raise PolicyError(
                            "P05", f"{path}.stage", "stage 前缀匹配值须为字符串"
                        )
                    if op == "prefix":
                        if not stage_map:
                            raise PolicyError(
                                "P04", f"{path}.stage", "stage 前缀匹配需要非空 stage_map"
                            )
                        out.append(Condition("stage", "prefix", val))
                    else:
                        raise PolicyError(
                            "P05", f"{path}.stage", f"stage 条件仅支持 prefix，收到 op={op!r}"
                        )
                continue
            raise PolicyError("P05", f"{path}.stage", "stage 条件须为字符串或 {prefix: ...}")
        out.append(_parse_condition(field_name, spec, f"{path}.{field_name}"))
    return tuple(out)


def _parse_condition(field_name: str, spec: Any, path: str) -> Condition:
    if spec is None or isinstance(spec, (str, int, float, bool)):
        return Condition(field_name, "eq", spec)
    if not isinstance(spec, Mapping):
        raise PolicyError("P05", path, f"条件须为标量或 {sorted(OP_WHITELIST)} 比较对象")
    seen_ops: list[str] = []
    for op, val in spec.items():
        if op not in OP_WHITELIST:
            raise PolicyError(
                "P05",
                path,
                f"非法算子 {op!r}（允许：{sorted(OP_WHITELIST)}）",
            )
        seen_ops.append(op)
        if op in ("eq", "neq"):
            continue
        if isinstance(val, str) and val.startswith("@"):
            continue
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
            raise PolicyError(
                "P05", f"{path}.{op}", f"数值比较 {op} 的取值须为数字或 @tuning 引用，收到 {val!r}"
            )
    if len(seen_ops) != 1:
        raise PolicyError("P05", path, "一个字段只允许一个比较算子（必要时拆成多条规则）")
    op = seen_ops[0]
    return Condition(field_name, op, spec[op])


def _parse_decision(
    decision_raw: Mapping[str, Any], path: str, contract: EngineContract,
) -> tuple[str | None, str | None, str | None, str | None, Any, str | None, str | None]:
    key = decision_raw.get("key")
    source = decision_raw.get("source")
    fallback_key = decision_raw.get("fallback_key")
    hint = decision_raw.get("hint")
    center = decision_raw.get("center")
    fatal = decision_raw.get("fatal")
    effect = decision_raw.get("effect")

    if key is not None and not isinstance(key, str):
        raise PolicyError("P05", f"{path}.key", "key 须为字符串")
    if hint is not None and not isinstance(hint, str):
        raise PolicyError("P05", f"{path}.hint", "hint 须为字符串")
    if source is not None:
        if not isinstance(source, str) or source not in contract.decision_sources:
            raise PolicyError(
                "P03",
                f"{path}.source",
                f"source 须为 {sorted(contract.decision_sources)} 之一，收到 {source!r}",
            )
    if fallback_key is not None and not isinstance(fallback_key, str):
        raise PolicyError("P05", f"{path}.fallback_key", "fallback_key 须为字符串")
    if center is not None:
        if isinstance(center, str):
            if not center:
                raise PolicyError("P05", f"{path}.center", "center 锚点 ID 不能为空字符串")
        elif isinstance(center, (list, tuple)):
            if len(center) != 2:
                raise PolicyError("P08", f"{path}.center", "center 数组须为 [x, y] 二元组")
            for axis, val in zip(("x", "y"), center):
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise PolicyError(
                        "P05", f"{path}.center.{axis}", f"坐标 {axis} 须为数字，收到 {val!r}"
                    )
        else:
            raise PolicyError("P05", f"{path}.center", "center 须为锚点 ID 或 [x, y] 坐标")
    if fatal is not None and not isinstance(fatal, str):
        raise PolicyError("P05", f"{path}.fatal", "fatal 须为字符串（终止原因文案）")
    if effect is not None:
        if not isinstance(effect, str) or effect not in contract.effects:
            raise PolicyError(
                "P05",
                f"{path}.effect",
                f"effect 须为 {sorted(contract.effects)} 之一，收到 {effect!r}",
            )
    if key is None and source is None and fatal is None:
        raise PolicyError(
            "P02", path, "decision 必须给出 key、source 或 fatal 之一（无产物=未定义行为）"
        )
    return key, source, fallback_key, hint, center, fatal, effect


# ------------------------------------------------------------------
# 编译：PolicyPlan（启动编译，运行时不可变）
# ------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledDecision:
    key: str
    hint: str
    source: str | None = None
    fallback_key: str | None = None
    center: tuple[float, float] | None = None
    fatal: str | None = None
    effect: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompiledRule:
    id: str
    conditions: tuple[Condition, ...]
    decision: CompiledDecision


def _resolve_center(center: Any, anchors: Mapping[str, Any]) -> tuple[float, float] | None:
    if center is None:
        return None
    if isinstance(center, (list, tuple)):
        x, y = float(center[0]), float(center[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise PolicyError("P08", "decision.center", "坐标须落在 [0, 1] 归一化范围内")
        return (x, y)
    anchor = anchors.get(center)
    if anchor is None:
        raise PolicyError("P08", f"decision.center", f"引用不存在的锚点 {center!r}")
    rect = getattr(anchor, "rect", None)
    as_list = getattr(rect, "as_list", None)
    if as_list is None:
        raise PolicyError("P08", f"decision.center", f"锚点 {center!r} 缺少 rect（无法解析中心）")
    x1, y1, x2, y2 = as_list()
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


class PolicyPlan:
    """policies → 不可变编译索引。运行时 `decide` 零 json lookup、零表达式解析。

    复杂度上界：O(#rules × #conditions)，N 很小（不承诺 O(1)）。
    """

    def __init__(
        self,
        *,
        rules: Sequence[CompiledRule],
        stage_map: Mapping[str, str],
        tuning: Mapping[str, Any],
        fallback: CompiledDecision,
        contract: EngineContract,
    ) -> None:
        self._rules = tuple(rules)
        self.stage_map = dict(stage_map)
        self.tuning = dict(tuning)
        self._fallback = fallback
        self.contract = contract

    @property
    def rules(self) -> tuple[CompiledRule, ...]:
        return self._rules

    def decide(self, facts: DecisionFacts) -> Decision:
        for rule in self._rules:
            if all(c.match(facts.get(c.field)) for c in rule.conditions):
                return self._build(rule.decision, facts)
        return self._build(self._fallback, facts)

    def _effects(self, key: str, compiled: CompiledDecision) -> tuple[str, ...]:
        """引擎侧副作用推断（更新逻辑留码；表驱动自契约，无业务分支）。"""
        out: list[str] = []
        auto = self.contract.auto_effects.get(key)
        if auto is not None:
            out.append(auto)
        if compiled.effect in self.contract.passthrough_effects:
            out.append(str(compiled.effect))
        return tuple(out)

    def _build(self, compiled: CompiledDecision, facts: DecisionFacts) -> Decision:
        payload = dict(compiled.payload)
        if compiled.center is not None:
            payload["center"] = compiled.center
        fatal = compiled.fatal
        if fatal is not None:
            fatal = fatal.format(frame=facts.get("frame_counter", 0))
        if compiled.source is not None:
            src = facts.get(compiled.source)
            if src is not None and isinstance(src, Mapping) and src.get("key"):
                key = str(src["key"])
                hint = compiled.hint or str(src.get("hint") or key)
                effects = self._effects(key, compiled)
                # 动态 center 由调用方（module._resolve_action_target）从上游 decision
                # 取值，不进决策 payload（与旧 _decide_action 返回结构一致）。
                return Decision(
                    key=key, hint=hint, source=compiled.source,
                    payload=payload, fatal=fatal, side_effects=effects,
                )
            key = compiled.fallback_key or self.contract.fallback_key
            hint = compiled.hint
            if hint is None and isinstance(src, Mapping) and src.get("hint"):
                hint = str(src["hint"])
            if hint is None:
                hint = self.contract.fallback_hint
            return Decision(key=key, hint=hint, source=compiled.source,
                            payload=payload, fatal=fatal)
        key = compiled.key or self.contract.fallback_key
        hint = compiled.hint or key
        effects = self._effects(key, compiled)
        return Decision(key=key, hint=hint, source=None,
                        payload=payload, fatal=fatal, side_effects=effects)


def _bake_conditions(
    conditions: Sequence[Condition],
    tuning: Mapping[str, Any],
    path: str,
) -> tuple[Condition, ...]:
    """把条件里的 `@tuning_key` 引用在编译期烘焙成数值（运行时零解析）。"""
    policy_tuning = tuning.get("policy") or {}
    out: list[Condition] = []
    for cond in conditions:
        value = cond.value
        if isinstance(value, str) and value.startswith("@"):
            name = value[1:]
            if name not in policy_tuning:
                raise PolicyError(
                    "P05", f"{path}.{cond.field}.{cond.op}",
                    f"@tuning 引用 {name!r} 未在 policies.tuning.policy 中定义",
                )
            value = policy_tuning[name]
        out.append(Condition(cond.field, cond.op, value))
    return tuple(out)


def compile_plan(policies: Policies, anchors: Mapping[str, Any]) -> PolicyPlan:
    """编译 Policies → PolicyPlan。`anchors` 来自数据面 spec（中心坐标编译期解析）。"""
    rules: list[CompiledRule] = []
    known_anchors = set(anchors)
    contract = policies.contract
    for rule in policies.rules:
        if rule.key is not None and rule.key not in known_anchors \
                and rule.source is None \
                and rule.key not in contract.wait_keys:
            raise PolicyError(
                "P02",
                f"policies.rules[{rule.order}].decision.key",
                f"decision.key {rule.key!r} 未命中任何锚点，且非保留等待 key",
            )
        center = _resolve_center(rule.center, anchors)
        compiled = CompiledDecision(
            key=rule.key or "",
            hint=rule.hint or "",
            source=rule.source,
            fallback_key=rule.fallback_key,
            center=center,
            fatal=rule.fatal,
            effect=rule.effect,
        )
        baked = _bake_conditions(
            rule.when, policies.tuning, f"policies.rules[{rule.order}]"
        )
        rules.append(CompiledRule(id=rule.id, conditions=baked, decision=compiled))

    fallback = CompiledDecision(key=contract.fallback_key, hint=contract.fallback_hint)
    return PolicyPlan(
        rules=rules,
        stage_map=policies.stage_map,
        tuning=policies.tuning,
        fallback=fallback,
        contract=contract,
    )


# ------------------------------------------------------------------
# 校验（P01-P09）：结构错误硬阻断；语义告警可配置
# ------------------------------------------------------------------


def validate_policy_document(
    policies: Policies,
    anchors: Mapping[str, Any],
    *,
    strict: bool = False,
) -> list[tuple[str, str, str, str]]:
    """P01-P09 校验，返回 [(code, level, path, message)]。

    `strict=False`（默认）：P06-P09 为告警；`strict=True`：升为 error。
    结构性错误（P01-P05）与引用错误（P02/P08 key/center 悬空）永远为 error。
    """
    issues: list[tuple[str, str, str, str]] = []
    warn_level = "error" if strict else "warning"

    known = set(anchors)
    rules = policies.rules
    contract = policies.contract

    for section, allowed in contract.tuning_keys.items():
        section_raw = policies.tuning.get(section)
        if section_raw is None:
            issues.append(("P01", "error", f"policies.tuning.{section}", f"tuning.{section} 缺失"))
            continue
        if not isinstance(section_raw, Mapping):
            issues.append(("P01", "error", f"policies.tuning.{section}", f"tuning.{section} 须为 object"))
            continue
        for key in section_raw:
            if key not in allowed:
                issues.append(
                    ("P01", "error", f"policies.tuning.{section}.{key}",
                     f"未知调参键（字段废弃/拼写错误会走 P 系列报错，不静默忽略）")
                )

    for rule in rules:
        base = f"policies.rules[{rule.order}]"
        policy_tuning = policies.tuning.get("policy") or {}
        for cond in rule.when:
            if isinstance(cond.value, str) and cond.value.startswith("@"):
                name = cond.value[1:]
                if name not in policy_tuning:
                    issues.append(
                        ("P05", "error", f"{base}.when.{cond.field}.{cond.op}",
                         f"@tuning 引用 {name!r} 未在 policies.tuning.policy 中定义")
                    )
        if rule.source is None and rule.key is not None and rule.key not in contract.wait_keys \
                and rule.key not in known:
            issues.append(
                ("P02", "error", f"{base}.decision.key",
                 f"decision.key {rule.key!r} 未命中任何锚点，且非保留等待 key")
            )
        if rule.center is not None and isinstance(rule.center, str) \
                and rule.center not in known:
            issues.append(
                ("P08", "error", f"{base}.decision.center",
                 f"引用不存在的锚点 {rule.center!r}")
            )
        if rule.effect is not None and rule.effect in contract.effect_required_conditions:
            fields = {c.field for c in rule.when}
            required = contract.effect_required_conditions[rule.effect]
            if not required <= fields:
                issues.append(
                    ("P05", "error", f"{base}.decision.effect",
                     f"effect={rule.effect} 的条件必须覆盖 {'/'.join(sorted(required))}")
                )

    # P07 duplicate / P06 unreachable：按规则顺序比较条件集合
    seen_conditions: list[frozenset[tuple[str, str, Any]]] = []
    for i, rule in enumerate(rules):
        base = f"policies.rules[{i}]"
        cur_conds = frozenset((c.field, c.op, _hashable(c.value)) for c in rule.when)
        if not cur_conds:
            continue
        for j, prev in enumerate(seen_conditions):
            if cur_conds == prev:
                issues.append(
                    ("P07", warn_level, base,
                     f"与 rules[{j}] 条件完全重复（后者永不命中，请删除或修正）")
                )
                break
            if prev <= cur_conds:
                issues.append(
                    ("P06", warn_level, base,
                     f"条件被 rules[{j}] 完全遮蔽（{j} 先匹配且条件更宽松，"
                     f"本规则不可达）")
                )
                break
        seen_conditions.append(cur_conds)

    # P09 terminal rule ambiguity：stage_map 中的每个稳定 ID 至少被一条规则覆盖
    covered: set[str] = set()
    for rule in rules:
        for cond in rule.when:
            if cond.field == "stage":
                if cond.op == "eq":
                    covered.add(cond.value)
                elif cond.op == "prefix":
                    covered.add(f"prefix:{cond.value}")
    for sid in policies.stage_map:
        if sid not in covered and f"prefix:{sid}" not in covered:
            issues.append(
                ("P09", warn_level, "policies.rules",
                 f"阶段 {sid!r} 无任何规则覆盖（将走默认兜底 {contract.fallback_key}），"
                 f"如属有意请忽略，否则请补充规则")
            )
    return issues


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, Mapping):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value