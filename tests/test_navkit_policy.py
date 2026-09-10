#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NavKit P1 决策策略层单测（纯标准库，CI 只装 pytest 即可运行）。

覆盖 docs/plan/NAVKIT_P1_PLAN.md 的契约：
- P0-6 DecisionFacts 冻结快照 / 派生事实（retry_elapsed / reward_elapsed / skip_cycle）
- P0-7 StateSnapshot 封闭白名单投影（未知字段 fail-closed）
- §4 schema：parse_policies 结构错误（P01-P05）
- §4.3 PolicyPlan 行为语义：各阶段决策/兜底/透传/冷却边界（单轨绝对断言）
- §4.2 validate_policy_document：P06/P07/P09 告警与 strict 升级
- §5.1 P1e fail-closed：policies 缺失/非法 = 启动失败
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from maaracing_assistant.core.navkit import (
    DEFAULT_FALLBACK_KEY,
    DecisionFacts,
    DecisionSnapshot,
    NavKitError,
    PolicyError,
    PolicyPlan,
    StateSnapshot,
    compile_plan,
    parse_policies,
    validate_policy_document,
)

_ASSETS_PATH = (
    Path(__file__).resolve().parents[1]
    / "maaracing_assistant/plugins/treasure/resources/config/treasure_assets.json"
)


def _load_assets():
    from maaracing_assistant.core.navkit import Assets

    return Assets.load(_ASSETS_PATH, module="treasure")


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
    settle_skip_since: int = 0,
    cooldown: int = 0,
    daily_high_score: int | None = None,
    egg_reading: bool = False,
    egg_read_done: bool = False,
    reward_enter_frame: int = 0,
) -> DecisionFacts:
    state = StateSnapshot.projection({
        "frame_counter": frame,
        "settle_income": settle_income,
        "clicked_once": clicked_once,
        "retry_count": retry_count,
        "settle_skip_since": settle_skip_since,
        "cooldown": cooldown,
        "daily_high_score": daily_high_score,
        "egg_reading": egg_reading,
        "egg_read_done": egg_read_done,
        "reward_enter_frame": reward_enter_frame,
    })
    outputs = {
        "stage": stage,
        "popup_kind": popup_kind,
        "session_decision": session_decision,
        "appraiser_decision": appraiser_decision,
        "bidding_decision": bidding_decision,
    }
    return DecisionFacts.freeze(state_snapshot=state, outputs=outputs, frame_counter=frame)


# ------------------------------------------------------------------
# P0-7：StateSnapshot 白名单投影
# ------------------------------------------------------------------


def test_state_snapshot_projection_rejects_unknown_fields():
    with pytest.raises(PolicyError) as exc:
        StateSnapshot.projection({"frame_counter": 1, "unknown_field": 3})
    assert exc.value.code == "P04"


def test_state_snapshot_projection_accepts_all_fields():
    snap = StateSnapshot.projection({
        "frame_counter": 1, "settle_income": None, "clicked_once": False,
        "retry_count": 0, "settle_skip_since": 0, "cooldown": 0,
        "daily_high_score": None, "egg_reading": False, "egg_read_done": False,
        "reward_enter_frame": 0,
    })
    assert snap.values["frame_counter"] == 1


# ------------------------------------------------------------------
# P0-6：DecisionFacts 冻结 + 派生事实
# ------------------------------------------------------------------


def test_decision_facts_derived_fields():
    facts = _make_facts(stage="settle", frame=20, clicked_once=True, settle_skip_since=5)
    assert facts.get("retry_elapsed") == 15
    assert facts.get("skip_cycle") == 20 % 3
    assert facts.get("frame_counter") == 20


def test_decision_facts_rejects_unknown_outputs():
    with pytest.raises(PolicyError) as exc:
        DecisionFacts.freeze(
            state_snapshot=StateSnapshot.projection({}),
            outputs={"stage": "hall", "not_a_fact": 1},
            frame_counter=1,
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
        parse_policies({"_schema_ver": 99, "stage_map": {"a": "b"}, "rules": [], "tuning": {}})
    assert exc.value.code == "P01"


def test_parse_policies_unknown_condition_field():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"bogus": 1}, "decision": {"key": "x"}}],
            "tuning": {},
        })
    assert exc.value.code == "P04"


def test_parse_policies_bad_source():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"stage": "hall"},
                       "decision": {"source": "not_a_source"}}],
            "tuning": {},
        })
    assert exc.value.code == "P03"


def test_parse_policies_bad_op():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"cooldown": {"between": 1}},
                       "decision": {"key": "x"}}],
            "tuning": {},
        })
    assert exc.value.code == "P05"


def test_parse_policies_stage_not_in_map():
    with pytest.raises(PolicyError) as exc:
        parse_policies({
            "_schema_ver": 1,
            "stage_map": {"hall": "游戏大厅"},
            "rules": [{"id": "r1", "when": {"stage": "ghost"}, "decision": {"key": "x"}}],
            "tuning": {},
        })
    assert exc.value.code == "P04"


# ------------------------------------------------------------------
# §5 P1d：双轨等价（同一 facts → LegacyPolicy 与 PolicyEngine 同输出）
# ------------------------------------------------------------------


def _compile() -> PolicyPlan:
    assets = _load_assets()
    assert assets.policies is not None
    return compile_plan(assets.policies, assets.anchors)


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
    """settle 分支矩阵：首点/数据齐/等待/重试/致命。"""
    plan = _compile()
    # 点击后收入未读出、未超时 → 等待 OCR
    d = plan.decide(_make_facts(stage="settle", frame=3, clicked_once=True, settle_income=None))
    assert d.key == "dividend_waiting"
    assert d.fatal is None and d.side_effects == ()
    # 超时且重试次数未耗尽 → 重试点击 + settle_skip_retry 副作用
    d = plan.decide(_make_facts(stage="settle", frame=20, clicked_once=True, settle_income=None,
                                settle_skip_since=5, retry_count=1))
    assert d.key == "settle_collect_red_btn"
    assert d.side_effects == ("settle_skip_retry",) and d.fatal is None
    # 超时且重试耗尽 → fatal 终止指令
    d = plan.decide(_make_facts(stage="settle", frame=30, clicked_once=True, settle_income=None,
                                settle_skip_since=5, retry_count=3))
    assert d.key == "settle_collect_red_btn"
    assert d.fatal is not None and "重试 3 次" in d.fatal


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
    assert d.key == DEFAULT_FALLBACK_KEY


def test_tuning_reference_baked_at_compile_time():
    """`@tuning_key` 条件在编译期烘焙：改 tuning 即改规则阈值（P1 调参上纸）。"""
    assets = _load_assets()
    assert assets.policies is not None
    plan = compile_plan(assets.policies, assets.anchors)
    # 烘焙后条件里不应残留 @ 引用
    for rule in plan.rules:
        for cond in rule.conditions:
            assert not (isinstance(cond.value, str) and cond.value.startswith("@")), (
                f"规则 {rule.id} 的条件值未烘焙：{cond.value!r}"
            )
    # 语义验证：settle 超时重试帧数 = tuning.policy.settle_skip_retry_frames
    def decide(frame: int, retry_count: int = 1) -> str:
        facts = _make_facts(
            stage="settle", frame=frame, clicked_once=True,
            settle_skip_since=1, retry_count=retry_count,
        )
        return plan.decide(facts).key

    frames = int(assets.policies.tuning["policy"]["settle_skip_retry_frames"])
    assert decide(frame=frames) == "dividend_waiting"   # elapsed = frames - 1，未超时
    assert decide(frame=frames + 1) == "settle_collect_red_btn"  # elapsed = frames，超时重试
    # 改 tuning → 重编译 → 阈值跟着变（证明非字面量硬编码）
    assets.policies.tuning["policy"]["settle_skip_retry_frames"] = 5
    plan5 = compile_plan(assets.policies, assets.anchors)
    # frame=5 → elapsed=4 < 5 仍等待；frame=6 → elapsed=5 触发重试
    assert plan5.decide(_make_facts(stage="settle", frame=5, clicked_once=True,
                                    settle_skip_since=1)).key == "dividend_waiting"
    assert plan5.decide(_make_facts(stage="settle", frame=6, clicked_once=True,
                                    settle_skip_since=1)).key == "settle_collect_red_btn"


def test_tuning_unknown_reference_rejected():
    policies = parse_policies({
        "_schema_ver": 1,
        "stage_map": {"hall": "游戏大厅"},
        "rules": [{"id": "r1", "when": {"cooldown": {"gte": "@not_defined"}},
                   "decision": {"key": "x"}}],
        "tuning": {"policy": {}},
    })
    issues = validate_policy_document(policies, {})
    assert any(code == "P05" for code, _, _, _ in issues)


# ------------------------------------------------------------------
# §5.1 P1e：policies 唯一决策源（缺失/非法 = 启动失败，无回退）
# ------------------------------------------------------------------


def test_policies_missing_is_startup_failure(monkeypatch, tmp_path):
    """P1e：资产缺 policies 段 → 模块启动失败（不允许静默回退代码常量）。"""
    import json as _json

    src = _ASSETS_PATH
    doc = _json.loads(src.read_text(encoding="utf-8"))
    doc.pop("policies", None)
    broken = tmp_path / "treasure_assets.json"
    broken.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    try:
        from maaracing_assistant.plugins.treasure import module as tm
    except Exception:
        pytest.skip("需完整运行时依赖（maa/cv2 等），CI 轻依赖环境下跳过")

    monkeypatch.setattr(tm, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("NAVKIT_SOURCE", "v3")
    tm._policy_tuning.cache_clear()
    m = tm.TreasureModule(None)
    with pytest.raises(Exception) as exc:
        m._init_policy_stack()
    assert "policies" in str(exc.value)
    tm._policy_tuning.cache_clear()


def test_policies_invalid_is_startup_failure(monkeypatch, tmp_path):
    """P1e：policies 结构非法（schema_ver 错）→ 启动失败。"""
    import json as _json

    src = _ASSETS_PATH
    doc = _json.loads(src.read_text(encoding="utf-8"))
    doc["policies"] = {"_schema_ver": 99, "stage_map": {}, "rules": [], "tuning": {}}
    broken = tmp_path / "treasure_assets.json"
    broken.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    try:
        from maaracing_assistant.plugins.treasure import module as tm
    except Exception:
        pytest.skip("需完整运行时依赖（maa/cv2 等），CI 轻依赖环境下跳过")

    monkeypatch.setattr(tm, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("NAVKIT_SOURCE", "v3")
    tm._policy_tuning.cache_clear()
    m = tm.TreasureModule(None)
    with pytest.raises(Exception):
        m._init_policy_stack()
    tm._policy_tuning.cache_clear()


def test_policies_missing_assets_load_fails():
    """P1e（schema 层）：`Assets` 的 `policies` 缺失=启动失败由调用方强制，
    但结构错误（`_schema_ver` 错）在 `Assets.load` 构造期即抛。"""
    import json as _json

    src = _ASSETS_PATH
    doc = _json.loads(src.read_text(encoding="utf-8"))
    doc["policies"] = {"_schema_ver": 99, "stage_map": {}, "rules": [], "tuning": {}}
    from maaracing_assistant.core.navkit import Assets

    with pytest.raises(NavKitError) as exc:
        Assets.from_document(doc, module="treasure")
    assert exc.value.code == "P01"


# ------------------------------------------------------------------
# §4.2：validate_policy_document（P06/P07/P09）
# ------------------------------------------------------------------


def test_validate_policy_document_reports_warnings():
    assets = _load_assets()
    assert assets.policies is not None
    issues = validate_policy_document(assets.policies, assets.anchors)
    codes = {c for c, _, _, _ in issues}
    assert codes <= {"P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08", "P09"}
    assert all(level != "error" or code in {"P02", "P08", "P05"} for code, level, _, _ in issues)


def test_validate_policy_document_strict_upgrades_warnings():
    assets = _load_assets()
    assert assets.policies is not None
    issues = validate_policy_document(assets.policies, assets.anchors, strict=True)
    assert all(level == "error" for _, level, _, _ in issues)


def test_tuning_unknown_key_rejected():
    policies = parse_policies({
        "_schema_ver": 1,
        "stage_map": {"hall": "游戏大厅"},
        "rules": [{"id": "r1", "when": {"stage": "hall"}, "decision": {"key": "hall_peak_appraise_card"}}],
        "tuning": {"perception": {"bogus_key": 1}},
    })
    issues = validate_policy_document(policies, {"hall_peak_appraise_card": _DummyAnchor()})
    assert any(code == "P01" for code, _, _, _ in issues)


class _DummyAnchor:
    """仅满足 validate 对锚点引用闭合的桩（rect 不参与语义检查）。"""

    def __init__(self) -> None:
        self.rect = None


def test_assets_policies_present():
    assets = _load_assets()
    assert assets.policies is not None
    assert len(assets.policies.rules) >= 20
    assert "hall" in assets.policies.stage_map
    assert set(assets.policies.tuning) == {"perception", "policy", "execution"}



