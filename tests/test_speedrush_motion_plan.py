# -*- coding: utf-8 -*-
"""A1 场景快照契约：帧/符号/单位、年龄与 σ、走廊陈旧、空场景。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.corridor import (
    BandBoundary, Corridor, CorridorBand)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.motion_plan import (
    EgoState, PlanGate, Scene, _Cand, build_scene, plan_motion)
from maaracing_master.plugins.speedrush.selection import (
    BonusSource, CarSource, CoinSource, RoadBand, RoadFrame)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, PerceptionHealth, TrackedTarget, WorldObservation)

LW = 3.38
CFG = _read_decision(DECISION_FILE)


def _road(*, src="measured", age_ms=None, conf=1.0):
    return RoadFrame(
        (RoadBand(0.0, 6.0, -4.0, 4.0, src=src, age_ms=age_ms, conf=conf),),
        8.0, -4.0, 4.0, True)


def _scene(**kw):
    args = dict(
        fid=10, ego_m=1.0, road=_road(), rewards=[], cars=[], coins=[],
        executed_lane=0.0, origin=0.0, lane_w_m=LW, frame_age_ms=20.0,
        corridor_age_ms=50.0, max_obj_age_s=0.25, corridor_age_max_s=0.25,
        sigma_obj0_m=0.15, sigma_obj_rate_mps=0.6, cam_z_datum_m=1.79)
    args.update(kw)
    return build_scene(**args)


def test_frame_symbols_and_units():
    s = _scene(origin=5.0, rewards=[
        BonusSource(10.0, 1.0, 0.9, age_ms=100.0, src="pixel")],
        coins=[CoinSource(8.0, 3, 0.8, 1.5, age_ms=200.0)],
        cars=[CarSource(12.0, 0.5, z_m=5.0, age_ms=50.0, src="metric")])
    r, car, c = s.objs
    assert (r.kind, c.kind, car.kind) == ("reward", "coin", "car")
    assert r.x_m == pytest.approx(5.0)          # 源 - origin
    assert c.x_m == pytest.approx(3.0)
    # 车按 z 处中心线回正（本测试路心恒 0）
    assert car.x_m == pytest.approx(7.0)
    assert r.sigma_m == pytest.approx(0.15 + 0.6 * 0.1)
    assert c.sigma_m == pytest.approx(0.15 + 0.6 * 0.2)
    assert s.ego_lane == pytest.approx(1.0 / LW)


def test_old_objects_dropped_and_corridor_stale():
    s = _scene(rewards=[BonusSource(0.0, 1.0, 1.0, age_ms=300.0)],
               corridor_age_ms=300.0)
    assert s.objs == ()                          # 超龄丢弃
    assert s.stale is True
    assert _scene(corridor_age_ms=None).stale is False


def test_empty_scene():
    s = _scene()
    assert s.objs == () and s.stale is False
    assert s.as_trace()["n"] == 0


def _corridor(*, fid=1, last_fid=1):
    bands = tuple(CorridorBand(
        -2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
        BandBoundary(-4.0, "measured", 1.0, last_fid),
        BandBoundary(4.0, "measured", 1.0, last_fid)) for i in range(3))
    return Corridor(bands, 0.0, 8.0 / LW, "pair", "ok")


def _obs(fid, groups=(), targets=()):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, bool(groups or targets), False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=20.0)


def _group(fid, n=3, x=0.4):
    members = tuple(TrackedTarget(
        id=100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=470 + i * 3, w=20, h=20, conf=0.85, rel_approach=2.5,
        first_seen_fid=1, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n))
    g = CoinGroup(group_id=1, member_ids=tuple(m.id for m in members),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=470,
                  cy_max=470 + 3 * (n - 1), conf_min=0.85,
                  partial_observation=False, first_seen_fid=1,
                  last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, members


def test_engine_exposes_scene_and_clears_without_road():
    eng = DecisionEngine(CFG)
    g, ms = _group(3)
    out = None
    for fid in (1, 2, 3):
        out = eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                         executed_lane=0.0, corridor=_corridor(fid=fid,
                                                               last_fid=fid))
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
    assert out.reason == "sel:coin"
    scene = eng.scene
    assert isinstance(scene, Scene)
    assert scene.fid == 3 and len(scene.objs) == 1
    assert scene.objs[0].kind == "coin"
    assert scene.objs[0].count == 3
    # 组上次观测在上一拍：20Hz 下年龄=50ms
    assert scene.objs[0].age_ms == pytest.approx(50.0)

    eng.update(_obs(3, groups=(g,), targets=ms), 0.05,
               executed_lane=0.0, corridor=None)
    assert eng.scene is None


def test_corridor_stale_uses_band_age():
    eng = DecisionEngine(CFG)
    # last_fid 远落后于当前 fid：20Hz 下 10 拍=500ms > 250ms
    out = eng.update(_obs(11), 0.05, executed_lane=0.0,
                     corridor=_corridor(fid=11, last_fid=1))
    assert out.reason == "sel:center"
    assert eng.scene is not None and eng.scene.stale is True


# ---------- A2 评估器 ----------

def _plan(scene, *, ego=None, motion=None, gate=None):
    return plan_motion(scene, ego or EgoState(0.0, 0.0, 0.0),
                       motion or CFG.motion, CFG.selection, CFG.planner, gate)


def _wide_road():
    return RoadFrame((RoadBand(0.0, 6.0, -5.0, 5.0, src="measured",
                               age_ms=0.0, conf=1.0),), 10.0, -5.0, 5.0, True)


def test_eight_candidates_and_determinism():
    s = _scene(road=_wide_road())
    a, b = _plan(s), _plan(s)
    assert len(a.cand_x) == 8
    assert (a.chosen, a.score_key, a.x_preview_m) == \
        (b.chosen, b.score_key, b.x_preview_m)


def test_reward_beats_coin_and_center():
    s = _scene(road=_wide_road(),
               rewards=[BonusSource(2.0, 1.0, 0.9, age_ms=0.0)],
               coins=[CoinSource(-2.0, 3, 0.8, 1.0, age_ms=0.0)])
    p = _plan(s)
    assert p.valid and p.layer == 1
    assert s.road.bands  # 环境自洽
    center_only = _plan(_scene(road=_wide_road()))
    assert center_only.layer == 4


def test_coin_layer_when_no_car_or_reward():
    p = _plan(_scene(road=_wide_road(),
                     coins=[CoinSource(1.5, 5, 0.9, 1.0, age_ms=0.0)]))
    assert p.valid and p.layer == 3
    assert p.score_key[5] <= -5.0          # -coin_count


def test_collision_all_infeasible_keeps_max_clearance():
    cars = [CarSource(-2.0, 0.5, age_ms=0.0),
            CarSource(0.0, 0.5, age_ms=0.0),
            CarSource(2.0, 0.5, age_ms=0.0)]
    p = _plan(_scene(road=_wide_road(), cars=cars))
    assert not p.valid
    assert p.reason == "plan:infeasible_collision"
    assert p.n_feasible == 0


def test_corridor_all_infeasible_holds_position():
    road = RoadFrame((RoadBand(0.0, 6.0, 4.0, 5.0, src="measured",
                               age_ms=0.0, conf=1.0),), 1.0, 4.0, 5.0, True)
    p = _plan(_scene(road=road))
    assert not p.valid and p.reason == "plan:infeasible_corridor"


def test_corridor_checked_only_near_term():
    """恒定杆 2s 必然驶出包络；走廊只验前 1s，不能把必要避让判成不可行。"""
    road = RoadFrame((RoadBand(0.0, 6.0, -6.0, 6.0, src="measured",
                               age_ms=0.0, conf=1.0),), 12.0, -6.0, 6.0, True)
    p = _plan(_scene(road=road, ego_m=0.0))
    assert p.valid
    assert max(p.cand_x[5]) > 6.0          # +0.55 杆 2s 确实出镜


def test_stale_scene_invalid():
    p = _plan(_scene(road=_wide_road(), corridor_age_ms=999.0))
    assert not p.valid and p.reason == "plan:stale"


def test_sigma_ego_shortens_horizon():
    m = replace(CFG.motion, sigma_ego0_m=0.1,
                sigma_ego_rate_mps=2.0, sigma_ego_max_m=0.5)
    p = _plan(_scene(road=_wide_road()), motion=m)
    assert p.reason == "plan:ok_shortened"
    assert len(p.cand_x[0]) < int(round(CFG.motion.horizon_s / CFG.motion.dt_s)) + 1


def _c(idx, layer, coin, feasible=True):
    key = (not feasible, 0, 0.0, 0, 0.0, -coin, 0.0, 0.0, 0.0, idx)
    return _Cand(idx, 0.0, (0.0,), 0.0, 0.0, feasible, 9.0,
                 0, 0.0, 0, 0.0, coin, 0.0, 0.0, 0.0, layer, key)


def test_gate_same_layer_requires_margin_and_dwell():
    g = PlanGate(switch_margin=0.3, dwell_s=0.1, epsilon=1e-3)
    inc, better = _c(3, 3, 1.0), _c(4, 3, 2.0)
    assert g.choose([inc], 0.05).idx == 3
    assert g.choose([better, inc], 0.05).idx == 3      # 第一次只计 streak
    assert g.choose([better, inc], 0.05).idx == 4      # 0.1s 后接管
    # 层级升高立即切
    reward = _c(5, 1, 0.0)
    assert g.choose([reward, better], 0.05).idx == 5


def test_planner_tracks_valid_plan_only():
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    from maaracing_master.plugins.speedrush.tracking import (
        DecisionOutput, DecisionState)
    s = _scene(road=_wide_road(),
               coins=[CoinSource(1.5, 5, 0.9, 1.0, age_ms=0.0)])
    p = _plan(s)
    out = DecisionOutput(schema_version=5, state=DecisionState.CRUISE,
                         x_target=0.0, reason="x", emitted_fid=1,
                         valid_until_fid=10)
    pl = LateralPlanner(CFG.planner)
    x_pred = pl.state.executed_lane + pl.state.v_lat_est * CFG.planner.lookahead_tau_s
    exp = (p.u_feedforward
           + CFG.planner.k_p * (p.x_preview_lane - x_pred)
           + CFG.planner.k_d * (p.v_preview_lane_s - pl.state.v_lat_est))
    pl.update(out, 0.05, 1, plan=p)
    assert pl.last_steer_raw == pytest.approx(max(-1.0, min(1.0, exp)))

    pl2 = LateralPlanner(CFG.planner)
    pl2.update(out, 0.05, 1, plan=replace(p, valid=False))
    assert pl2.last_steer_raw != pytest.approx(exp)
