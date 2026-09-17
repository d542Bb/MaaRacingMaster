#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NavKit P1 决策策略层单测（纯标准库，CI 只装 pytest 即可运行）。

覆盖 docs/plan/NAVKIT_P1_PLAN.md 的契约：
- P0-6 DecisionFacts 冻结快照 / 派生事实（retry_elapsed_ms / reward_elapsed / skip_cycle）
- P0-7 StateSnapshot 封闭白名单投影（未知字段 fail-closed）
- §4 schema：parse_policies 结构错误（P01-P05）
- §4.3 PolicyPlan 行为语义：各阶段决策/兜底/透传/冷却边界（单轨绝对断言）
- §4.2 validate_policy_document：P06/P07/P09 告警与 strict 升级
- §5.1 P1e fail-closed：policies 缺失/非法 = 启动失败
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maaracing_master.core.navkit import (
    DecisionFacts,
    DecisionSnapshot,
    EngineContract,
    PolicyError,
    PolicyPlan,
    StateSnapshot,
    compile_plan,
    parse_engine_contract,
    parse_policies,
    validate_policy_document,
)

_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "maaracing_master/plugins/treasure/resources/policy/treasure.policy.json"
)


def _load_nav():
    """policy.json 数据面（P4b 起本套件的唯一输入源）。"""
    from maaracing_master.core.navkit.v4_source import load_nav_source

    return load_nav_source(_POLICY_PATH)


def _contract() -> EngineContract:
    return _load_nav().policies.contract


def _make_facts(
    *,
    stage: str | None,
    frame: int,
    popup_kind: str | None = None,
    session_decision: dict | None = None,
    appraiser_decision: dict | None = None,
    bidding_decision: dict | None = None,
    settle_income: int | None = None,
    clicked_once: bool = False,
    retry_count: int = 0,
    now_ms: int = 0,
    settle_skip_since_ms: int = 0,
    cooldown: int = 0,
    daily_high_score: int | None = None,
    egg_reading: bool = False,
    egg_read_done: bool = False,
    reward_enter_frame: int = 0,
) -> DecisionFacts:
    contract = _contract()
    state = StateSnapshot.projection({
        "frame_counter": frame,
        "settle_income": settle_income,
        "clicked_once": clicked_once,
        "retry_count": retry_count,
        "now_ms": now_ms,
        "settle_skip_since_ms": settle_skip_since_ms,
        "cooldown": cooldown,
        "daily_high_score": daily_high_score,
        "egg_reading": egg_reading,
        "egg_read_done": egg_read_done,
        "reward_enter_frame": reward_enter_frame,
    }, fields=contract.state_fields)
    outputs = {
        "stage": stage,
        "popup_kind": popup_kind,
        "session_decision": session_decision,
        "appraiser_decision": appraiser_decision,
        "bidding_decision": bidding_decision,
    }
    return DecisionFacts.freeze(
        state_snapshot=state, outputs=outputs, frame_counter=frame, contract=contract)


# ------------------------------------------------------------------
# P0-7：StateSnapshot 白名单投影
# ------------------------------------------------------------------


def test_state_snapshot_projection_rejects_unknown_fields():
    with pytest.raises(PolicyError) as exc:
        StateSnapshot.projection({"frame_counter": 1, "unknown_field": 3},
                                 fields=_contract().state_fields)
    assert exc.value.code == "P04"


def test_state_snapshot_projection_accepts_all_fields():
    snap = StateSnapshot.projection({
        "frame_counter": 1, "settle_income": None, "clicked_once": False,
        "retry_count": 0, "now_ms": 0, "settle_skip_since_ms": 0, "cooldown": 0,
        "daily_high_score": None, "egg_reading": False, "egg_read_done": False,
        "reward_enter_frame": 0,
    }, fields=_contract().state_fields)
    assert snap.values["frame_counter"] == 1


# ------------------------------------------------------------------
# P0-6：DecisionFacts 冻结 + 派生事实
# ------------------------------------------------------------------


def test_decision_facts_derived_fields():
    facts = _make_facts(stage="settle", frame=20, clicked_once=True,
                        now_ms=20_000, settle_skip_since_ms=5_000)
    # 重试已过多久按**墙钟毫秒**差（不是帧号差）：v4 帧率由框架驱动，帧数口径会被稀释
    assert facts.get("retry_elapsed_ms") == 15_000
    assert facts.get("skip_cycle") == 20 % 3
    assert facts.get("frame_counter") == 20
    # 推导源（now_ms / settle_skip_since_ms）冻结后不出现在 facts 里
    assert facts.get("now_ms") is None
    assert facts.get("settle_skip_since_ms") is None


def test_decision_facts_rejects_unknown_outputs():
    with pytest.raises(PolicyError) as exc:
        DecisionFacts.freeze(
            state_snapshot=StateSnapshot.projection({}, fields=_contract().state_fields),
            outputs={"stage": "hall", "not_a_fact": 1},
            frame_counter=1,
            contract=_contract(),
        )
    assert exc.value.code == "P04"


def test_decision_snapshot_structure():
    facts = _make_facts(stage="hall", frame=1)
    plan = _compile()
    snap = DecisionSnapshot.from_decision(facts, plan.decide(facts))
    d = snap.as_dict()
    assert d["facts_projection"]["stage"] == "hall"
    assert d["decision"]["key"] == "hall_peak_appraise_card"
    assert "state" not in d["decision"]


# ------------------------------------------------------------------
# §4 schema：parse_policies 结构错误
# ------------------------------------------------------------------


def test_parse_policies_bad_schema_ver():
    with pytest.raises(PolicyError) as exc:
        parse_policies({"_schema_ver": 99, "stage_map": {"a": "b"}, "rules": [], "tuning": {}},
                       _contract())
    assert exc.value.code == "P01"


def test_parse_policies_unknown_condition_field():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"bogus": 1}, "decision": {"key": "x"}}],
            "tuning": {},
        }, _contract())
    assert exc.value.code == "P04"


def test_parse_policies_bad_source():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"stage": "hall"},
                       "decision": {"source": "not_a_source"}}],
            "tuning": {},
        }, _contract())
    assert exc.value.code == "P03"


def test_parse_policies_bad_op():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"cooldown": {"between": 1}},
                       "decision": {"key": "x"}}],
            "tuning": {},
        }, _contract())
    assert exc.value.code == "P05"


def test_parse_policies_stage_not_in_map():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"stage": "ghost"}, "decision": {"key": "x"}}],
            "tuning": {},
        }, _contract())
    assert exc.value.code == "P04"


# ------------------------------------------------------------------
# §4.4 P4d 契约化：EngineContract fail-closed + 异域合同直跑（域中性证明）
# ------------------------------------------------------------------


def _alt_contract_doc() -> dict:
    """一个与鉴宝无关的最小异域合同：字段、key、effect 全换成测试自造词汇。"""
    return {
        "facts": ["phase", "door_state", "frame_counter"],
        "state_fields": ["lever_pos", "frame_counter"],
        "algo_fields": ["phase"],
        "decision_sources": [],
        "effects": ["lever_reset"],
        "wait_keys": ["hall_waiting", "fatal"],
        "fallback": {"key": "hall_waiting", "hint": "等待"},
        "auto_effects": {},
        "passthrough_effects": ["lever_reset"],
        "derived": [],
        "tuning_keys": {"policy": ["cool"]},
    }


def test_engine_contract_parses_minimal_domain():
    c = parse_engine_contract(_alt_contract_doc())
    assert c.facts == frozenset({"phase", "door_state", "frame_counter"})
    assert c.fallback_key == "hall_waiting"


@pytest.mark.parametrize("mutate, path_kw", [
    (lambda d: d.pop("facts"), "facts"),
    (lambda d: d.update(fallback={"key": "x"}), "fallback"),
    (lambda d: d.update(derived=[{"field": "nope", "op": "elapsed", "from": "lever_pos"}]), "derived"),
    (lambda d: d.update(auto_effects={"ghost": "not_an_effect"}), "auto_effects"),
])
def test_engine_contract_fail_closed(mutate, path_kw):
    doc = _alt_contract_doc()
    mutate(doc)
    with pytest.raises(PolicyError) as exc:
        parse_engine_contract(doc)
    assert exc.value.code == "P01"
    assert path_kw in exc.value.path


def test_alt_domain_plan_decides_with_its_own_contract():
    """引擎域中性：异域合同 + 异域规则直编直跑，鉴宝词汇零参与。"""
    contract = parse_engine_contract(_alt_contract_doc())
    policies = parse_policies({
        "_schema_ver": 1,
        "stage_map": {"foyer": "前厅"},
        "rules": [
            {"id": "open", "when": {"phase": "foyer", "door_state": "locked"},
             "decision": {"key": "open_door_btn", "effect": "lever_reset"}},
        ],
        "tuning": {"policy": {"cool": 3}},
    }, contract)
    plan = compile_plan(policies, {"open_door_btn": object()})
    state = StateSnapshot.projection(
        {"frame_counter": 7}, fields=contract.state_fields)
    facts = DecisionFacts.freeze(
        state_snapshot=state, outputs={"phase": "foyer"}, frame_counter=7,
        contract=contract)
    dec = plan.decide(
        DecisionFacts(values={**facts.projection(), "door_state": "locked"}))
    assert dec.key == "open_door_btn"
    assert dec.side_effects == ("lever_reset",)
    assert plan.decide(facts).key == "hall_waiting"


# ------------------------------------------------------------------
# §5 P1d：阶段决策绝对断言（同一 facts → 编译计划输出锁值）
# ------------------------------------------------------------------


def _compile() -> PolicyPlan:
    nav = _load_nav()
    assert nav.policies is not None
    return compile_plan(nav.policies, nav.spec)


def _decide_key(plan: PolicyPlan, **kw) -> str:
    return plan.decide(_make_facts(**kw)).key


def test_plan_stage_decisions():
    """各阶段锚点/兜底行为（§4.3 规则顺序敏感：冷却规则全局优先）。"""
    plan = _compile()
    assert _decide_key(plan, stage="hall", frame=1) == "hall_peak_appraise_card"
    assert _decide_key(plan, stage="activity", frame=1) == "goto_appraise_btn"
    assert _decide_key(plan, stage="matching", frame=1) == "stage_waiting"
    assert _decide_key(plan, stage="auction_result", frame=1) == "stage_waiting"
    # settle：首点跳动画 / 数据齐真领取
    assert _decide_key(plan, stage="settle", frame=1, clicked_once=False) == "settle_collect_red_btn"
    assert _decide_key(plan, stage="settle", frame=2, clicked_once=False, settle_income=5000) == "settle_collect_red_btn"
    # popup：盲点分支（skip_cycle=0）走点击，其余等待
    assert _decide_key(plan, stage="popup", frame=9) == "popup_high_continue"
    assert _decide_key(plan, stage="popup", frame=7) == "popup_waiting"
    # 无上游决策的 defer 阶段 → 各自 fallback_key 等待；未知阶段 → 全局兜底
    assert _decide_key(plan, stage="session", frame=1) == "session_waiting"
    assert _decide_key(plan, stage="appraiser", frame=1) == "appraiser_waiting"
    assert _decide_key(plan, stage="bid", frame=1) == "bid_waiting"
    assert _decide_key(plan, stage=None, frame=1) == "stage_waiting"


def test_plan_deferred_sources_passthrough():
    """defer 阶段：上游决策 dict 有 key → 透传；无 → 兜底等待。"""
    plan = _compile()
    facts = _make_facts(
        stage="session", frame=1,
        session_decision={"key": "session_start_match_btn", "hint": "开始匹配"},
    )
    d = plan.decide(facts)
    assert d.key == "session_start_match_btn"
    assert d.source == "session_decision"
    facts = _make_facts(
        stage="appraiser", frame=1,
        appraiser_decision={"key": "appraiser_p1_caroline", "hint": "选她"},
    )
    assert plan.decide(facts).key == "appraiser_p1_caroline"
    facts = _make_facts(
        stage="bid", frame=1,
        bidding_decision={"key": "bid_main_red_btn", "hint": "点出价"},
    )
    assert plan.decide(facts).key == "bid_main_red_btn"


def test_plan_settle_variants():
    """settle 分支矩阵：首点/数据齐/等待/重试/致命（重试节奏按墙钟毫秒）。"""
    plan = _compile()
    # 点击后收入未读出、距上次点击未到重试间隔 → 等待 OCR
    d = plan.decide(_make_facts(stage="settle", frame=3, clicked_once=True, settle_income=None,
                                now_ms=1_000, settle_skip_since_ms=1_000))
    assert d.key == "dividend_waiting"
    assert d.fatal is None and d.side_effects == ()
    # 已过重试间隔且重试次数未耗尽 → 重试点击 + settle_skip_retry 副作用
    d = plan.decide(_make_facts(stage="settle", frame=20, clicked_once=True, settle_income=None,
                                now_ms=60_000, settle_skip_since_ms=1_000, retry_count=1))
    assert d.key == "settle_collect_red_btn"
    assert d.side_effects == ("settle_skip_retry",) and d.fatal is None
    # 已过重试间隔且重试耗尽 → fatal 终止指令
    d = plan.decide(_make_facts(stage="settle", frame=30, clicked_once=True, settle_income=None,
                                now_ms=60_000, settle_skip_since_ms=1_000, retry_count=10))
    assert d.key == "settle_collect_red_btn"
    assert d.fatal is not None and "跳过动画" in d.fatal


def test_plan_popup_variants():
    """弹窗分支矩阵：今日最高（就绪/超时/等待）/彩蛋/OCR 中/盲点/冷却短路。"""
    plan = _compile()
    base = dict(stage="popup", frame=6)
    expect = [
        (dict(base, popup_kind="daily_high_banner", daily_high_score=12345), "popup_high_continue"),
        (dict(base, popup_kind="daily_high_banner"), "popup_waiting"),
        (dict(base, popup_kind="daily_high_banner", reward_enter_frame=1), "popup_waiting"),
        (dict(base, popup_kind="egg_reward_title", egg_read_done=True), "popup_reward_continue"),
        (dict(base, popup_kind="egg_reward_title"), "popup_waiting"),
        (dict(base, popup_kind="egg_reward_title", reward_enter_frame=1), "popup_waiting"),
        (dict(base, egg_reading=True, egg_read_done=True), "popup_reward_continue"),
        (dict(base, egg_reading=True), "popup_waiting"),
        (dict(base, egg_reading=True, reward_enter_frame=1), "popup_waiting"),
    ]
    for kw, want in expect:
        assert _decide_key(plan, **kw) == want, f"facts={kw}"


def test_plan_cooldown_boundary():
    """cooldown>0 全局短路边界：冷却期内任意阶段都返回等待意图，不产出点击（§4.3）。"""
    plan = _compile()
    # 冷却期跨阶段（点击成功后检测器延迟切阶段/误判回退的窗口）
    for stage in ("hall", "activity", "settle", "bid", "session", "appraiser"):
        for frame in (10, 11):
            d = plan.decide(_make_facts(stage=stage, frame=frame, clicked_once=True, cooldown=3))
            assert d.key == "popup_click_cooldown", f"stage={stage} frame={frame}"
            assert d.side_effects == ("popup_cooldown_decr",)
    # 冷却递减序列：5→4→3→2→1→0（恢复决策）
    for cd in (5, 4, 3, 2, 1):
        d = plan.decide(_make_facts(stage="activity", frame=20, cooldown=cd))
        assert d.key == "popup_click_cooldown" and d.side_effects == ("popup_cooldown_decr",)
    # cooldown 归零后恢复正常决策
    assert plan.decide(_make_facts(stage="activity", frame=25, cooldown=0)).key == "goto_appraise_btn"
    # settle 真领取后冷却窗口（income 已读出 + cooldown>0）：等待而非重复点击
    d = plan.decide(_make_facts(stage="settle", frame=30, clicked_once=True,
                                settle_income=5000, cooldown=4))
    assert d.key == "popup_click_cooldown"


def test_plan_fallback_key():
    plan = _compile()
    d = plan.decide(_make_facts(stage="session", frame=1, session_decision=None))
    assert d.key == "session_waiting"
    d = plan.decide(_make_facts(stage="bid", frame=1, bidding_decision=None))
    assert d.key == "bid_waiting"
    d = plan.decide(_make_facts(stage="unknown_stage", frame=1))
    assert d.key == _contract().fallback_key


def test_tuning_reference_baked_at_compile_time():
    """`@tuning_key` 条件在编译期烘焙：改 tuning 即改规则阈值（P1 调参上纸）。"""
    nav = _load_nav()
    assert nav.policies is not None
    plan = compile_plan(nav.policies, nav.spec)
    # 烘焙后条件里不应残留 @ 引用
    for rule in plan.rules:
        for cond in rule.conditions:
            assert not (isinstance(cond.value, str) and cond.value.startswith("@")), (
                f"规则 {rule.id} 的条件值未烘焙：{cond.value!r}"
            )
    # 语义验证：settle 重试间隔 = tuning.policy.settle_skip_retry_ms（墙钟毫秒口径）
    def decide(elapsed_ms: int, retry_count: int = 1) -> str:
        facts = _make_facts(
            stage="settle", frame=10, clicked_once=True,
            now_ms=100_000 + elapsed_ms, settle_skip_since_ms=100_000,
            retry_count=retry_count,
        )
        return plan.decide(facts).key

    interval = int(nav.policies.tuning["policy"]["settle_skip_retry_ms"])
    assert decide(interval - 1) == "dividend_waiting"        # 未到间隔
    assert decide(interval) == "settle_collect_red_btn"      # 到间隔 → 重试点击
    # 改 tuning → 重编译 → 阈值跟着变（证明非字面量硬编码）
    nav.policies.tuning["policy"]["settle_skip_retry_ms"] = 500
    plan5 = compile_plan(nav.policies, nav.spec)
    assert plan5.decide(_make_facts(stage="settle", frame=10, clicked_once=True,
                                    now_ms=100_499, settle_skip_since_ms=100_000)
                        ).key == "dividend_waiting"
    assert plan5.decide(_make_facts(stage="settle", frame=10, clicked_once=True,
                                    now_ms=100_500, settle_skip_since_ms=100_000)
                        ).key == "settle_collect_red_btn"


def test_tuning_unknown_reference_rejected():
    policies = parse_policies({
        "_schema_ver": 1,
        "stage_map": {"hall": "游戏大厅"},
        "rules": [{"id": "r1", "when": {"cooldown": {"gte": "@not_defined"}},
                   "decision": {"key": "x"}}],
        "tuning": {"policy": {}},
    }, _contract())
    issues = validate_policy_document(policies, {})
    assert any(code == "P05" for code, _, _, _ in issues)


# ------------------------------------------------------------------
# §5.1 P1e：policies 唯一决策源（缺失/非法 = 启动失败，无回退）
# ------------------------------------------------------------------


def test_policies_missing_is_startup_failure(monkeypatch, tmp_path):
    """P1e（v4）：policy.json 缺 policy 段 → 模块启动失败（不允许静默回退代码常量）。"""
    import json as _json

    doc = _json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    doc.pop("policy", None)
    broken = tmp_path / "treasure.policy.json"
    broken.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    try:
        from maaracing_master.plugins.treasure import module as tm
    except Exception:
        pytest.skip("需完整运行时依赖（maa/cv2 等），CI 轻依赖环境下跳过")

    from maaracing_master.core.navkit.v4_source import load_nav_source
    monkeypatch.setattr(tm, "nav_source", lambda: load_nav_source(broken))
    tm._policy_tuning.cache_clear()
    m = tm.TreasureModule(None)
    with pytest.raises(Exception) as exc:
        m._init_policy_stack()
    assert "policy" in str(exc.value)
    tm._policy_tuning.cache_clear()


def test_policies_invalid_is_startup_failure(monkeypatch, tmp_path):
    """P1e（v4）：policy 段结构非法（schema_ver 错）→ 启动失败。"""
    import json as _json

    doc = _json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    doc["policy"]["schema_ver"] = 99
    broken = tmp_path / "treasure.policy.json"
    broken.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    try:
        from maaracing_master.plugins.treasure import module as tm
    except Exception:
        pytest.skip("需完整运行时依赖（maa/cv2 等），CI 轻依赖环境下跳过")

    from maaracing_master.core.navkit.v4_source import load_nav_source
    monkeypatch.setattr(tm, "nav_source", lambda: load_nav_source(broken))
    tm._policy_tuning.cache_clear()
    m = tm.TreasureModule(None)
    with pytest.raises(Exception):
        m._init_policy_stack()
    tm._policy_tuning.cache_clear()


def test_policies_bad_schema_ver_parse_fails():
    """P1e（引擎层）：结构错误（schema_ver 错）在 `parse_policies` 构造期即抛 P01。"""
    with pytest.raises(PolicyError) as exc:
        parse_policies({"_schema_ver": 99, "stage_map": {}, "rules": [], "tuning": {}},
                       _contract())
    assert exc.value.code == "P01"


# ------------------------------------------------------------------
# §4.2：validate_policy_document（P06/P07/P09）
# ------------------------------------------------------------------


def test_validate_policy_document_reports_warnings():
    nav = _load_nav()
    assert nav.policies is not None
    issues = validate_policy_document(nav.policies, nav.spec)
    codes = {c for c, _, _, _ in issues}
    assert codes <= {"P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08", "P09"}
    assert all(level != "error" or code in {"P02", "P08", "P05"} for code, level, _, _ in issues)


def test_validate_policy_document_strict_upgrades_warnings():
    nav = _load_nav()
    assert nav.policies is not None
    issues = validate_policy_document(nav.policies, nav.spec, strict=True)
    assert all(level == "error" for _, level, _, _ in issues)


def test_tuning_unknown_key_rejected():
    policies = parse_policies({
        "_schema_ver": 1,
        "stage_map": {"hall": "游戏大厅"},
        "rules": [{"id": "r1", "when": {"stage": "hall"}, "decision": {"key": "hall_peak_appraise_card"}}],
        "tuning": {"perception": {"bogus_key": 1}},
    }, _contract())
    issues = validate_policy_document(policies, {"hall_peak_appraise_card": _DummyAnchor()})
    assert any(code == "P01" for code, _, _, _ in issues)


class _DummyAnchor:
    """仅满足 validate 对锚点引用闭合的桩（rect 不参与语义检查）。"""

    def __init__(self) -> None:
        self.rect = None


def test_assets_policies_present():
    nav = _load_nav()
    assert nav.policies is not None
    assert len(nav.policies.rules) >= 20
    assert "hall" in nav.policies.stage_map
    assert set(nav.policies.tuning) == {"perception", "policy", "execution"}



