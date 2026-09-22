# -*- coding: utf-8 -*-
"""车流观测层（阶段 C 设计稿 §三 pass 判据表）的回归锁。

锁面：三事件（pass/lost/ghost）各按判据落定、d_min 全寿命 min 口径、闪现不发事件、
同帧重发幂等、frame_id 倒退 fail-loud、非 car 目标不入账。cy 轨迹以 gate0 真源
v_ego 为基准构造（不写像素字面量——同三源分立纪律）。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.config import Traffic, load_decision
from maaracing_master.plugins.speedrush.traffic import (
    OUTCOME_GHOST, OUTCOME_LOST, OUTCOME_PASS, TrafficObserver)
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
    over = Traffic(P.exit_margin_px, P.min_obs_ticks, P.ghost_max_age_ticks, 0.1)
    o = TrafficObserver(over, CAL)
    age = over.ghost_max_age_ticks + 2
    cars = {fid: [(9, CAL.v_ego - 5, 0.7, 8.0)] for fid in range(1, age + 1)}
    _, evs = _drive(o, cars)
    assert [e.outcome for e in evs] == [OUTCOME_PASS]


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
