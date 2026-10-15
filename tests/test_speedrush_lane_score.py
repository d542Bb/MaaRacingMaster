# -*- coding: utf-8 -*-
"""道级收益评分（lane_score，2026-10-12 设计裁决）的回归锁。

锁面：道格归属与出界丢弃、币事件语义（可收/遮挡/远权）、pacing 整窗占用、
碰撞道负额、邻道错身价值（含变道来得及判据）、平手取当前道、executed 缺席
诚实弃权、引擎闸 shadow 接线（默认关=零开销，行为逐位不变）。
数值场景以 decision.json 起值反推（horizon 2.0/crash_t 0.8/pass 30/远权 0.3），
改参数会红——参数与场景是契约面。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine, Scorer
from maaracing_master.plugins.speedrush.lane_score import LANES, LaneScorer
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DistanceSource, MetricCarState, PerceptionHealth,
    TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.traffic import CarView
from maaracing_master.plugins.speedrush.world_model import load_calib
from dataclasses import replace

CAL = load_calib()
_BASE = _read_decision(DECISION_FILE)
CFG = replace(_BASE, lane_scoring=replace(_BASE.lane_scoring, enabled=True),
              # 走廊收益场闸显式钉死（不继承部署态，rollout 翻转不漂）
              corridor_reach=replace(_BASE.corridor_reach,
                                     enabled=False, control=False))

DT = 0.05


def _scorer() -> Scorer:
    s = Scorer(CFG, CAL)
    s.fid_hz = None          # 测试统一走 fallback 时基（rel 按 px/tick 消费）
    return s


def _ls() -> LaneScorer:
    return LaneScorer(CFG, CAL, _scorer())


def _health() -> PerceptionHealth:
    return PerceptionHealth(True, True, True, True, False)


def _obs(groups=(), targets=()):
    return WorldObservation(
        schema_version=1, frame_id=1, ts_ns=50_000_000, frame_age_ms=10.0,
        stage=1, health=_health(), boundary=None,
        targets=tuple(targets), far_targets=(), coin_groups=tuple(groups))


def _group(gid=1, x=0.5, n=3, conf=0.85):
    """币组 fixture：成员 cy=500/rel=5（流 k 可估，t_miss≈0.97s ≤ 窗）。"""
    m = TrackedTarget(id=gid * 100, kind="coin", x_lane=x, x_sigma=0.1,
                      cy=500, w=20, h=20, conf=conf, rel_approach=5.0,
                      first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
    g = CoinGroup(group_id=gid, member_ids=(m.id,), observed_count=n,
                  estimated_count=n, x_center=x, x_span=0.0, cy_min=500,
                  cy_max=500, conf_min=conf, partial_observation=False,
                  first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
    return g, (m,)


def _cv(tid=9, x=0.5, cy=600, rel=10.0, x_memb=None):
    return CarView(id=tid, x_lane=x, cy=cy, rel_approach=rel, d_min=abs(x),
                   age_ticks=5, x_memb=x_memb)


# ---------- 道格归属与出界 ----------

def test_coin_value_lands_on_its_lane():
    """币组按路心系位置入道：x_road=+0.5 → 只有 +0.5 道拿全值 76.5。"""
    g, ms = _group()
    rep = _ls().benefits(_obs((g,), ms), (), 0.0, road_width=None)
    assert rep is not None
    b = dict(zip(LANES, rep.benefits))
    assert b[0.5] == pytest.approx(30 * 3 * 0.85)
    assert b[-1.5] == b[-0.5] == b[1.5] == 0.0
    assert rep.pick == 0.5


def test_out_of_road_readings_discarded():
    """出界门：|x_road|>半宽的币/车不入道格（远场膨胀=几何胡话）。"""
    g, ms = _group(x=4.0)                      # 远超半宽 2.2
    rep = _ls().benefits(_obs((g,), ms), (), 0.0, road_width=None)
    assert all(b == 0.0 for b in rep.benefits)
    car = _cv(x=5.0, cy=600, rel=10.0)
    rep2 = _ls().benefits(_obs((), ()), (car,), 0.0, road_width=None)
    assert all(b == 0.0 for b in rep2.benefits)   # 不 crash 不占道


# ---------- 币事件语义 ----------

def test_coin_blocked_by_earlier_car_in_same_lane():
    """遮挡：本道有车在币到站前到达（t_car=0.9 < t_miss≈0.97 且 ≥ need）→
    币不可收；邻道拿错身保底 30。"""
    g, ms = _group()
    car = _cv(x=0.5, cy=600, rel=6.44)         # t_car=0.9s：∈[need 0.5, 0.8) 之外
    rep = _ls().benefits(_obs((g,), ms), (car,), 0.0, road_width=None)
    b = dict(zip(LANES, rep.benefits))
    assert b[0.5] == pytest.approx(0.0)        # 币被遮挡
    assert b[-0.5] == pytest.approx(30.0)      # 邻道错身保底
    assert b[1.5] == pytest.approx(30.0)
    assert b[-1.5] == pytest.approx(0.0)       # 隔道无缘


def test_pacing_car_blocks_whole_horizon():
    """pacing 车（rel=0）整窗占用：到站时刻对跟速车失效，占用表达。"""
    g, ms = _group()
    car = _cv(x=0.5, cy=600, rel=0.0)
    rep = _ls().benefits(_obs((g,), ms), (car,), 0.0, road_width=None)
    b = dict(zip(LANES, rep.benefits))
    assert b[0.5] == pytest.approx(0.0)        # 整窗占用，币不可收
    assert b[-0.5] == pytest.approx(0.0)       # pacing 车不产生错身（它不过去）


def test_far_coin_gets_direction_weight():
    """前瞻窗外币记远权 0.3（方向信息仍在）：组无成员观测→t_miss=inf 路径。"""
    g = CoinGroup(group_id=1, member_ids=(), observed_count=3,
                  estimated_count=3, x_center=0.5, x_span=0.0, cy_min=400,
                  cy_max=400, conf_min=0.85, partial_observation=False,
                  first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
    rep = _ls().benefits(_obs((g,)), (), 0.0, road_width=None)
    b = dict(zip(LANES, rep.benefits))
    assert b[0.5] == pytest.approx(0.3 * 30 * 3 * 0.85)


# ---------- 碰撞与错身 ----------

def test_crash_lane_gets_big_negative():
    """碰撞道：车到达时刻 ∈ [need, crash_t) 且我们到得了 → 大负额压过一切。"""
    g, ms = _group()
    car = _cv(x=0.5, cy=600, rel=10.0)         # t_car=0.58 ∈ [0.5, 0.8)
    rep = _ls().benefits(_obs((g,), ms), (car,), 0.0, road_width=None)
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(-100.0)


def test_car_arriving_before_we_can_get_there_is_no_threat():
    """t_car < need（我们还没到它就过了）：不撞不挡——道照常可收币。"""
    g, ms = _group(x=-1.5)
    car = _cv(x=-1.5, cy=600, rel=10.0)        # t_car=0.58 < need(1.5 道外)=0.9
    rep = _ls().benefits(_obs((g,), ms), (car,), 0.0, road_width=None)
    b = dict(zip(LANES, rep.benefits))
    assert b[-1.5] == pytest.approx(30 * 3 * 0.85)
    assert b[-0.5] == pytest.approx(30.0)      # 错身价值照给（它先过，我们在旁）


def test_pass_value_needs_lane_change_feasibility():
    """错身价值要变道来得及（t_meet ≥ need）：邻道车 0.58s 到、2 道外
    need=0.9s → 隔道拿不到 30。"""
    car = _cv(x=-0.5, cy=600, rel=10.0)        # t_car=0.58
    rep = _ls().benefits(_obs((), ()), (car,), 0.0, road_width=None)
    b = dict(zip(LANES, rep.benefits))
    assert b[-1.5] == pytest.approx(0.0)       # need(1.5 道外)=0.9 > 0.58
    assert b[0.5] == pytest.approx(30.0)       # need(0.5 道外)=0.5 ≤ 0.58


# ---------- pick 与弃权 ----------

def test_empty_road_tie_breaks_to_current_lane():
    """空旷路道间等价：pick=当前道（防抖默认），不主动挪。"""
    rep = _ls().benefits(_obs((), ()), (), -0.5, road_width=None)
    assert all(b == 0.0 for b in rep.benefits)
    assert rep.pick == -0.5


def test_executed_none_abstains():
    """无路心锚（executed 缺席）=诚实弃权，不臆造道格。"""
    assert _ls().benefits(_obs((), ()), (), None, road_width=None) is None


# ---------- 引擎闸（shadow 接线） ----------

def test_engine_shadow_gate_on_off():
    """闸开：逐拍 LaneReport 可读、行为不变（选择仍走现役栈）；部署 json
    enabled=true（影子期）。闸关构造（replace enabled=False）不建评定器。"""
    e_on = DecisionEngine(CFG)
    assert e_on.lane_scorer is not None
    out = e_on.update(_obs(), DT, executed_lane=0.0)
    assert out.reason == "hold:no_candidate"    # 行为面零变化
    assert e_on.lane_report is not None and len(e_on.lane_report.benefits) == 4
    e_off = DecisionEngine(replace(CFG, lane_scoring=replace(
        CFG.lane_scoring, enabled=False)))
    assert e_off.lane_scorer is None
    e_off.update(_obs(), DT, executed_lane=0.0)
    assert e_off.lane_report is None


# ---------- 米制优先、像素回退（C 线 2026-10-12，计划稿 §5C） ----------

def _ms(tid=9, *, z=10.0, x_m=1.69, v=5.0, t=2.0,
        src=DistanceSource.RESCALED_ANCHOR):
    """米制车 fixture：x_m=1.69m≈半道（lane_w_m 3.38）→ 道 +0.5；
    t=2.0s 缺省在窗外（占用但不撞不挡错身）。"""
    return MetricCarState(target_id=tid, source=src, z_m=z, x_m=x_m,
                          v_close_mps=v, t_meet_s=t, anchor_age_ms=200.0)


def test_metric_car_crash_lane():
    """米制到站 0.6s ∈ [need, crash_t) 且 x_m 落 +0.5 道 → 碰撞负额；
    像素视图不参与（同 id 不双计）。"""
    ms = _ms(t=0.6)
    rep = _ls().benefits(_obs((), ()), (_cv(tid=9),), 0.0, road_width=None,
                         metric_cars=(ms,))
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(-100.0)


def test_metric_far_only_car_enters_lane():
    """只活在米制里的 far 轨（无 CarView——地平线退化带住户）同样入账：
    整窗占用挡掉本道币。这是本次搬家要治的盲区本体。"""
    g, msgs = _group(x=0.5)
    ms = _ms(v=-0.5, t=None)                   # 远离车：占用语义
    rep = _ls().benefits(_obs((g,), msgs), (), 0.0, road_width=None,
                         metric_cars=(ms,))
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(0.0)


def test_metric_pacing_takes_precedence_over_pixel():
    """米制说 pacing（v_close<0）→ 不回退像素 t（像素 rel>0 的噪声不翻案）：
    占用而非碰撞——车道币被挡但不判 −100。"""
    g, msgs = _group(x=0.5)
    ms = _ms(v=-0.5, t=None)
    car = _cv(tid=9, x=0.5, cy=600, rel=10.0)  # 像素口径 t=0.58 会判碰撞
    rep = _ls().benefits(_obs((g,), msgs), (car,), 0.0, road_width=None,
                         metric_cars=(ms,))
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(0.0)


def test_metric_fresh_anchor_falls_back_to_pixel_t():
    """锚新估计窗未开（v_close=None）→ 像素 t_meet 顶上：像素 0.58s 到站
    判碰撞（米制 x_m 已生效定道）。"""
    ms = _ms(v=None, t=None)
    car = _cv(tid=9, x=0.5, cy=600, rel=10.0)  # 像素 t=0.58 ∈ [need 0.5, 0.8)
    rep = _ls().benefits(_obs((), ()), (car,), 0.0, road_width=None,
                         metric_cars=(ms,))
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(-100.0)


def test_no_anchor_pixel_fallback_counted_once():
    """无锚车（UNKNOWN）：像素仪器回退且只记一次（不与米制路径双计）。"""
    ms = _ms(src=DistanceSource.UNKNOWN, z=None, x_m=None, v=None, t=None)
    car = _cv(tid=9, x=0.5, cy=600, rel=10.0)
    rep = _ls().benefits(_obs((), ()), (car,), 0.0, road_width=None,
                         metric_cars=(ms,))
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(-100.0)


def test_metric_x_uses_lane_width_conversion():
    """米制横位按 lane_w_m 换道（1.69m≈0.5 道）：x_m 定道，不经过成员位
    投票——米制读数本身即整道归属。"""
    g, msgs = _group(x=0.5)
    ms = _ms(x_m=1.69, v=-0.5, t=None)         # 道 +0.5：币被占
    rep = _ls().benefits(_obs((g,), msgs), (), 0.0, road_width=None,
                         metric_cars=(ms,))
    assert dict(zip(LANES, rep.benefits))[0.5] == pytest.approx(0.0)
    assert dict(zip(LANES, rep.benefits))[1.5] == pytest.approx(0.0)


def test_engine_metric_gate():
    """供数闸：默认开（部署 json metric_cars.enabled=true）→ 实例通道透传；
    闸关 → 引擎不吃 obs.metric_cars（回退=与 C 线前逐位同构）。"""
    ms = _ms()
    obs = _obs((), ())
    obs = replace(obs, metric_cars=(ms,))
    e_on = DecisionEngine(CFG)                  # CFG.metric_cars 默认开
    e_on.update(obs, DT, executed_lane=0.0)
    assert e_on._metric_cars == (ms,)
    e_off = DecisionEngine(replace(CFG, metric_cars=replace(
        CFG.metric_cars, enabled=False)))
    e_off.update(obs, DT, executed_lane=0.0)
    assert e_off._metric_cars == ()
