# -*- coding: utf-8 -*-
"""车流观测层（阶段 C 设计稿 §三 pass 判据表）的回归锁。

锁面：三事件（pass/lost/ghost）各按判据落定、d_min 全寿命 min 口径、闪现不发事件、
同帧重发幂等、frame_id 倒退 fail-loud、非 car 目标不入账。cy 轨迹以 gate0 真源
v_ego 为基准构造（不写像素字面量——同三源分立纪律）。
"""
from __future__ import annotations

import dataclasses

import pytest

from maaracing_master.plugins.speedrush.config import Traffic, load_decision
from maaracing_master.plugins.speedrush.traffic import (
    OUTCOME_GHOST, OUTCOME_LOST, OUTCOME_PASS, OUTCOME_PRESUMED_PASS,
    TrafficObserver)
from maaracing_master.plugins.speedrush.tracking import (
    PerceptionHealth, TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import KIND_CAR, KIND_COIN, load_calib

CAL = load_calib()
P = load_decision().traffic


def _health() -> PerceptionHealth:
    return PerceptionHealth(perception_alive=True, frame_fresh=True,
                            geometry_valid=True, target_presence=True,
                            stage_transition=False)


def _car(tid: int, fid: int, cy: float, x_lane: float, rel: float) -> TrackedTarget:
    return TrackedTarget(id=tid, kind=KIND_CAR, x_lane=x_lane, x_sigma=0.05,
                         cy=int(cy), w=80, h=40, conf=0.9, rel_approach=rel,
                         first_seen_fid=max(1, fid - 1), last_seen_fid=fid,
                         validity_until_fid=fid + P.min_obs_ticks)


def _obs(fid: int, targets: tuple[TrackedTarget, ...]) -> WorldObservation:
    return WorldObservation(schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
                            frame_age_ms=20.0, stage=1, health=_health(),
                            boundary=None, targets=targets, far_targets=())


def _drive(o: TrafficObserver, cars_by_fid: dict[int, list[tuple]]) -> tuple:
    """按帧喂车；cars_by_fid[fid] = [(id, cy, x_lane, rel), ...]，缺省=空帧（触发落定）。"""
    events, views = [], []
    fid = 0
    for fid in range(1, max(cars_by_fid) + 2):
        tg = tuple(_car(i, fid, cy, x, r) for (i, cy, x, r) in cars_by_fid.get(fid, []))
        v, e = o.update(_obs(fid, tg))
        events.extend(e)
        views = v
    return views, events


# ---------- 三事件 ----------

def test_pass_when_car_exits_below_ego_row():
    o = TrafficObserver(P, CAL)
    near = CAL.v_ego - 10          # 自车行附近（下带内）
    far = CAL.y_h + 60
    cars = {1: [(7, far, 1.2, 60)], 2: [(7, far + 80, 1.0, 60)],
            3: [(7, near - 40, 0.7, 60)], 4: [(7, near, 0.65, 60)]}
    views, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_PASS]
    assert evs[0].track_id == 7
    assert evs[0].d_min == 0.65    # 全寿命 min|x_lane|（C4 标定口径）
    assert views == ()


def test_lost_when_disappears_uproad_not_bottom():
    o = TrafficObserver(P, CAL)
    cars = {1: [(3, CAL.y_h + 50, 1.0, 5)], 2: [(3, CAL.y_h + 60, 1.0, 5)],
            3: [(3, CAL.y_h + 55, 1.0, 5)]}   # 之后消失：在远端，不是从车尾出画
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_LOST]
    assert evs[0].d_min is None    # 没超成不产标定数据（宁缺毋滥）


def test_ghost_when_aged_slow_then_bottom_exit():
    o = TrafficObserver(P, CAL)
    age = P.ghost_max_age_ticks + 3
    cars = {fid: [(9, CAL.v_ego - 5, 0.7, 0.2) for _ in range(1)] for fid in range(1, age + 1)}
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_GHOST]


def test_bottom_exit_but_fast_still_pass_not_ghost():
    """超龄但 rel 超鬼影界 = 真慢车被追上也算超车，不误判 ghost。"""
    over = dataclasses.replace(P, ghost_rel_eps=0.1)
    o = TrafficObserver(over, CAL)
    age = over.ghost_max_age_ticks + 2
    cars = {fid: [(9, CAL.v_ego - 5, 0.7, 8.0)] for fid in range(1, age + 1)}
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_PASS]


# ---------- 推定超越（presumed_pass，2026-10-08 §4A 取证定档）----------

def test_presumed_pass_when_dies_beside_ego_sliding():
    """检测死区修复：真检贴身（≥presumed_cy_px）+ 邻道 + 仍下滑 → 推定超越。"""
    o = TrafficObserver(P, CAL)
    hi = P.presumed_cy_px + 30
    cars = {1: [(7, P.presumed_cy_px - 40, 1.3, 20)],
            2: [(7, P.presumed_cy_px, 1.35, 18)],
            3: [(7, hi, 1.4, 15)],
            4: []}                      # 检测死区：车没被再看到，轨迹消失
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_PRESUMED_PASS]
    assert evs[0].d_min == 1.3          # 推定超越也是真超越，d_min 进标定面


def test_lost_when_dies_below_presumed_band():
    """远车夭折（真检 cy 在地平线带）不晋升——404~431 数据缺口防泄漏位。"""
    o = TrafficObserver(P, CAL)
    cars = {1: [(7, P.presumed_cy_px - 60, 1.2, 20)],
            2: [(7, P.presumed_cy_px - 50, 1.2, 18)],
            3: [(7, P.presumed_cy_px - 55, 1.2, 16)],
            4: []}
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_LOST]
    assert evs[0].d_min is None


def test_lost_when_receding_at_death():
    """rel<0（cy 收缩=车在上移远离）：贴身也不算超越中消失。"""
    o = TrafficObserver(P, CAL)
    cars = {1: [(7, P.presumed_cy_px + 40, 1.3, 10)],
            2: [(7, P.presumed_cy_px + 30, 1.3, -25)],
            3: [(7, P.presumed_cy_px + 20, 1.3, -30)],
            4: []}
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_LOST]


def test_lost_when_dies_beyond_adjacent_band():
    """横向超出邻道界（|x_lane|>presumed_lane_max）：不判推定超越。"""
    o = TrafficObserver(P, CAL)
    cars = {1: [(7, P.presumed_cy_px + 30, 3.0, 20)],
            2: [(7, P.presumed_cy_px + 40, 3.0, 15)],
            3: [(7, P.presumed_cy_px + 50, 3.0, 12)],
            4: []}
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_LOST]


# ---------- 自卫与契约 ----------

def test_flicker_no_event():
    """观测次数 < min_obs_ticks 即消失：不发任何事件（检测噪声不记账）。"""
    o = TrafficObserver(P, CAL)
    cars = {1: [(5, CAL.v_ego - 5, 0.8, 50)]}   # 只出现一帧
    _, evs = _drive(o, cars)
    assert evs == []


def test_coins_not_tracked():
    """非 car 目标不入账（金币组聚合有专层，边界不糊）。"""
    o = TrafficObserver(P, CAL)
    coin = TrackedTarget(id=1, kind=KIND_COIN, x_lane=0.0, x_sigma=0.1,
                         cy=int(CAL.v_ego), w=10, h=10, conf=0.9, rel_approach=50,
                         first_seen_fid=1, last_seen_fid=1, validity_until_fid=3)
    v, e = o.update(_obs(1, (coin,)))
    assert v == () and e == ()


def test_same_frame_idempotent():
    """同帧重发幂等（WGC 中心缓存纪律，与 Tracker 同款）：结果对象完全一致。"""
    o = TrafficObserver(P, CAL)
    obs = _obs(2, (_car(7, 2, CAL.v_ego - 10, 0.7, 60),))
    assert o.update(obs) == o.update(obs)


def test_frame_id_regress_fail_loud():
    o = TrafficObserver(P, CAL)
    o.update(_obs(10, ()))
    with pytest.raises(ValueError):
        o.update(_obs(9, ()))


# ---------- 阶段一：街车横向速率账本（CV 外推数据源） ----------

def test_car_view_lateral_rate_ema():
    """CarView.v_lat（车道/tick，右正）=帧间差分 EMA：横移车收敛正速率、
    静止车恒零、遮挡延续拍（last_seen 落后于 fid）不投毒——x 突变的外推
    读数不得被计成速率。"""
    o = TrafficObserver(P, CAL)
    v = ()
    for fid in range(1, 11):                        # x 每 tick +0.02 = 0.4 车道/s
        v, _ = o.update(_obs(fid, (_car(7, fid, CAL.y_h + 60, 1.0 + 0.02 * fid, 5),)))
    assert v[0].v_lat == pytest.approx(0.02, abs=0.004)
    o2 = TrafficObserver(P, CAL)
    for fid in range(1, 8):
        v, _ = o2.update(_obs(fid, (_car(7, fid, CAL.y_h + 60, 1.0, 5),)))
    assert abs(v[0].v_lat) < 0.004
    o3 = TrafficObserver(P, CAL)
    o3.update(_obs(1, (_car(7, 1, CAL.y_h + 60, 1.0, 5),)))
    stale = TrackedTarget(id=7, kind=KIND_CAR, x_lane=2.0, x_sigma=0.05,
                          cy=int(CAL.y_h + 80), w=80, h=40, conf=0.9, rel_approach=5,
                          first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
    v, _ = o3.update(_obs(2, (stale,)))             # last_seen=1 < fid=2：延续拍
    assert v[0].v_lat == pytest.approx(0.0, abs=1e-9)


# ---------- 车道成员资格投票（2026-10-11 前置三件套②） ----------

def test_membership_votes_through_noise():
    """带内车 x 读数噪声/膨胀（V0 病灶①，car 51 案形态 1.34→2.38→1.44）：
    偶发出带读数停留少数派不翻成员位，连续一致读数成立成员位=整道中心。"""
    o = TrafficObserver(P, CAL)
    v = ()
    for f, x in {1: 1.3, 2: 2.4, 3: 1.4, 4: 1.1, 5: 1.2, 6: 1.35}.items():
        v, _ = o.update(_obs(f, (_car(51, f, 400.0, x, 5.0),)))
    assert v[0].x_memb == 1.0                        # 2.4 的噪声票没翻位


def test_membership_follows_true_lane_change():
    """真横移要及时响应（验证边界②）：稳定 1 道后真实变到 2 道，连续新读数
    数拍内翻成员位（衰减票仓 0.7：旧质量 2.77，两拍新票即反超）。"""
    o = TrafficObserver(P, CAL)
    v = ()
    for fid in range(1, 12):
        x = 1.2 if fid < 6 else 2.1
        v, _ = o.update(_obs(fid, (_car(7, fid, 450.0, x, 5.0),)))
    assert v[0].x_memb == 2.0


def test_membership_does_not_inherit_across_track_switch():
    """身份连续性边界（验证边界①）：track 切换（同位再现按新 id，旧 id 消失
    落定）不继承旧票仓——单拍读数不成成员位。"""
    o = TrafficObserver(P, CAL)
    for fid in range(1, 6):
        o.update(_obs(fid, (_car(7, fid, 450.0, 1.2, 5.0),)))
    v, _ = o.update(_obs(6, (_car(8, 6, 450.0, 2.4, 5.0),)))
    assert [x.id for x in v] == [8]
    assert v[0].x_memb is None                       # 一拍读数不成成员位


def test_membership_sigma_gate_keeps_distorted_readings_out():
    """σ 超门的失真读数（远场/弯道）不进票仓：近场保守，弯道退化安全形态。"""
    o = TrafficObserver(P, CAL)
    v = ()
    for fid in range(1, 8):
        t = TrackedTarget(id=9, kind=KIND_CAR, x_lane=2.4, x_sigma=0.9,
                          cy=450, w=80, h=40, conf=0.9, rel_approach=5.0,
                          first_seen_fid=1, last_seen_fid=fid,
                          validity_until_fid=fid + 8)
        v, _ = o.update(_obs(fid, (t,)))
    assert v[0].x_memb is None


def test_membership_holds_through_occlusion():
    """遮挡延续拍成员位保持（票仓只在真实读数拍动）：宽限内不闪退单拍。"""
    o = TrafficObserver(P, CAL)
    for fid in range(1, 5):
        o.update(_obs(fid, (_car(7, fid, 450.0, 1.2, 5.0),)))
    stale = TrackedTarget(id=7, kind=KIND_CAR, x_lane=1.2, x_sigma=0.05,
                          cy=470, w=80, h=40, conf=0.9, rel_approach=5,
                          first_seen_fid=1, last_seen_fid=3, validity_until_fid=11)
    v, _ = o.update(_obs(5, (stale,)))              # last_seen=3 < fid=5：延续拍
    assert v[0].x_memb == 1.0


# ---------- 宽度有效性面（2026-10-11 前置三件套③） ----------

def test_width_validity_hold_and_ttl_expiry():
    """有效读数出 w_m（年龄/置信/分母三门）；遮挡延续拍保持最后有效值并带
    龄；超 TTL 显式转 None 不冒充有效。"""
    o = TrafficObserver(P, CAL, conf_floor=0.5)
    for fid in range(1, 6):                         # 5 拍真实读数：f3 起年龄达标
        o.update(_obs(fid, (_car(7, fid, 500.0, 1.2, 5.0),)))
    v, _ = o.update(_obs(5, (_car(7, 5, 500.0, 1.2, 5.0),)))
    assert v[0].w_m is not None and v[0].w_m_age == 0
    exp_w = 80 * CAL.a_x * CAL.lane_w_m / (500.0 - CAL.y_h)
    assert v[0].w_m == pytest.approx(exp_w, rel=1e-6)
    for age in range(1, P.width_ttl_ticks + 1):     # 遮挡延续：保持+带龄
        stale = TrackedTarget(id=7, kind=KIND_CAR, x_lane=1.2, x_sigma=0.05,
                              cy=int(500.0 + 20 * age), w=80, h=40, conf=0.9,
                              rel_approach=5, first_seen_fid=1, last_seen_fid=5,
                              validity_until_fid=20)
        v, _ = o.update(_obs(5 + age, (stale,)))
        assert v[0].w_m == pytest.approx(exp_w, rel=1e-6)   # 外推 cy 不重算
        assert v[0].w_m_age == age
    stale = TrackedTarget(id=7, kind=KIND_CAR, x_lane=1.2, x_sigma=0.05,
                          cy=700, w=80, h=40, conf=0.9, rel_approach=5,
                          first_seen_fid=1, last_seen_fid=5, validity_until_fid=30)
    v, _ = o.update(_obs(5 + P.width_ttl_ticks + 1, (stale,)))
    assert v[0].w_m is None and v[0].w_m_age is None   # 过期=显式 unknown


def test_width_gates_low_conf_and_missing_floor():
    """置信低于地板不出宽度；conf_floor 未供（None）宽度面整体不出——
    诚实降级不装懂。"""
    o = TrafficObserver(P, CAL, conf_floor=0.5)
    low = TrackedTarget(id=7, kind=KIND_CAR, x_lane=1.2, x_sigma=0.05,
                        cy=500, w=80, h=40, conf=0.3, rel_approach=5.0,
                        first_seen_fid=1, last_seen_fid=6, validity_until_fid=14)
    v, _ = o.update(_obs(6, (low,)))
    assert v[0].w_m is None
    o2 = TrafficObserver(P, CAL)                    # 缺省无地板：宽度面不出
    v, _ = o2.update(_obs(6, (_car(7, 6, 500.0, 1.2, 5.0),)))
    assert v[0].w_m is None
