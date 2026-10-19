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
    Scene, build_scene)
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
