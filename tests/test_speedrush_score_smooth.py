# -*- coding: utf-8 -*-
"""得分源平滑（第 7 期）回归锁：两位口径与证据生命周期。

锁定：
- 证据位（平滑）只进请求发出与证据累计；动作位（原始 slack）决定目标与
  起意——**平滑永远不能把不可达拍变成起意拍**（离线 stuck7=0 的单点锁）。
- 证据消失（平滑 slack<0）立即停发请求（路线照旧按丢检冻结/清零）。
- 开关关或对象无身份时两位同源（第 6 期行为逐位不变）。

场景数值：逐拍 slack 抖动 ±1s（实测 220134 oid 14 序列），原始可达门在
0 边界反复翻面；平滑 τ=0.2s 后确认数 3→6、6→9 且无「够格但起不了意」
（世界状态实验区的窗口复算仪，2026-11-01 两局口径）。
"""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.coarse import CoarseLane, CoarseReport
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.route import (
    PHASE_KEEP, PHASE_MERGING, RouteParams, init_state, tick_route)
from maaracing_master.plugins.speedrush.selection import (
    Obj, PendingReq, RoadBand, RoadFrame, SelectionParams, _ev_t, _pick_coin,
    reach_slack)

LW = 3.38
DT = 0.05
CFG = _read_decision(DECISION_FILE)

P = RouteParams(
    switch_gain_m=CFG.coarse.switch_margin_m,
    proxy_speed_mps=CFG.coarse.range_speed_mps,
    urgent_confirm_s=CFG.route.urgent_confirm_s,
    opportunity_confirm_s=CFG.route.opportunity_confirm_s,
    cancel_unsafe_s=CFG.route.cancel_unsafe_s,
    cancel_cooldown_s=CFG.route.cancel_cooldown_s,
    finish_tol_lane=CFG.selection.center_band_lane,
    finish_v_tol_lane_s=CFG.route.finish_v_tol_lane_s,
    merge_max_s=CFG.route.merge_max_s)
OPP_TICKS = round(P.opportunity_confirm_s / DT)
CLEAR = (24.0, None, False)
WINDOW_M = CLEAR[0]


def _road(center=0.0, width_lanes=4.0):
    half = width_lanes * LW / 2.0
    bands = tuple(
        RoadBand(z0, z0 + 3.0, center - half, center + half)
        for z0 in range(-2, 25, 3))
    return RoadFrame(bands, width_lanes * LW, center - half,
                     center + half, True)


def _p(**kw):
    return SelectionParams(lane_w_m=LW, **kw)


def _rep(ego=0.0, spec=None):
    """五条候选道（±2）的原始粗层报告；spec 语义同 test_speedrush_route。"""
    spec = spec or {}
    lanes = []
    for k in (-2.0, -1.0, 0.0, 1.0, 2.0):
        free, blocker, unknown = spec.get(k, CLEAR)
        lanes.append(CoarseLane(k, free, 1.0, blocker, unknown))
    return CoarseReport(ego, 0.0, 0, WINDOW_M, "keep", tuple(lanes))


# ---------- 证据位 / 动作位的数据结构 ----------

def test_ev_t_prefers_smoothed_and_falls_back():
    """平滑值优先；未平滑（第 6 期）两位同源。"""
    raw = Obj("coin", 3.0, 0.5)
    sm = Obj("coin", 3.0, 0.5, t_ev=3.0)
    assert _ev_t(raw) == 0.5
    assert _ev_t(sm) == 3.0


def test_coin_request_gated_by_evidence_bit():
    """出跨度请求：证据位（平滑可达）决定发出，动作位（原始不可达）如实
    记 False——证据可以继续累计，路线不得按这一拍起意。"""
    p = _p()
    road = _road()
    # 承诺道中心 0：币在 +1 道中心（出 ±半道跨度）；原始到站 0.5s 来不及，
    # 平滑证据位 3.0s 可达。
    coin = Obj("coin", LW, 0.5, oid=7, t_ev=3.0)
    hit, pend = _pick_coin([coin], [], road, 0.0, p, 0.0)
    assert hit is None
    assert isinstance(pend, PendingReq) and pend.lane == 1.0
    assert pend.action is False
    # 原始与平滑都不可达：不发请求
    coin2 = Obj("coin", LW, 0.5, oid=7, t_ev=0.4)
    hit, pend = _pick_coin([coin2], [], road, 0.0, p, 0.0)
    assert hit is None and pend is None
    # 原始也可达：动作位 True（第 6 期起意路径不受影响）
    coin3 = Obj("coin", LW, 3.0, oid=7, t_ev=3.0)
    hit, pend = _pick_coin([coin3], [], road, 0.0, p, 0.0)
    assert hit is None and pend is not None and pend.action is True


# ---------- 路线层：动作位进起意门 ----------

def _req_run_action(state, request, n, action):
    """连续 n 拍同侧请求（带动作位），返回末拍状态与定案。"""
    v = None
    for _ in range(n):
        state, v = tick_route(state, _rep(0.0, {0.0: CLEAR}), P, dt_s=DT,
                              epoch=0, request=request,
                              request_live=True, request_action=action)
    return state, v


def test_merge_waits_for_action_bit():
    """证据攒满但本拍原始不可达（动作位 False）→ 不起意；原始可达拍起意。
    平滑不能把不可达拍变成起意拍（stuck7=0 的单点锁）。"""
    s, _ = tick_route(init_state(0), _rep(0.0), P, dt_s=DT, epoch=0)
    s, v = _req_run_action(s, 1.0, OPP_TICKS, False)
    assert s.phase == PHASE_KEEP
    assert v.verdict == "keep" and v.why == "no_reach"   # 证据够、原始不可达
    s, v = _req_run_action(s, 1.0, 1, True)
    assert v.verdict == "merge_start" and v.why == "opportunity"
    assert v.lane_ref == 1.0 and s.phase == PHASE_MERGING


def test_action_bit_none_keeps_phase6_behavior():
    """动作位 None（未平滑/第 6 期）：不设额外门，原始路径逐位不变。"""
    s, _ = tick_route(init_state(0), _rep(0.0), P, dt_s=DT, epoch=0)
    s, v = _req_run_action(s, 1.0, OPP_TICKS, None)
    assert v.verdict == "merge_start" and v.why == "opportunity"


# ---------- EMA 状态机（决策端写回 t_ev） ----------

def _engine(score_smooth=True):
    cfg = CFG
    if not score_smooth:
        cfg = replace(cfg, selection=replace(cfg.selection,
                                             score_smooth=False))
    eng = DecisionEngine(cfg)
    eng._dt_last = DT
    return eng


def test_smooth_score_writes_ev_and_raw_untouched():
    """EMA 写回证据位；原始 t 不动；reach_slack(x, ego, t_ev) = 平滑 slack。"""
    eng = _engine()
    x = 2.0
    p = eng.selector.p
    objs = [Obj("coin", x, 2.0, oid=11)]
    out = eng._smooth_score(objs, 0.0)
    assert out[0].t == 2.0 and out[0].t_ev == 2.0     # 首拍：平滑=原始
    out = eng._smooth_score([Obj("coin", x, 1.0, oid=11)], 0.0)
    o = out[0]
    assert o.t == 1.0 and o.t_ev is not None and o.t_ev != 1.0
    a = 1.0 - math.exp(-DT / CFG.selection.score_smooth_tau_s)
    s2 = reach_slack(x, 0.0, 2.0, p)          # 首拍原始 slack（EMA 起点）
    s1 = reach_slack(x, 0.0, 1.0, p)          # 本拍原始 slack
    expect = s2 + a * (s1 - s2)               # 与 _smooth_score 同一 EMA
    assert reach_slack(x, 0.0, o.t_ev, p) == pytest.approx(expect, abs=1e-9)


def test_smooth_score_switch_off_and_no_identity():
    """开关关或对象无身份：不平滑，两位同源（第 6 期逐位不变）。"""
    eng = _engine(score_smooth=False)
    out = eng._smooth_score([Obj("coin", 2.0, 2.0, oid=11)], 0.0)
    assert out[0].t_ev is None
    eng2 = _engine()
    out = eng2._smooth_score([Obj("coin", 2.0, 2.0, oid=-1)], 0.0)
    assert out[0].t_ev is None
