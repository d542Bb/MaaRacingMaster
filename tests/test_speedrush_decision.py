# -*- coding: utf-8 -*-
"""step 4 决策层的回归锁：配置 fail-loud、校验合取、评分偏序、FSM 全转移矩阵。

矩阵按设计稿 v2 §二逐格上锁（含取消/超时/改判/恢复/升级五类收尾路径）；
评分只锁**偏序**不锁绝对值（验收 3）；数值场景以 decision.json 起值反推
（helper 里注释了各场景过/不过哪道门的算式），改参数会红——这是刻意的：
参数与测试场景是一对契约面。
"""

from __future__ import annotations

import json

import pytest

from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import (
    DecisionEngine, Scorer, ValidationWatch)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import _read_gate0, load_calib
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH, GATE0_FILE as GATE0_PATH

DT = 0.05          # 20Hz 名义 tick
CFG = _read_decision(
    __import__("maaracing_master.plugins.speedrush", fromlist=["DECISION_FILE"])
    .DECISION_FILE)


# ---------- 观测构造 helper（场景算式见各测试注释） ----------

def _obs(*, fid=1, groups=(), targets=(), presence=None, alive=True, fresh=True,
         geometry=True, transition=False):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(alive, fresh, geometry, presence, transition),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups)


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


# 场景 A（默认"良候选"）：n=3 conf=0.85 x=0.5 cy=500 rel=10
#   value=30·3·0.6=54；t_miss=(716−502)/200=1.07s；life=e^−1.07/5=0.81 → 43.7
#   −shift 5 = 38.7 ≥ min 20 ✓；need=0.3+0.4·0.5+0.13+0.15=0.78 < 1.07 ✓可行
GOOD = dict(gid=1, n=3, x=0.5, cy=500, rel=10)


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


def test_score_monotone_in_distance():
    assert _score(cy=620).score > _score(cy=500).score > _score(cy=420).score


def test_score_penalizes_lateral_shift():
    assert _score(x=0.3).score > _score(x=1.5).score


def test_score_conf_ramp_and_floor():
    assert _score(conf=0.99).score > _score(conf=0.7).score
    assert _score(conf=0.4) is None          # 地板下不低信决策
    held = CoinGroup(group_id=9, member_ids=(1,), observed_count=1, estimated_count=1,
                     x_center=0.5, x_span=0, cy_min=500, cy_max=500, conf_min=0.0,
                     partial_observation=True, first_seen_fid=0, last_seen_fid=0,
                     validity_until_fid=9)
    assert Scorer(CFG, load_calib()).score(held, _obs(groups=(held,))) is None


def test_score_rejects_receding_target():
    """rel≤0（远离）：机会视为无限远 → 分数被折扣压到不可选（−cost）。"""
    s = _score(rel=-5)
    assert s is None or s.score < CFG.hysteresis.min_score


# ---------- FSM 全转移矩阵（§二优先级逐格） ----------

def _eng(**over):
    import dataclasses
    # allow_all_moves 是**部署开关**（V0 直行 / V1 横向），不是测试夹具：
    # 矩阵测试需要能进 CHANGE，故基座强制 V1，与 decision.json 的当前态解耦；
    # no_moves 显式转 V0（§〇 验收 V0 那条锁的就是它）。
    cfg = dataclasses.replace(CFG, mode=dataclasses.replace(
        CFG.mode, allow_all_moves=True))
    if over.get("no_moves"):
        cfg = dataclasses.replace(cfg, mode=dataclasses.replace(
            cfg.mode, allow_all_moves=False))
    return DecisionEngine(cfg)


def test_cruise_selects_feasible_candidate():
    e = _eng()
    out = e.update(_good_obs(), DT)
    assert out.state is DecisionState.CHANGE and out.reason == "select:score_win"
    assert out.move_allowed is True and out.target_id == 1


def test_cruise_low_score_holds():
    e = _eng()
    out = e.update(_good_obs(conf=0.52), DT)      # 折扣后 < min_score
    assert out.state is DecisionState.CRUISE and "no_candidate" in out.reason


def test_cruise_infeasible_timing_holds():
    e = _eng()
    # 近处大横移：t_miss=(716−656)/(50×20)=0.06s ≪ need=0.3+0.88+0.28=1.46s
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
    gA1, msA1 = _group(1, 1, n=3, x=0.5, cy=500, rel=10)
    e.update(_obs(fid=1, groups=(gA1,), targets=msA1), DT)   # CHANGE A score≈38.7
    # fid2：A 在场、B 更优过 margin（67 ≥ 38.7+15）→ 改判换目标
    gA2, msA2 = _group(1, 2, n=3, x=0.5, cy=500, rel=10)
    gB2, msB2 = _group(2, 2, n=6, x=0.3, cy=500, rel=10)
    out = e.update(_obs(fid=2, groups=(gA2, gB2), targets=msA2 + msB2), DT)
    assert out.reason == "switch:score_win" and out.target_id == 2
    # fid3：不足 margin 的组合（C≈51.9 < 67.2+15）与自身回落都不触发再改判
    gB3, msB3 = _group(2, 3, n=6, x=0.3, cy=500, rel=10)
    gC3, msC3 = _group(3, 3, n=4, x=0.4, cy=500, rel=10)
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
