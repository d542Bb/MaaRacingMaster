# -*- coding: utf-8 -*-
"""合成闭环台架：决策→计划→执行车模跑 120 拍。

目的不是验证“开得好”，而是验证第 4 期两个默认关闭的开关（动态视界、
选择层驻留）打开后不会把控制跑飞、不会产生 NaN/越界命令，并让对象表/
车道图/开度/锚/影子视界在长序列里每拍推进。真实效果仍以实车为准。
"""
from __future__ import annotations

import math
from dataclasses import replace

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.corridor import (
    BandBoundary, Corridor, CorridorBand)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.motion_plan import (
    EgoState, plan_motion)
from maaracing_master.plugins.speedrush.planner import (
    _STICK_FULL, LateralPlanner)
from maaracing_master.plugins.speedrush.traffic import CarView
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, PerceptionHealth, TrackedTarget, WorldObservation)

CFG = _read_decision(DECISION_FILE)
DT = 0.05
LW = 3.5


def _corridor():
    """2 道宽直路（拥挤档）：驱动影子视界从 3.0 降到 2.0。"""
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(-1.0 * LW, "measured", 1.0, 1),
                     BandBoundary(1.0 * LW, "measured", 1.0, 1))
        for i in range(11))
    return Corridor(bands, 0.0, 2.0, "pair", "ok")


def _obs(fid, *, coin_cy, car_cy):
    coins = tuple(TrackedTarget(
        id=100 + i, kind="coin", x_lane=0.6 - 0.1 * i, x_sigma=0.1,
        cy=coin_cy + i * 4, w=20, h=20, conf=0.9, rel_approach=1.0,
        first_seen_fid=1, last_seen_fid=fid,
        validity_until_fid=fid + 15) for i in range(3))
    group = CoinGroup(
        group_id=1, member_ids=tuple(c.id for c in coins),
        observed_count=3, estimated_count=3, x_center=0.6, x_span=0.2,
        cy_min=coin_cy, cy_max=coin_cy + 8, conf_min=0.9,
        partial_observation=False, first_seen_fid=1,
        last_seen_fid=fid, validity_until_fid=fid + 24, lead_id=coins[-1].id)
    car = TrackedTarget(
        id=900, kind="car", x_lane=1.0, x_sigma=0.1, cy=car_cy,
        w=40, h=40, conf=0.9, rel_approach=1.0, first_seen_fid=1,
        last_seen_fid=fid, validity_until_fid=fid + 15)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, True, False,
                                raw_cars=1, raw_coins=3),
        boundary=None, targets=coins + (car,), far_targets=(),
        coin_groups=(group,), fid_rate_hz=20.0)


def _run(switches_on: bool, ticks: int = 120):
    cfg = CFG
    if switches_on:
        cfg = replace(cfg, selection=replace(
            cfg.selection, dynamic_horizon=True, layer_dwell=True))
    eng = DecisionEngine(cfg)
    pl = LateralPlanner(cfg.planner)
    x = 0.0
    heading = 0.0
    yaw = 0.0
    v = 45.0
    steer = 0.0
    xs, layers, horizons = [], [], []
    coin_cy, car_cy = 420.0, 420.0
    for fid in range(1, ticks + 1):
        view = CarView(id=900, x_lane=1.0, cy=int(car_cy),
                       rel_approach=1.0, d_min=0.0, age_ticks=fid,
                       last_seen_fid=fid)
        out = eng.update(
            _obs(fid, coin_cy=int(coin_cy), car_cy=int(car_cy)), DT,
            executed_lane=pl.state.executed_lane, corridor=_corridor(),
            traffic=((view,), ()), ego_speed_mps=v,
            ego_v_lat_lane_s=pl.state.v_lat)
        scene = eng.scene
        plan = None
        if scene is not None:
            intent = out.x_target if out.x_target is not None else 0.0
            plan = plan_motion(
                scene, EgoState(pl.state.executed_lane, heading, v, steer),
                cfg.motion, cfg.selection, cfg.planner, None,
                intent_lane=intent)
        cmd = pl.update(
            out, DT, fid, road_offset=x, lane_x=x, lane_ok=True,
            lane_theta=heading, lane_theta_ok=True, lane_fid=fid,
            lane_age_ms=5.0, lane_n_locked=3, plan=plan)
        steer = cmd.steer_x / _STICK_FULL
        assert -_STICK_FULL <= cmd.steer_x <= _STICK_FULL
        a = 1.0 - math.exp(-DT / 0.2)
        yaw += a * (0.46 * steer - yaw)
        heading = max(-0.6, min(0.6, heading + yaw * DT))
        x += v * math.tan(heading) / LW * DT
        xs.append(x)
        layers.append(out.reason)
        if eng.horizon_trace() is not None:
            horizons.append(eng.horizon_trace()["h"])
    return {"x": xs, "layers": layers, "horizons": horizons,
            "eng": eng, "pl": pl}


def test_closed_loop_is_finite_and_bounded_with_switches_on():
    off = _run(False)
    on = _run(True)
    for rep in (off, on):
        assert all(math.isfinite(v) for v in rep["x"])
        assert max(abs(v) for v in rep["x"]) < 2.0
        assert rep["pl"].state.steer_norm is not None
        assert math.isfinite(rep["pl"].state.steer_norm)
    # 影子视界在拥挤档真的降到 2.0
    assert min(on["horizons"]) <= 2.0
    assert on["horizons"].count(2.0) > 10
    # 固定 3s：2.8s 到站的币持续可追；2s 视界 + 驻留后不再选它
    coin_off = sum(1 for r in off["layers"] if r.startswith("sel:coin"))
    coin_on = sum(1 for r in on["layers"] if r.startswith("sel:coin"))
    assert coin_off > 50
    assert coin_on < coin_off
    assert max(off["x"]) > max(on["x"]) + 0.05
