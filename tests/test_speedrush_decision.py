# -*- coding: utf-8 -*-
"""step 4 决策层的回归锁：配置 fail-loud、校验合取、评分偏序、FSM 全转移矩阵。

矩阵按设计稿 v2 §二逐格上锁（含取消/超时/改判/恢复/升级五类收尾路径）；
评分只锁**偏序**不锁绝对值（验收 3）；数值场景以 decision.json 起值反推
（helper 里注释了各场景过/不过哪道门的算式），改参数会红——这是刻意的：
参数与测试场景是一对契约面。
"""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import (
    DecisionEngine, LateralSafety, Scorer, ValidationWatch)
from maaracing_master.plugins.speedrush.traffic import (
    OUTCOME_GHOST, OUTCOME_LOST, OUTCOME_PASS, OUTCOME_PRESUMED_PASS, CarView,
    PassEvent)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, DistanceSource, MetricCarState,
    PerceptionHealth, TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    _read_gate0, load_calib, t_meet_car_s)
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH, GATE0_FILE as GATE0_PATH

DT = 0.05          # 20Hz 名义 tick
# 本文件锁的是 legacy 决策语义：闸值自钉关，不随部署 decision.json 的
# trajectory_sampling 运行态漂（验收期部署文件开闸，语义测试不跟着翻）。
_CFG_BASE = _read_decision(
    __import__("maaracing_master.plugins.speedrush", fromlist=["DECISION_FILE"])
    .DECISION_FILE)
CFG = replace(_CFG_BASE,
              mode=replace(_CFG_BASE.mode, trajectory_sampling=False),
              metric_cars=replace(_CFG_BASE.metric_cars, fusion=False),
              # 走廊收益场闸显式钉死（legacy 行为测试不继承部署态——部署闸
              # 在 rollout 中翻转 shadow→control，测试基线必须自持）
              corridor_reach=replace(_CFG_BASE.corridor_reach,
                                     enabled=False, control=False))


# ---------- 观测构造 helper（场景算式见各测试注释） ----------

def _obs(*, fid=1, groups=(), targets=(), presence=None, alive=True, fresh=True,
         geometry=True, transition=False, boundary=None, fid_hz=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(alive, fresh, geometry, presence, transition),
        boundary=boundary, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=fid_hz)


def _members(fid, gid, n, x, cy, rel, conf=0.85):
    return [TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n)]


def _group(gid, fid, n, x, cy, rel, conf=0.85):
    ms = _members(fid, gid, n, x, cy, rel, conf)
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, tuple(ms)


# 场景 A（默认"良候选"）：n=3 conf=0.85 x=0.5 cy=500 rel=5
#   流口径（全场 k 中位 = rel·hz/(cy−y_h)²）：k=100/178²≈3.16e-3；
#   t_miss=∫=(1/k)·(1/181.8−1/391.8)≈0.93s；life=e^−0.93/5≈0.83
#   value=30·3·0.6=54 → 44.8 −shift 5 ≈ 39.8 ≥ min 20 ✓；
#   need=0.3+0.4·0.5+0.13+0.15=0.78 < 0.93 ✓可行
GOOD = dict(gid=1, n=3, x=0.5, cy=500, rel=5)


def _good_obs(fid=1, **kw):
    p = {**GOOD, **kw}
    gid = p.pop("gid")
    g, ms = _group(gid, fid, **p)
    return _obs(fid=fid, groups=(g,), targets=ms)


# ---------- 配置 fail-loud ----------

def _decision_dict():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("schema_version"), "schema_version"),
    (lambda d: d["hysteresis"].pop("switch_margin"), "switch_margin"),
    (lambda d: d["validate"].update(t_recover_s=99.0), "t_conserve_max"),
    (lambda d: d["hysteresis"].update(dead_zone_lane=3.0), "死区"),
    (lambda d: d["mode"].update(allow_car_graze=True), "allow_car_graze"),
    (lambda d: d["mode"].update(allow_all_moves="true"), "非布尔"),
    (lambda d: d["scoring"].update(conf_floor=1.5), "越界"),
    (lambda d: d["scoring"].update(conf_floor=0.9), "折扣区倒置"),
    (lambda d: d["control"].update(frame_rate_hz=0), "越界"),
    (lambda d: d.pop("planner"), "planner"),
    (lambda d: d["planner"].update(lookahead_tau_s=0.05), "lookahead"),
    (lambda d: d["planner"].update(rate_limit_raw=99999.0), "限幅"),
    (lambda d: d.pop("overtake"), "overtake"),
    (lambda d: d["overtake"].update(value_limit=10.0), "倒置"),
    (lambda d: d["overtake"].update(d_hold_lane=0.8), "矛盾"),
    (lambda d: d["control"].pop("abort_settle_v_lane_s"), "abort_settle"),
    (lambda d: d["overtake"].pop("t_cool_pass_s"), "t_cool_pass"),
    (lambda d: d["overtake"].pop("ttc_dodge_s"), "ttc_dodge"),
    (lambda d: d["validate"].pop("lat_veto_gap_lane"), "lat_veto_gap"),
    (lambda d: d["validate"].update(lat_veto_gap_lane=0.7), "贴窗"),
    (lambda d: d["validate"].pop("lat_veto_horizon_s"), "lat_veto_horizon"),
    (lambda d: d["traffic"].pop("v_lat_ema_alpha"), "v_lat_ema"),
])
def test_decision_fail_loud(tmp_path, mutate, frag):
    d = _decision_dict()
    mutate(d)
    p = tmp_path / "decision.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        _read_decision(p)
    assert frag in str(e.value)


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("a_x"), "a_x"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.update(v_ego=300.0), "分母退化"),        # v_ego < y_h
    (lambda d: d.update(y_h="地平线"), "非数值"),
])
def test_gate0_fail_loud(tmp_path, mutate, frag):
    d = json.loads(GATE0_PATH.read_text(encoding="utf-8"))
    mutate(d)
    p = tmp_path / "gate0.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        _read_gate0(p)
    assert frag in str(e.value)


def test_real_sources_load():
    assert CFG.validate.t_recover_s < CFG.validate.t_conserve_max_s
    cal = load_calib()
    assert cal.v_ego - cal.y_h > 0 and cal.min_denom > 0


# ---------- 校验合取（§四：五信号合取，任何单项不作数） ----------

def test_watch_frame_stale_fatal():
    w = ValidationWatch(CFG)
    r = w.check(_obs(fresh=False), DT)
    assert r.fatal == "frame_stale"


def test_watch_stale_during_transition_not_fatal():
    w = ValidationWatch(CFG)
    assert w.check(_obs(fresh=False, transition=True), DT).fatal is None


def test_watch_empty_needs_conjunction():
    """枯竭计时要求 alive∧fresh∧geometry∧¬transition 全立——空旷/过渡/半死不计时。"""
    w = ValidationWatch(CFG)
    for _ in range(int(CFG.validate.t_empty_s / DT)):
        assert w.check(_obs(presence=False, geometry=False), DT).fatal is None
    # 合取恢复后从头计时（旧账不追溯）
    for i in range(int(CFG.validate.t_empty_s / DT) - 1):
        assert w.check(_obs(presence=False), DT).fatal is None
    assert w.check(_obs(presence=False), DT).fatal == "target_empty"


def test_watch_target_presence_resets_timer():
    w = ValidationWatch(CFG)
    for _ in range(10):
        w.check(_obs(presence=False), DT)
    assert w.check(_good_obs(), DT).fatal is None      # 有目标：清零
    for _ in range(int(CFG.validate.t_empty_s / DT) - 1):
        assert w.check(_obs(presence=False), DT).fatal is None


def test_watch_cy_jump_reversal():
    w = ValidationWatch(CFG)
    g, ms = _group(1, 1, 3, 0.5, 500, 10)
    w.check(_obs(fid=1, groups=(g,), targets=ms), DT)
    g2, ms2 = _group(1, 2, 3, 0.5, 700, 10)            # 同 id 全组 +200px ≫ 90
    assert w.check(_obs(fid=2, groups=(g2,), targets=ms2), DT).fatal == "cy_jump"


# ---------- 评分偏序（验收 3：锁排序语义不锁绝对值） ----------

def _score(**kw):
    p = {**GOOD, **kw}
    gid = p.pop("gid")
    g, ms = _group(gid, 1, **p)
    s = Scorer(CFG, load_calib())
    return s.score(g, _obs(groups=(g,), targets=ms))


def test_score_monotone_in_count():
    assert _score(n=4).score > _score(n=3).score


def test_score_group_floor_partial_view():
    """组下限（RULES 2026-10-14 定案）：观测 1~2 枚是残缺视图（距离/遮挡/置信
    门削剩的头部），价值按整组 ≥3 枚计，不按观测枚数折算——n=1 组按 30 分
    基值再被车道惩罚一压连 min_score 都过不了（20261014 实机 score 拒
    106~119 次/局、金币候选 ok 每局 1 拍）。锁：同几何下 n=1 与 n=3 同值
    （残缺视图按整组计）且过 min_score；n=4 仍严格更高（test_monotone 已锁）。"""
    def _g_with(n):
        ms = _members(1, 1, n, 0.5, 500, 5)
        g = CoinGroup(group_id=1, member_ids=tuple(m.id for m in ms),
                      observed_count=n, estimated_count=n, x_center=0.5,
                      x_span=0.0, cy_min=500, cy_max=500, conf_min=0.85,
                      partial_observation=False,
                      first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
        return g, ms

    s = Scorer(CFG, load_calib())
    g1, ms1 = _g_with(1)
    g3, ms3 = _g_with(3)
    s1 = s.score(g1, _obs(groups=(g1,), targets=ms1))
    s3 = s.score(g3, _obs(groups=(g3,), targets=ms3))
    assert s1 is not None and s3 is not None
    # 组值项逐值同（max(1,3)=3）；t_miss 的全场 k 随组员集中位略差，放宽到 2%
    assert s1.score == pytest.approx(s3.score, rel=0.02)
    assert s1.score >= CFG.hysteresis.min_score


def test_score_monotone_in_distance():
    # 流口径下速率随行号二次增长，远行目标必须配按场一致的 rel（同一 k）——
    # rel(cy) ∝ (cy−y_h)²，锚定 GOOD(cy=500, rel=5)；锁「越远时间越多」的序
    def _rel(cy):
        return 5.0 * (cy - load_calib().y_h) ** 2 / (500 - load_calib().y_h) ** 2
    assert _score(cy=620, rel=_rel(620)).score \
        > _score(cy=500).score > _score(cy=420, rel=_rel(420)).score


def test_score_penalizes_lateral_shift():
    assert _score(x=0.3).score > _score(x=1.5).score


def test_score_conf_ramp_and_floor():
    assert _score(conf=0.99).score > _score(conf=0.7).score
    # 20261017 教训：地板降到检测门槛 0.35（日落逆光场景 conf 帧间 0.42~0.83
    # 剧烈跳，0.5 切在区间正中把肉眼可见的币整组拒掉）——0.4 进低权坡度不拒
    assert _score(conf=0.4) is not None      # 低权计入（折扣代替拒收）
    assert _score(conf=0.4).score < _score(conf=0.7).score
    assert _score(conf=0.34) is None         # 地板（=检测门槛）下不低信决策
    held = CoinGroup(group_id=9, member_ids=(1,), observed_count=1, estimated_count=1,
                     x_center=0.5, x_span=0, cy_min=500, cy_max=500, conf_min=0.0,
                     partial_observation=True, first_seen_fid=0, last_seen_fid=0,
                     validity_until_fid=9)
    assert Scorer(CFG, load_calib()).score(held, _obs(groups=(held,))) is None


def test_score_rejects_receding_target():
    """rel≤0（远离）：机会视为无限远 → 分数被折扣压到不可选（−cost）。"""
    s = _score(rel=-5)
    assert s is None or s.score < CFG.hysteresis.min_score


def test_score_survives_member_id_churn_via_field_flow():
    """换基主锁（2026-10-02）：组员 id 全断（不在 targets 里，逐轨账本旧法
    必然饿死成 life=0）但场内另有健康 track——地面流是全场属性，k 从横截面
    拟合出来，金币照常可选。旧逐轨实现下此锁必红。"""
    g, _ = _group(7, 1, 3, x=0.5, cy=500, rel=0.0)     # 组员 rel=0 且不在 targets
    # 场内两个健康 track（行号不同、速率按同一 k=100/178² 流场自洽）
    flow = [TrackedTarget(
        id=90 + i, kind="coin", x_lane=-0.8 + 1.6 * i, x_sigma=0.1,
        cy=cy, w=20, h=20, conf=0.9,
        rel_approach=100.0 * (cy - load_calib().y_h) ** 2 / (500 - load_calib().y_h) ** 2
        / CFG.control.frame_rate_hz,
        first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
        for i, cy in enumerate((600, 450))]
    s = Scorer(CFG, load_calib()).score(g, _obs(targets=flow))
    assert s is not None and s.score > CFG.hysteresis.min_score


def test_score_blackout_keeps_failsafe_discount():
    """全场无任何速率测量（断供黑视）：无限远折扣 → 不可选——安全默认与
    逐轨断供时代同形，不许在无证据时臆造可达性。"""
    g, _ = _group(7, 1, 3, x=0.5, cy=500, rel=0.0)
    s = Scorer(CFG, load_calib()).score(g, _obs(targets=()))
    assert s is not None and s.score < CFG.hysteresis.min_score


# ---------- FSM 全转移矩阵（§二优先级逐格） ----------

def _eng(**over):
    import dataclasses
    # allow_all_moves / allow_overtake 是**部署开关**（V0/V1/V2），不是测试夹具：
    # 基座一律强制"横向开、超车关"（矩阵测试需能进 CHANGE，但超车默认关），
    # 与 decision.json 当前态解耦；no_moves→V0、overtake→V2 显式翻转。
    cfg = dataclasses.replace(CFG, mode=dataclasses.replace(
        CFG.mode, allow_all_moves=True, allow_overtake=False))
    if over.get("no_moves"):
        cfg = dataclasses.replace(cfg, mode=dataclasses.replace(
            cfg.mode, allow_all_moves=False))
    if over.get("overtake"):
        cfg = dataclasses.replace(cfg, mode=dataclasses.replace(
            cfg.mode, allow_overtake=True))
    if over.get("t_pass_max_s") is not None:
        cfg = dataclasses.replace(cfg, overtake=dataclasses.replace(
            cfg.overtake, t_pass_max_s=over["t_pass_max_s"]))
    return DecisionEngine(cfg)


def test_cruise_selects_feasible_candidate():
    e = _eng()
    out = e.update(_good_obs(), DT)
    assert out.state is DecisionState.CHANGE and out.reason == "select:score_win"
    assert out.move_allowed is True and out.target_id == 1


def test_cruise_low_score_holds():
    e = _eng()
    out = e.update(_good_obs(conf=0.4), DT)       # 低权坡度底（0.35 地板）后 < min_score
    assert out.state is DecisionState.CRUISE and "no_candidate" in out.reason


def test_cruise_infeasible_timing_holds():
    e = _eng()
    # 近处大横移：k=1000/325.8²≈9.4e-3 → t_miss=(1/k)(1/331.8−1/391.8)≈0.05s
    # ≪ need=0.3+0.88+0.28=1.46s
    out = e.update(_good_obs(x=2.2, cy=650, rel=50), DT)
    assert out.state is DecisionState.CRUISE


def test_change_timeout_finishes_with_cooling():
    """无反馈降级路：不进死区 → t_change_max 超时落位 + 冷却。"""
    e = _eng()
    e.update(_good_obs(), DT)                     # → CHANGE
    f = 2
    out = e.update(_good_obs(fid=f), DT)          # x 恒定 0.5，不进死区
    while not out.reason.startswith("done") and f < 200:
        f += 1
        out = e.update(_good_obs(fid=f), DT)
    assert out.state is DecisionState.CRUISE and out.reason == "done:change_timeout"
    nxt = e.update(_good_obs(fid=f + 1), DT)
    assert "cooling" in nxt.reason                # 冷却拒绝再起新变道


def test_change_converges_by_deadzone_proxy():
    e = _eng()
    o1 = _good_obs(fid=1)
    e.update(o1, DT)                              # → CHANGE demand=0.5
    for f in (2, 3):
        out = e.update(_good_obs(fid=f, x=0.1), DT)   # 目标 x 进死区（0.1<0.15 不严格…用 0.05）
    # x=0.05 才稳进死区；x=0.1 也在 0.15 内——两 tick 即收敛
    assert out.state is DecisionState.CRUISE and "converged:proxy" in out.reason


def test_change_converges_by_feedback():
    e = _eng()
    e.update(_good_obs(fid=1), DT)
    out = e.update(_good_obs(fid=2), DT, executed_lane=0.45)  # demand 0.5−死区0.15=0.35 已过
    assert "converged:feedback" in out.reason


def test_change_target_lost_aborts_then_cruise():
    e = _eng()
    e.update(_good_obs(fid=1), DT)                # CHANGE，组 validity=9
    out = e.update(_obs(fid=10, presence=True), DT)   # 组消失（过宽限）
    assert out.state is DecisionState.ABORT_CHANGE and out.reason == "cancel:target_lost"
    out = e.update(_obs(fid=11, presence=True), DT)   # 无反馈 1 tick 落稳
    assert out.state is DecisionState.CRUISE and out.reason == "abort:settled"


def test_change_fatal_goes_abort_to_conserve():
    e = _eng()
    e.update(_good_obs(fid=1), DT)                # CHANGE
    out = e.update(_obs(fid=2, fresh=False), DT)  # 帧过旧（1 tick 即致命）
    assert out.state is DecisionState.ABORT_CHANGE
    assert out.reason == "validate_fail:frame_stale"
    out = e.update(_obs(fid=3, presence=True), DT)
    assert out.state is DecisionState.CONSERVE    # 回稳落定后进保守


def test_change_retargets_only_beyond_margin():
    e = _eng()
    gA1, msA1 = _group(1, 1, n=3, x=0.5, cy=500, rel=5)
    e.update(_obs(fid=1, groups=(gA1,), targets=msA1), DT)   # CHANGE A score≈39.8
    # fid2：A 在场、B 更优过 margin（≈86.7 ≥ 39.8+15）→ 改判换目标
    gA2, msA2 = _group(1, 2, n=3, x=0.5, cy=500, rel=5)
    gB2, msB2 = _group(2, 2, n=6, x=0.3, cy=500, rel=5)
    out = e.update(_obs(fid=2, groups=(gA2, gB2), targets=msA2 + msB2), DT)
    assert out.reason == "switch:score_win" and out.target_id == 2
    # fid3：不足 margin 的组合（C≈56.4 < 86.7+15）与自身回落都不触发再改判
    gB3, msB3 = _group(2, 3, n=6, x=0.3, cy=500, rel=5)
    gC3, msC3 = _group(3, 3, n=4, x=0.4, cy=500, rel=5)
    out = e.update(_obs(fid=3, groups=(gB3, gC3), targets=msB3 + msC3), DT)
    assert "switch" not in out.reason and out.target_id == 2


def test_conserve_output_holds_and_forbids_moves():
    e = _eng()
    e._enter_conserve()                            # 直接进态（矩阵其余路径另有锁）
    out = e.update(_obs(fid=5, presence=True), DT)
    assert out.state is DecisionState.CONSERVE
    assert out.x_target is None and out.move_allowed is False   # 保持≠回中（§二）


def test_conserve_recovers_with_full_reset():
    e = _eng()
    e.update(_good_obs(fid=1), DT)                 # CHANGE
    e.update(_obs(fid=2, fresh=False), DT)         # abort
    e.update(_obs(fid=3, presence=True), DT)       # CONSERVE
    f = 4
    out = e.update(_obs(fid=f, presence=True, geometry=True), DT)
    while out.reason != "conserve_recovered" and f < 200:
        f += 1
        out = e.update(_obs(fid=f, presence=True, geometry=True), DT)
    assert out.state is DecisionState.CRUISE
    assert f - 3 >= int(CFG.validate.t_recover_s / DT)   # 不早于 T_recover（不许秒回）
    assert e._x_smooth == 0.0 and e._cool_t == 0.0 and e._target_gid is None  # 重置面


def test_conserve_escalates_to_fault_and_back():
    """升级要"降级但不致命"的观测（过旧+过渡=非致命但 degraded），否则满 T_recover
    就恢复、到不了 T_conserve_max 的 FAULT 升级；恢复再切回健康帧走双 T_recover。"""
    e = _eng()
    e._enter_conserve()
    f = 10
    out = e.update(_obs(fid=f, presence=True, fresh=False, transition=True), DT)
    while out.reason != "fault:conserve_max" and f < 400:
        f += 1
        out = e.update(_obs(fid=f, presence=True, fresh=False, transition=True), DT)
    assert out.state is DecisionState.FAULT
    while out.reason != "fault_recovered" and f < 600:
        f += 1
        out = e.update(_obs(fid=f, presence=True), DT)   # 健康帧：双 T_recover 降回
    assert out.state is DecisionState.CRUISE and e._x_smooth == 0.0


def test_v0_straight_baseline_gate():
    """allow_all_moves=false（V0 直行基线）：永不进 CHANGE，x 恒 0（§〇 验收 V0）。"""
    e = _eng(no_moves=True)
    for f in range(1, 10):
        out = e.update(_good_obs(fid=f), DT)
        assert out.state is DecisionState.CRUISE and out.move_allowed is False
    assert out.x_target == pytest.approx(0.0)


def test_output_schema_and_validity_window():
    e = _eng()
    out = e.update(_good_obs(), DT)
    assert out.schema_version == 2                 # v2：reanchor_lane 契约
    assert out.valid_until_fid > out.emitted_fid
    assert isinstance(out.reason, str) and out.reason
    assert out.reanchor_lane is None               # 非完成拍不携带


def test_signed_completion_rejects_reverse():
    """有符号完成判据（planner 设计稿 §二，维护者裁定 2026-09-22）：
    demand=+0.5 时反方向 executed=−0.45 绝对值够大也不得过收敛门。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT)                 # CHANGE demand=+0.5
    out = e.update(_good_obs(fid=2), DT, executed_lane=-0.45)
    assert "converged" not in out.reason
    assert out.state is DecisionState.CHANGE
    out = e.update(_good_obs(fid=3), DT, executed_lane=0.45)   # 同向才吃门
    assert "converged:feedback" in out.reason


def test_reanchor_lane_carried_once_signed():
    """完成拍携带有符号目标读数（非裸 bool）；下一拍即清（一次性）。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT)
    out = e.update(_good_obs(fid=2), DT, executed_lane=0.45)
    assert out.reanchor_lane == pytest.approx(0.5)  # g.x_center 有符号
    out = e.update(_obs(fid=3, presence=True), DT)
    assert out.reanchor_lane is None


def test_lowpass_is_time_based():
    """同目标不同 dt：大 dt 一步更靠近目标（α=1−exp(−dt/τ)，§五）。"""
    obs1 = _good_obs(fid=1)
    e_fast, e_slow = _eng(), _eng()
    e_fast.update(obs1, 0.2)                       # 快 tick 大 dt
    e_slow.update(obs1, 0.02)
    assert abs(e_fast._x_smooth) > abs(e_slow._x_smooth)


def test_engine_reset_clears_everything():
    e = _eng()
    e.update(_good_obs(), DT)
    e.reset()
    assert e._state is DecisionState.CRUISE and e._x_smooth == 0.0

def test_change_surviving_group_does_not_poison_x_target():
    """回放抓到的缺陷：CHANGE 中组转存续态（x_center=nan）曾把 x_smooth 污染成
    nan 直达输出——存续时保持最后 goal，不外插 nan。"""
    import math
    e = _eng()
    e.update(_good_obs(fid=1), DT)                 # → CHANGE，x_smooth 开始爬向 0.5
    assert not math.isnan(e.update(_good_obs(fid=2), DT).x_target)
    # 组进存续：成员不在 targets、conf_min=0、x_center=nan（同聚合器真实输出形态）
    held = CoinGroup(group_id=1, member_ids=(100, 101, 102), observed_count=3,
                     estimated_count=3, x_center=float("nan"), x_span=0.3,
                     cy_min=500, cy_max=506, conf_min=0.0, partial_observation=True,
                     first_seen_fid=1, last_seen_fid=2, validity_until_fid=10)
    out = e.update(_obs(fid=3, groups=(held,), presence=True), DT)
    assert not math.isnan(out.x_target)            # 核心断言：nan 不得穿透
    assert out.state is DecisionState.CHANGE       # 存续≠取消（宽限内继续计划）


# ---------- 阶段 C step 2：超车候选（allow_overtake=V2） ----------

def _cv(tid=5, x=1.1, cy=500, rel=10.0, x_memb=None, w_m=None, w_m_age=None):
    return CarView(id=tid, x_lane=x, cy=cy, rel_approach=rel,
                   d_min=abs(x), age_ticks=10, x_memb=x_memb,
                   w_m=w_m, w_m_age=w_m_age)


def _traffic(views=(), events=()):
    return (tuple(views), tuple(events))


def _pass_ev(tid=5, d=0.63, fid=3, outcome=OUTCOME_PASS):
    return PassEvent(track_id=tid, outcome=outcome, settled_fid=fid,
                     d_min=d, age_ticks=30)


def test_overtake_gate_closed_ignores_traffic():
    """关闸（默认）：喂车流观测也不成候选——V0/V1 兼容性红线的行为锁。"""
    e = _eng()   # allow_overtake=False
    out = e.update(_obs(presence=True), DT, traffic=_traffic([_cv()]))
    assert out.state is DecisionState.CRUISE and "no_candidate" in out.reason


def test_overtake_select_track_and_pass_done():
    e = _eng(overtake=True)
    out = e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv()]))
    assert out.state is DecisionState.CHANGE and out.reason == "select:score_win"
    assert out.target_id == 5 and out.x_target > 0        # 右车内贴=向右小幅
    # 在途：goal 跟车滚动（车横向读数变小，贴邻目标随之收缩）
    out = e.update(_obs(fid=2, presence=True), DT,
                   traffic=_traffic([_cv(x=0.8)]))
    assert out.state is DecisionState.CHANGE and "moving:overtake" in out.reason
    # pass 落定：done + 冷却；reanchor=None（超车完成拍无目标读数可锚，§六裁定）
    out = e.update(_obs(fid=3, presence=True), DT,
                   traffic=_traffic([], [_pass_ev()]))
    assert out.state is DecisionState.CRUISE and "done:overtake_pass" in out.reason
    assert out.reanchor_lane is None


def test_overtake_lost_or_ghost_aborts():
    for ev in (_pass_ev(fid=3).__class__(track_id=5, outcome=OUTCOME_LOST,
                                         settled_fid=3, d_min=None, age_ticks=30),
               _pass_ev(fid=3).__class__(track_id=5, outcome=OUTCOME_GHOST,
                                         settled_fid=3, d_min=None, age_ticks=99)):
        e = _eng(overtake=True)
        e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv()]))
        out = e.update(_obs(fid=3, presence=True), DT, traffic=_traffic([], [ev]))
        assert out.state is DecisionState.ABORT_CHANGE             and out.reason == "cancel:overtake_lost"


def test_overtake_presumed_pass_same_settlement_distinct_reason():
    """presumed_pass 与 pass 同路径结算（完成+短冷却），trace reason 分开记。"""
    e = _eng(overtake=True)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv()]))
    out = e.update(_obs(fid=3, presence=True), DT,
                   traffic=_traffic([], [_pass_ev(outcome=OUTCOME_PRESUMED_PASS)]))
    assert out.state is DecisionState.CRUISE \
        and "done:overtake_pass_presumed" in out.reason
    assert out.reanchor_lane is None


def test_overtake_vanish_without_event_aborts():
    """车从视图消失且无落定事件（traffic 断供/异常）：保守走 ABORT 不硬猜 pass。"""
    e = _eng(overtake=True)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv()]))
    out = e.update(_obs(fid=2, presence=True), DT, traffic=_traffic())
    assert out.state is DecisionState.ABORT_CHANGE


def test_passed_car_not_reselected_fresh_car_can():
    e = _eng(overtake=True)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv()]))
    e.update(_obs(fid=2, presence=True), DT, traffic=_traffic([], [_pass_ev(fid=2)]))
    for i in range(3, 30):                    # 过冷却窗（t_cool=1.0s=20 拍）
        e.update(_obs(fid=i, presence=True), DT, traffic=_traffic())
    out = e.update(_obs(fid=30, presence=True), DT, traffic=_traffic([_cv()]))
    assert "no_candidate" in out.reason       # 同 track id 已落定，不重选
    out = e.update(_obs(fid=31, presence=True),
                   DT, traffic=_traffic([_cv(tid=6)]))
    assert out.state is DecisionState.CHANGE  # 新车照常成候选


def test_overtake_timeout_forces_done():
    e = _eng(overtake=True, t_pass_max_s=0.2)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv()]))
    reasons = []
    for i in range(2, 8):
        out = e.update(_obs(fid=i, presence=True), DT,
                       traffic=_traffic([_cv(cy=650)]))
        reasons.append(out.reason)
    assert any("done:pass_timeout" in r for r in reasons)   # 兜底强制落定
    assert "moving:overtake" in reasons[0]                  # 落定前确在计划路上


def test_hug_goal_inner_side_both_signs():
    e = _eng(overtake=True)
    d = CFG.overtake.d_hold_lane
    assert e.scorer.hug_goal(1.1) == pytest.approx(1.1 - d)    # 右车从内侧贴
    assert e.scorer.hug_goal(-1.2) == pytest.approx(-1.2 + d)  # 左车镜像


def test_overtake_score_partial_order():
    s = _eng(overtake=True).scorer
    near = s.score_car(_cv(tid=1, cy=650))
    far = s.score_car(_cv(tid=2, cy=450))
    assert near.score > far.score                              # 同车越近越值
    assert s.score_car(_cv(x=0.3)) is None                     # 本车道车不作候选
    assert s.score_car(_cv(x=1.9)) is None                     # 带外杂框自卫
    assert s.score_car(_cv(tid=3, x=-1.1, rel=0.0)) is None    # 不接近（等速在前）
    left = s.score_car(_cv(tid=4, x=-1.1))
    right = s.score_car(_cv(tid=5, x=1.1))
    assert left.score == pytest.approx(right.score)            # 左右对称


# ---------- 对称性原则（维护者裁定 2026-09-22：决策默认左右对称，禁用方向需显式证据） ----------

def _corridor(left_m=None, right_m=None):
    """单带走廊（_side_has_space 消费面）：米制入参，obs.boundary 通道
    v4 恒 None 已退役（第 6 步侧别检查改吃走廊带）。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand, UNKNOWN)
    band = CorridorBand(
        z0=3.0, z1=6.0,
        left=BandBoundary(left_m, "measured") if left_m is not None
        else BandBoundary(None, UNKNOWN),
        right=BandBoundary(right_m, "measured") if right_m is not None
        else BandBoundary(None, UNKNOWN))
    return Corridor((band,), None, None, "pair", "ok")


def test_mirror_symmetry_engine_and_planner():
    """观测整体镜像喂入 → 决策输出与规划杆值必须严格镜像（对称性机检锁）。"""
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    e1, e2 = _eng(overtake=True), _eng(overtake=True)
    p1, p2 = LateralPlanner(CFG.planner), LateralPlanner(CFG.planner)
    for fid in range(1, 13):
        x = 1.1 + 0.03 * fid
        o1 = e1.update(_obs(fid=fid, presence=True), DT,
                       traffic=_traffic([_cv(tid=5, x=x)]))
        o2 = e2.update(_obs(fid=fid, presence=True), DT,
                       traffic=_traffic([_cv(tid=5, x=-x)]))
        assert o1.state is o2.state and o1.reason == o2.reason
        assert o1.x_target == pytest.approx(-(o2.x_target or 0.0), abs=1e-12)
        c1 = p1.update(o1, DT, fid)
        c2 = p2.update(o2, DT, fid)
        assert c1.steer_x == -c2.steer_x and c1.throttle == c2.throttle


def test_space_gate_disables_only_with_evidence():
    ov = CFG.overtake
    tight = ov.d_hold_lane + ov.space_margin_lane - 0.05
    e = _eng(overtake=True)
    lw = e.cal.lane_w_m
    # 左缘只剩 tight 的空间 → 左候选被禁、右候选照常（走廊带来源规则）
    c = _corridor(left_m=-tight * lw, right_m=2.0 * lw)
    out = e.update(_obs(fid=1, presence=True), DT,
                   traffic=_traffic([_cv(tid=9, x=-1.1)]), corridor=c)
    assert "no_candidate" in out.reason
    out = e.update(_obs(fid=2, presence=True), DT,
                   traffic=_traffic([_cv(tid=9, x=1.1)]), corridor=c)
    assert out.state is DecisionState.CHANGE
    # 证据不足（unknown 带）→ 两侧都放行（对称默认，不得无证据禁方向）
    e2 = _eng(overtake=True)
    out = e2.update(_obs(fid=1, presence=True), DT,
                    traffic=_traffic([_cv(tid=9, x=-1.1)]),
                    corridor=_corridor(None, None))
    assert out.state is DecisionState.CHANGE
    e3 = _eng(overtake=True)
    out = e3.update(_obs(fid=1, presence=True), DT,
                    traffic=_traffic([_cv(tid=9, x=-1.1)]))
    assert out.state is DecisionState.CHANGE


# ---------- 19:44 复盘三刀：可行性门 / pass 短冷却 / ABORT 速率落稳 ----------

def test_overtake_feasibility_gate():
    """来不及贴的车不追：t_meet < 贴邻耗时+响应+余量 → 不成候选。"""
    e = _eng(overtake=True)
    near = _cv(tid=1, cy=650)          # t_meet=(716−650)/200=0.33s < ~0.77s
    out = e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([near]))
    assert "no_candidate" in out.reason
    far = _cv(tid=2, cy=400)           # t_meet=1.58s > 0.77s
    out = e.update(_obs(fid=2, presence=True), DT, traffic=_traffic([far]))
    assert out.state is DecisionState.CHANGE


def test_overtake_pass_short_cooling():
    """pass 落定后短冷却（0.2s）：第 6 拍（0.3s）新车已可再选；旧 1s 口径下还在 cooling。"""
    e = _eng(overtake=True)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv(tid=5, cy=400)]))
    out = e.update(_obs(fid=2, presence=True), DT,
                   traffic=_traffic([], [_pass_ev(tid=5, fid=2)]))
    assert "done:overtake_pass" in out.reason
    out = e.update(_obs(fid=3, presence=True), DT,
                   traffic=_traffic([_cv(tid=6, x=1.2, cy=400)]))
    assert "cooling" in out.reason                # 0.05s：短冷却内，不误开
    for i in range(4, 8):                          # 递减过 0.2s 窗尾（浮点尾差需多一拍）
        out = e.update(_obs(fid=i, presence=True), DT,
                       traffic=_traffic([_cv(tid=6, x=1.2, cy=400)]))
    assert out.state is DecisionState.CHANGE       # 0.2s 窗过即重开（旧 1s 口径仍在 cooling）


def test_abort_settles_on_velocity_zero():
    """有反馈 ABORT：executed 停住（速率≤界）即落稳，不再一律冻满 2s。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT)                     # → CHANGE
    e.update(_obs(fid=10, presence=True), DT)          # 组消失 → ABORT（executed None？）
    # 上一拍 executed=None 直接落稳（v1 路）——本锁走有反馈路：
    e2 = _eng()
    e2.update(_good_obs(fid=1), DT, executed_lane=0.1)
    out = e2.update(_obs(fid=10, presence=True), DT, executed_lane=0.2)  # 进 ABORT
    assert out.state is DecisionState.ABORT_CHANGE
    out = e2.update(_obs(fid=11, presence=True), DT, executed_lane=0.2)  # 首拍立参考
    assert out.state is DecisionState.ABORT_CHANGE
    out = e2.update(_obs(fid=12, presence=True), DT, executed_lane=0.2)  # v=0 → 落稳
    assert out.state is DecisionState.CRUISE and "abort:settled" in out.reason
    # 仍在横移：不落稳
    e3 = _eng()
    e3.update(_good_obs(fid=1), DT, executed_lane=0.1)
    e3.update(_obs(fid=10, presence=True), DT, executed_lane=0.2)
    e3.update(_obs(fid=11, presence=True), DT, executed_lane=0.2)          # 立参考
    out = e3.update(_obs(fid=12, presence=True), DT, executed_lane=0.35)   # v=3>0.3
    assert out.state is DecisionState.ABORT_CHANGE


# ---------- 阶段一（2026-10-02）：门吃执行回执 + 逐拍重评 + 横向安全 veto ----------
# 病灶锚（真机第四轮 trace 1836 两局）：可行性门假设自车瞬移（滞后中位 0.45/纸 p90 0.9
# 车道）、目标掠过窗漂移中位 0.3 道追不上、变道中不重评——三刀各一把锁。

def test_gate_priced_from_execution_receipt():
    """可行性门从执行回执起算（need=|目标−executed|），不再假设自车在 0：
    同一候选（x=2.0，t_miss≈0.99s），无回执按旧口径 need(2.0)=1.1→1.38≥0.99 落选；
    回执 executed=1.5 时 need(0.5)=0.5→0.78<0.99 入选。"""
    e = _eng()
    out = e.update(_good_obs(x=2.0, fid=1), DT)
    assert out.state is DecisionState.CRUISE and "no_candidate" in out.reason
    e2 = _eng()
    out = e2.update(_good_obs(x=2.0, fid=1), DT, executed_lane=1.5)
    assert out.state is DecisionState.CHANGE


def test_overtake_gate_priced_from_receipt():
    """超车门同刀：car x=1.1 cy=570（t_meet=0.73s）——从 0 起算
    need(0.475)+0.28=0.77≥0.73 落选；从回执 0.35 起算 need(0.125)+0.28=0.63<0.73 入选。"""
    e = _eng(overtake=True)
    out = e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([_cv(cy=570)]))
    assert "no_candidate" in out.reason
    e2 = _eng(overtake=True)
    out = e2.update(_obs(fid=1, presence=True), DT, executed_lane=0.35,
                    traffic=_traffic([_cv(cy=570)]))
    assert out.state is DecisionState.CHANGE


def test_change_aborts_when_window_missed():
    """逐拍重评主锁（金币=及格制，到不了=零分）：执行停滞 + 目标迫近
    （t_miss 缩到 need 以下）连拍破判 → ABORT，不追注定错过的变道。
    破判轨迹：fid2/fid3 组 cy=585、rel=11（流场自洽，k 与 fid1 同为 100/178²）
    → t_miss≈0.38 < need(0.5)+0.28=0.78。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT, executed_lane=0.0)
    near1, ms1 = _group(1, 2, 3, x=0.5, cy=585, rel=11.0)
    out = e.update(_obs(fid=2, groups=(near1,), targets=ms1), DT, executed_lane=0.0)
    assert out.state is DecisionState.CHANGE               # 单拍破判不动（防抖）
    near2, ms2 = _group(1, 3, 3, x=0.5, cy=585, rel=11.0)
    out = e.update(_obs(fid=3, groups=(near2,), targets=ms2), DT, executed_lane=0.0)
    assert out.state is DecisionState.ABORT_CHANGE and out.reason == "cancel:infeasible"


def test_change_single_breach_does_not_abort():
    """重评防抖锁：单拍破判（读数噪声量级）只记 streak，回落后清零——
    两拍连破才 ABORT，噪声不得撕掉进行中的计划。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT, executed_lane=0.0)
    near, ms = _group(1, 2, 3, x=0.5, cy=585, rel=11.0)
    out = e.update(_obs(fid=2, groups=(near,), targets=ms), DT, executed_lane=0.0)
    assert out.state is DecisionState.CHANGE
    back, msb = _group(1, 3, 3, x=0.5, cy=500, rel=5)
    out = e.update(_obs(fid=3, groups=(back,), targets=msb), DT, executed_lane=0.0)
    assert out.state is DecisionState.CHANGE and "moving" in out.reason


def test_overtake_aborts_when_target_escapes():
    """超车逐拍重评（逃逸判据，第五轮裁定）：散布 > 贴窗距离且不收缩 → 连拍
    弃追（白超 1.15~1.29 道病灶的决策侧刀）。目标横漂远去（x 1.1→1.55），
    执行位停在 0，散布 0.475→0.675→0.925 后停住——贴窗外原地踏步同判。"""
    e = _eng(overtake=True)
    e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
             traffic=_traffic([_cv(cy=400)]))                   # t_meet=1.58，hug=0.475
    out = e.update(_obs(fid=2, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(x=1.3, cy=480)]))      # disp=0.675：首拍立基线不判
    assert out.state is DecisionState.CHANGE
    out = e.update(_obs(fid=3, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(x=1.55, cy=560)]))     # disp=0.925，涨 → 破判1
    assert out.state is DecisionState.CHANGE
    out = e.update(_obs(fid=4, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(x=1.55, cy=640)]))     # disp 停在 0.925，不收缩 → 破判2
    assert out.state is DecisionState.ABORT_CHANGE and out.reason == "cancel:infeasible"


def test_overtake_survives_drift_and_completes_pass():
    """第五轮 1742 案回归锁：已贴近（散布 ≤ 贴窗距离）时目标横漂/迫近不得弃追
    ——超车是 pass 制不是到点制，车反正要过，弃追向中回拉只会贴得更远。
    执行收敛、目标小幅横漂、cy 步进 80（不触 cy_jump）→ 计划存活 → pass 落定。"""
    e = _eng(overtake=True)
    out = e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(cy=400)]))             # 选中，hug=0.475
    assert out.state is DecisionState.CHANGE
    for i, (x, cy, el) in enumerate(((1.15, 480, 0.10), (1.20, 560, 0.20),
                                     (1.25, 640, 0.28)), start=2):
        out = e.update(_obs(fid=i, presence=True), DT, executed_lane=el,
                       traffic=_traffic([_cv(x=x, cy=cy)]))
        assert out.state is DecisionState.CHANGE, out.reason
    out = e.update(_obs(fid=5, presence=True), DT, executed_lane=0.30,
                   traffic=_traffic([], [_pass_ev(tid=5, fid=5)]))
    assert out.state is DecisionState.CRUISE and "done:overtake_pass" in out.reason


def test_veto_horizon_clamped_by_pass_time():
    """危险窗按到站时间截断：车马上过我们车头（t_pass=0.1s）时，包络还没扫到
    它的地盘 → 放行；若无截断，包络驻停位与它的静态位置只差 0.25 → 误 veto。"""
    e = _eng()
    car = CarView(id=9, x_lane=0.75, cy=int(load_calib().v_ego) - 20, rel_approach=10.0,
                  d_min=0.75, age_ticks=5, v_lat=0.0)   # t_pass=20px/200px/s=0.1s
    out = e.update(_good_obs(fid=1), DT, executed_lane=0.0, traffic=_traffic([car]))
    assert out.state is DecisionState.CHANGE


def test_veto_treats_cars_static_not_phantom_drift():
    """第六轮正中靶心案回归锁：x_lane 读数噪声（EMA 后 ±1 道/s 级）不得被当成
    真实运动外推——噪声方向「飘走」曾把实际停在走廊里的车外推出走廊 → 全速
    扫上去正中靶心。静态判据下它留在原地 → veto 拦下。"""
    e = _eng()
    car = CarView(id=9, x_lane=0.75, cy=500, rel_approach=10.0, d_min=0.75,
                  age_ticks=5, v_lat=0.05)     # 噪声读数：+1 道/s「向右飘走」
    out = e.update(_good_obs(fid=1), DT, executed_lane=0.0, traffic=_traffic([car]))
    assert "no_candidate" in out.reason         # 静态 0.75 距贴位 0.25 < 0.45 → 拦


# ---------- 第七轮：本车道前车紧急回避 + σ 选中拍锁定 ----------

def test_dodge_triggers_on_inlane_approaching_car():
    """同车道逼近的车（ttc<0.5s、横距<接触界）绕过冷却与评分强制变道，
    目标=远离车体一侧的贴邻位（自车 0.0 在车的左侧 → 向左避，不横穿）。"""
    e = _eng(overtake=True)
    out = e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=620)]))   # ttc=0.48s
    assert out.state is DecisionState.CHANGE and out.reason == "select:dodge"
    assert out.target_id == 9 and out.x_target < 0


def test_dodge_bypasses_cooling():
    """冷却中的盲持是追尾窗口（2029 局 near_dx 0.00 全在 hold:cooling）——
    威胁在场时冷却不作数。"""
    e = _eng(overtake=True)
    e._cool_t = 5.0
    out = e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=620)]))
    assert out.state is DecisionState.CHANGE and out.reason == "select:dodge"


def test_dodge_not_triggered_without_threat():
    """车还在远处（ttc≥0.5s）或在邻道（横距≥接触界）：不回避，正常机制管。"""
    e = _eng(overtake=True)
    e._cool_t = 5.0
    out = e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=600)]))   # ttc=0.58s≥0.5
    assert out.state is DecisionState.CRUISE and "cooling" in out.reason
    out = e.update(_obs(fid=2, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.8, cy=600)]))    # 横距 0.8
    assert out.state is DecisionState.CRUISE


def test_dodge_sigma_locked_goal_stays_away_side():
    """σ 选中拍锁定（第七轮）：回避中车体横漂穿过中线，跟踪目标不得翻边横穿
    车体（旧 hug_goal 按 sign(x) 翻侧，会把自车指挥到车的另一侧=对穿）。"""
    e = _eng(overtake=True)
    out = e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=620)]))
    assert out.reason == "select:dodge" and e._demand_lane < 0     # 左避
    obs2 = _obs(fid=2, presence=True)
    out = e.update(obs2, DT, executed_lane=-0.1,
                   traffic=_traffic([_cv(tid=9, x=-0.10, cy=640)]))
    assert out.state is DecisionState.CHANGE
    # 跟踪目标（_current_goal）锁左：−0.10 + (−1)·0.625 = −0.725；旧语义翻到 +0.525
    assert e._current_goal(obs2) == pytest.approx(
        -0.10 - CFG.overtake.d_hold_lane)


def test_safety_veto_blocks_candidate():
    """横向安全 veto（RSS 形状外挂判据，不进评分）：机动走廊 [0→0.5] 终点
    与走廊内街车的预测最小距 0.3 < 接触界 0.45 → 候选拦下；车在走廊外
    （0.625≥0.45）同一候选照常入选。veto 只在有执行回执时判（假起点毒化几何）。"""
    e = _eng()
    out = e.update(_good_obs(fid=1), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.8, cy=650)]))
    assert "no_candidate" in out.reason
    e2 = _eng()
    out = e2.update(_good_obs(fid=1), DT, executed_lane=0.0,
                    traffic=_traffic([_cv(tid=9, x=1.1, cy=650)]))
    assert out.state is DecisionState.CHANGE


def test_safety_veto_aborts_mid_change():
    """变道中走廊被占 → 立即 ABORT 有界回稳（安全 veto 不吃防抖连拍：
    误弃一个候选的代价 ≪ 预测接触还继续扫的代价）。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT, executed_lane=0.0)
    out = e.update(_good_obs(fid=2), DT, executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.75, cy=500)]))
    assert out.state is DecisionState.ABORT_CHANGE and out.reason == "cancel:lat_veto"


# ---------- V0 门账本（V0 决策门账本 5071c02；只记账不改判） ----------

def _car_tgt(tid, x, cy, rel, w=40, conf=0.9, fid=1):
    return TrackedTarget(id=tid, kind="car", x_lane=x, x_sigma=0.1, cy=cy,
                         w=w, h=30, conf=conf, rel_approach=rel,
                         first_seen_fid=fid, last_seen_fid=fid,
                         validity_until_fid=fid + 8)


def test_gate_ledger_rows_and_reasons():
    """账本逐车落行、拒绝原因来自唯一判定处（_score_car_reason）；宽度列走
    CarView 有效性面（TrafficObserver 年龄/置信门+TTL 产物，三件套③——
    decision 只透传不换算；w_m 缺席=无效/过期显式 unknown 不冒充）。"""
    e = _eng(overtake=True)
    views = [_cv(tid=9, x=1.1, cy=500, rel=10.0, w_m=1.72, w_m_age=0),
             _cv(tid=10, x=0.2, cy=500, rel=10.0, w_m=1.8, w_m_age=2),
             _cv(tid=11, x=1.1, cy=500, rel=-2.0)]   # 不接近：approach 拒
    obs = _obs(fid=1, presence=True,
               targets=(_car_tgt(9, 1.1, 500, 10.0),
                        _car_tgt(10, 0.2, 500, 10.0),
                        _car_tgt(11, 1.1, 500, -2.0)))
    out = e.update(obs, DT, traffic=_traffic(views))
    led = e.gate_ledger
    assert led is not None and led["v"] == 2 and led["fid_hz"] is None
    rows = {r["id"]: r for r in led["cars"]}
    assert rows[9]["why"] is None and rows[9]["score"] >= 20.0
    assert rows[9]["w_m"] == 1.72 and rows[9]["w_m_age"] == 0
    assert rows[9]["w_px"] == 40                      # 原料列照落（对账用）
    assert rows[10]["why"] == "band" and rows[10]["w_m"] == 1.8
    assert rows[11]["why"] == "approach" and "w_m" not in rows[11]
    assert out.target_id == 9                        # 账本没改判：选择照旧


def test_gate_ledger_done_pass_and_low_score():
    """done_pass 车标记独立于评分；低分车 why=score（分数门读数面）。
    注意分数门量级：E≈132（勘误后），t_meet=1.9s 的远车分数 ~80 照样过——
    造"分数不过"必须 t_meet≈9.5s（rel=0.4），顺带就是勘误的行为锁。"""
    e = _eng(overtake=True)
    out = e.update(_obs(fid=1, presence=True), DT,
                   traffic=_traffic([_cv(tid=9, x=1.1, cy=500, rel=10.0)]))
    assert out.state is DecisionState.CHANGE           # 锁定 9 号
    out = e.update(_obs(fid=3, presence=True), DT,
                   traffic=_traffic([], (_pass_ev(tid=9, fid=3),)))
    assert "done:overtake_pass" in out.reason          # 正常结算路径入 done_pass
    # pass 冷却（t_cool_pass_s=0.2）走完再给车流：冷却拍 _select 不跑
    e.update(_obs(fid=4, presence=True), 0.3)
    e.update(_obs(fid=5, presence=True), DT,
             traffic=_traffic([_cv(tid=9, x=1.1, cy=500, rel=10.0),
                               _cv(tid=12, x=1.3, cy=640, rel=0.4)]))
    rows = {r["id"]: r for r in e.gate_ledger["cars"]}
    assert rows[9]["why"] == "done_pass" and "score" not in rows[9]
    assert rows[12]["why"] == "score" and rows[12]["t_meet"] > 8.0


def test_gate_ledger_none_without_select():
    """未跑选择的拍（致命→CONSERVE）不留旧账：gate_ledger=None。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT)
    e.update(_obs(fid=2, alive=False), DT)
    assert e.gate_ledger is None


def test_gate_ledger_coin_rows():
    """金币行：好组 why=None；低信组读数面落在 conf/score（不细分，账面如实）。"""
    e = _eng()
    e.update(_good_obs(fid=1), DT)
    assert e.gate_ledger["coins"][0]["why"] is None
    e2 = _eng()
    e2.update(_good_obs(fid=1, conf=0.34), DT)    # 地板（检测门槛）下 → why="conf"
    assert e2.gate_ledger["coins"][0]["why"] in ("conf", "score")


# ---------- 前置三件套①：车流 t_meet 地面流化（2026-10-11 V0 归因） ----------

def test_car_t_meet_ground_flow_and_fallback():
    """fid 率有供=地面流积分式（对 1/r 沿行积分的闭式对账）；缺供=退回旧
    行率式（已知 TTC 高估方向的旧消费口径，行为不静默换式）。"""
    cal = load_calib()
    d, D, rel, f = 200.0, cal.v_ego - cal.y_h, 4.0, 30.0
    assert t_meet_car_s(rel, cal.y_h + d, cal, f) == \
        pytest.approx(d * (D - d) / (D * rel) / f)
    assert math.isinf(t_meet_car_s(-1.0, 500.0, cal, f))   # 不接近→永不到站
    assert t_meet_car_s(4.0, cal.v_ego + 5, cal, f) == 0.0  # 已在行上
    with pytest.raises(ValueError):
        t_meet_car_s(4.0, 500.0, cal, 0.0)
    e = _eng(overtake=True)
    v = _cv(tid=1, cy=cal.y_h + d, rel=rel)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([v]))
    # 缺供退路=旧线性式（rel 按 px/tick 消费），与三件套①前的行为逐位同
    assert e.scorer.car_t_meet_s(rel, cal.y_h + d) == \
        pytest.approx((cal.v_ego - cal.y_h - d) / (rel * 20.0))
    # 引擎逐拍喂 fid 率后换积分式
    e.update(_obs(fid=2, presence=True, fid_hz=f), DT, traffic=_traffic([v]))
    assert e.scorer.fid_hz == f
    assert e.scorer.car_t_meet_s(rel, cal.y_h + d) == \
        pytest.approx(d * (D - d) / (D * rel) / f)


def test_far_car_passes_score_gate_with_ground_flow():
    """V0 病灶②行为锁：同一辆远车（rel 按 px/fid 如实透传），旧口径
    t_meet≈9.5s 被分数门拒；换地面流+fid 时基后到站秒级——分数门放行。
    这就是「开得不积极」的输入侧修正，行为可从账本 why 翻转直接读。"""
    cal = load_calib()
    cy, rel = int(cal.y_h) + 316, 0.4        # 旧口径 t=(D−316)/(0.4·20)≈9.5s
    e_old = _eng(overtake=True)
    e_old.update(_obs(fid=1, presence=True), DT,
                 traffic=_traffic([_cv(tid=12, cy=cy, rel=rel)]))
    rows = {r["id"]: r for r in e_old.gate_ledger["cars"]}
    assert rows[12]["why"] == "score"          # 旧口径：分数门拒（原锁保留）
    e_new = _eng(overtake=True)
    e_new.update(_obs(fid=1, presence=True, fid_hz=30.0), DT,
                 traffic=_traffic([_cv(tid=12, cy=cy, rel=rel)]))
    rows = {r["id"]: r for r in e_new.gate_ledger["cars"]}
    assert rows[12]["why"] is None             # 新口径：t≈5.1s，分数门放行


def test_membership_picked_for_band_and_goal():
    """三件套②决策面：成员位成立时带门/贴位目标吃成员位（整道中心），
    单拍读数出带（V0 病灶①的 1.34→2.38→1.44 形态）不再误拒。"""
    e = _eng(overtake=True)
    # 单拍 x=1.44 落带内，但历史读数确证它在 2 道：成员位 2.0 → band 拒
    v_far = _cv(tid=21, x=1.44, cy=400, rel=8.0, x_memb=2.0)
    # 单拍 x=2.38 出带，但成员位确证 1 道：带门放行
    v_in = _cv(tid=22, x=2.38, cy=500, rel=10.0, x_memb=1.0)
    e.update(_obs(fid=1, presence=True), DT,
             traffic=_traffic([v_far, v_in]))
    rows = {r["id"]: r for r in e.gate_ledger["cars"]}
    assert rows[21]["why"] == "band"
    assert rows[22]["why"] is None
    # 成员位未成立（None）退回单拍读数：行为与三件套②前逐位同
    e2 = _eng(overtake=True)
    e2.update(_obs(fid=1, presence=True), DT,
              traffic=_traffic([_cv(tid=23, x=2.38, cy=500, rel=10.0)]))
    rows = {r["id"]: r for r in e2.gate_ledger["cars"]}
    assert rows[23]["why"] == "band"           # 单拍 2.38 出带照拒


# ---------- V2 行为闸：dodge ttc / veto 危险窗米制融合（metric_cars.fusion） ----------

def _mc(tid, *, z=4.0, v=10.0, x_m=0.05):
    """米制车：v>0 → t=z/v；v≤0 → t=None（pacing 占用）；v=None=估计窗未开。"""
    return MetricCarState(target_id=tid, source=DistanceSource.ANCHOR,
                          z_m=z, x_m=x_m, v_close_mps=v,
                          t_meet_s=z / v if v is not None and v > 0 else None,
                          anchor_age_ms=200.0)


def _fus_eng():
    """fusion 闸开 + 超车开（与 _eng 同基座纪律：部署开关显式翻转）。"""
    return DecisionEngine(replace(
        CFG, mode=replace(CFG.mode, allow_all_moves=True, allow_overtake=True),
        metric_cars=replace(CFG.metric_cars, fusion=True)))


def test_dodge_fusion_metric_sees_pixel_blind():
    """像素 rel≤0（退化带盲区）但米制报逼近 t=0.4s<0.55 → dodge 照触发
    ——逼近门放宽为任一仪器报逼近，恰是地平线带的结构性治愈。"""
    e = _fus_eng()
    mc = _mc(9)                                # t=0.4
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=620, rel=0.0)]))
    assert out.state is DecisionState.CHANGE and out.reason == "select:dodge"
    assert out.target_id == 9 and out.x_target < 0


def test_dodge_fusion_off_keeps_pixel_gate():
    """回滚红线：fusion 关时同一像素盲区场景不触发（rel≤0 门原样）。"""
    e = _eng(overtake=True)
    mc = _mc(9)
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=620, rel=0.0)]))
    assert out.state is DecisionState.CRUISE and "dodge" not in out.reason


def test_dodge_fusion_metric_advances_trigger():
    """像素读远处（t=1.08s≥0.55 不触发）、米制读近处（t=0.4）→ 取较小者
    提前触发——先到者先防。"""
    e = _fus_eng()
    mc = _mc(9)                                # t=0.4 < 像素 (716−500)/200≈1.08
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=500, rel=10.0)]))
    assert out.reason == "select:dodge"


def test_dodge_metric_pacing_does_not_suppress_pixel():
    """米制 pacing（v_close≤0 → t=None）不造到站也不压像素：像素报逼近
    （t=0.48）照旧触发——pacing 占用语义归道评分，不进 dodge。"""
    e = _fus_eng()
    mc = _mc(9, v=-2.0)
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0,
                   traffic=_traffic([_cv(tid=9, x=0.05, cy=620, rel=10.0)]))
    assert out.reason == "select:dodge"


def test_dodge_metric_only_sees_pixel_dead_zone():
    """米制-only 车扩展（闸 metric_cars.dodge）：像素车轨为空（检测死区/
    雨天，trace 151206_p2 实测 45% 拍无车轨）但米制链有近车（x_m=0.05m、
    t=0.4s<0.55）→ 开闸触发回避，目标=远离侧贴邻位。"""
    e = DecisionEngine(replace(
        CFG, mode=replace(CFG.mode, allow_all_moves=True, allow_overtake=True),
        metric_cars=replace(CFG.metric_cars, fusion=True, dodge=True)))
    mc = _mc(9, x_m=0.5)                       # x_m=0.5m≈0.15 道（偏右），t=0.4s
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0, traffic=_traffic([]))
    assert out.state is DecisionState.CHANGE and out.reason == "select:dodge"
    assert out.target_id == 9 and out.x_target < 0     # 目标=远离车体的左贴邻位


def test_dodge_metric_only_off_shadow_only():
    """关闸=行为逐拍不变（同一场景不触发）+ 影子账有记录（对照列数据源）。"""
    e = DecisionEngine(replace(
        CFG, mode=replace(CFG.mode, allow_all_moves=True, allow_overtake=True),
        metric_cars=replace(CFG.metric_cars, fusion=True, dodge=False)))
    mc = _mc(9)
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0, traffic=_traffic([]))
    assert out.state is DecisionState.CRUISE and "dodge" not in out.reason
    assert len(e.metric_dodge_shadow) == 1            # 影子账：米制-only 候选在账


def test_dodge_metric_only_x_gate():
    """米制-only 车在邻道（横位距自车 ≥ 接触界）→ 不作回避对象，影子账空。"""
    e = DecisionEngine(replace(
        CFG, mode=replace(CFG.mode, allow_all_moves=True, allow_overtake=True),
        metric_cars=replace(CFG.metric_cars, fusion=True, dodge=True)))
    mc = _mc(9, x_m=2.0)                       # 2.0m≈0.59 道 ≥ 接触界 0.45
    out = e.update(replace(_obs(fid=1, presence=True), metric_cars=(mc,)), DT,
                   executed_lane=0.0, traffic=_traffic([]))
    assert out.state is DecisionState.CRUISE
    assert e.metric_dodge_shadow == ()


def test_veto_window_fusion_truncates_only_later():
    """LateralSafety 危险窗截断融合（V2）：两仪器都有正到站取较晚（截断只许
    更晚不许更早）；pacing=永不到站=全窗；估计窗未开/闸关=纯像素。"""
    lat = LateralSafety(CFG, load_calib(), t_meet_fn=lambda rel, cy: 2.0)
    car = _cv(tid=9, x=0.8, cy=500, rel=10.0)
    assert lat.metric_map is None
    assert lat._t_pass_s(car) == pytest.approx(2.0)
    lat.metric_map = {9: _mc(9, z=10.0, v=2.0)}          # t=5.0 > 像素 2.0
    assert lat._t_pass_s(car) == pytest.approx(5.0)
    lat.metric_map = {9: _mc(9, v=-1.0)}                 # pacing：全窗
    assert lat._t_pass_s(car) == math.inf
    lat.metric_map = {9: _mc(9, v=None)}                 # 窗未开：像素
    assert lat._t_pass_s(car) == pytest.approx(2.0)


def test_hold_returns_to_grid_green_center():
    """hold 回中锚（2026-10-15 贴墙诊断）：无候选时 x_target 朝栅格绿区路心
    回中，不再守走廊中心——pair_slot 车锚走廊「骑黄线报正中」（域内 gate
    note 实录），守中心=守原地，贴墙不回。绿区路心在左 → x_target 负；
    栅格弃权（state 全 0）→ 回退 0（旧行为逐位）。"""
    import numpy as np
    from maaracing_master.plugins.speedrush.corridor import Corridor
    from maaracing_master.plugins.speedrush.depth_geo import (
        GRID_DRIVABLE, GRID_NX, GRID_NZ, GRID_UNKNOWN, DrivableGrid)
    # 绿区路心在左 ~−3.25m（列 41~42 的格界 [−3.5,−3.0]m，中点 −3.25）
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    st[:, 41:43] = GRID_DRIVABLE
    g = DrivableGrid(state=st, coef=(0.0, 0.0, 2.0),
                     counts=np.full(st.shape, 9, np.int32),
                     dig_cells=0, latency_ms=1.0)
    c = Corridor((), 0.0, None, "pair_slot", "ok")   # 车锚走廊：报居中
    e = DecisionEngine(CFG)
    out = e.update(_obs(fid=1, presence=True), DT, corridor=c, grid=g)
    assert out.state is DecisionState.CRUISE
    assert out.reason == "hold:no_candidate"
    assert out.x_target is not None and out.x_target < 0   # 朝绿区路心回中
    # 栅格弃权（state 全 0=拟合失败）→ 回退 0
    g0 = DrivableGrid(state=np.zeros((GRID_NZ, GRID_NX), np.int8),
                      coef=None, counts=np.zeros(st.shape, np.int32),
                      dig_cells=0, latency_ms=1.0)
    e0 = DecisionEngine(CFG)
    out0 = e0.update(_obs(fid=1, presence=True), DT, corridor=c, grid=g0)
    assert out0.x_target == pytest.approx(0.0, abs=1e-12)
