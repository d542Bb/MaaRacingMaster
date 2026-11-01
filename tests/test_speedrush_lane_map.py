# -*- coding: utf-8 -*-
"""持久车道图回归锁（第 2 期）：弧长预测、横移补偿、置信衰减、粗带不覆盖
实测、误差样本与支撑距离。"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.lane_map import LaneMap
from maaracing_master.plugins.speedrush.selection import RoadBand

LW = 3.5


def _band(z0, z1, c, *, src="measured", conf=1.0, age=0.0, half=1.75):
    return RoadBand(z0, z1, c - half, c + half, src=src,
                    age_ms=age, conf=conf)


def test_measured_band_fills_nodes_and_support():
    m = LaneMap(LW)
    tr = m.update(bands=[_band(0.0, 24.0, 1.0)], dt_s=0.05)
    assert m.at(20.0).center == pytest.approx(1.0, abs=1e-6)
    assert tr["sup"] >= 22.0
    assert tr["n"] > 0


def test_forward_prediction_moves_content_toward_ego():
    m = LaneMap(LW)
    m.update(bands=[_band(19.9, 20.1, 1.0)], dt_s=0.05)
    assert m.at(20.0).center == pytest.approx(1.0)
    # 20 m/s × 0.1 s = 2 m 前进：原 20 m 处的内容应到 18 m
    m.update(bands=[], v_mps=20.0, dt_s=0.1)
    assert m.at(18.0).center == pytest.approx(1.0)
    assert m.at(20.0) is None or m.at(20.0).center is None


def test_lateral_prediction_compensates_ego_shift():
    m = LaneMap(LW)
    m.update(bands=[_band(19.9, 20.1, 1.0)], dt_s=0.05)
    # 自车右移 1 道/s × 0.1 s → 同一路点在自车系左移 0.35 m
    m.update(bands=[], v_lat_lane_s=1.0, dt_s=0.1)
    assert m.at(20.0).center == pytest.approx(1.0 - 0.35, abs=1e-6)


def test_confidence_decays_and_nodes_expire():
    m = LaneMap(LW)
    m.update(bands=[_band(19.9, 20.1, 1.0)], dt_s=0.05)
    for _ in range(20):                  # 1 s 无证据
        m.update(bands=(), dt_s=0.05)
    n = m.at(20.0)
    assert n is not None and n.conf < 1.0
    for _ in range(40):                  # 再 2 s：超龄退役
        m.update(bands=(), dt_s=0.05)
    assert m.at(20.0) is None or m.at(20.0).center is None


def test_coarse_evidence_does_not_override_measured():
    m = LaneMap(LW)
    m.update(bands=[_band(19.9, 20.1, 1.0)], dt_s=0.05)
    m.update(bands=[], coarse_bands=[_band(18.0, 22.0, 3.0, src="inferred",
                                           conf=0.0)], dt_s=0.05)
    n = m.at(20.0)
    assert n.center == pytest.approx(1.0)
    assert n.src == "measured"


def test_coarse_fills_gap_with_capped_confidence():
    m = LaneMap(LW)
    m.update(bands=[], coarse_bands=[_band(38.0, 42.0, 2.0, src="inferred",
                                           conf=0.0)], dt_s=0.05)
    n = m.at(40.0)
    assert n is not None and n.center == pytest.approx(2.0)
    assert n.conf <= 0.3 and n.src == "inferred"


def test_prediction_error_samples_compare_before_correction():
    m = LaneMap(LW)
    m.update(bands=[_band(19.9, 20.1, 1.0)], dt_s=0.05)
    tr = m.update(bands=[_band(19.9, 20.1, 1.2)], dt_s=0.05)
    err = [e for e in tr["err"] if abs(e[0] - 20.0) < 1.5][0]
    assert err[1] == pytest.approx(-0.2, abs=1e-3)   # 图(1.0) − 实测(1.2)
    assert m.at(20.0).center == pytest.approx(1.2)   # 校正后采纳实测


def test_reset_clears_all_nodes():
    m = LaneMap(LW)
    m.update(bands=[_band(19.9, 20.1, 1.0)], dt_s=0.05)
    m.reset()
    assert m.at(20.0) is None and m.support() == 0.0


def _engine():
    from maaracing_master.plugins.speedrush import DECISION_FILE
    from maaracing_master.plugins.speedrush.config import _read_decision
    from maaracing_master.plugins.speedrush.decision import DecisionEngine
    return DecisionEngine(_read_decision(DECISION_FILE))


def _obs(fid):
    from maaracing_master.plugins.speedrush.tracking import (
        PerceptionHealth, WorldObservation)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, False, False),
        boundary=None, targets=(), far_targets=(), fid_rate_hz=20.0)


def _corridor():
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand)
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(-7.0, "measured", 1.0, 1),
                     BandBoundary(7.0, "measured", 1.0, 1))
        for i in range(9))
    return Corridor(bands, 0.0, 14.0 / LW, "pair", "ok")


def test_engine_exposes_map_trace_without_affecting_output():
    eng = _engine()
    out = eng.update(_obs(1), 0.05, executed_lane=0.0,
                     corridor=_corridor(), ego_speed_mps=20.0)
    tr = eng.lane_map_trace()
    assert tr is not None and tr["sup"] > 0.0
    assert any(p[4] == "measured" for p in tr["pts"] if p[1] is not None)
    # 观测面：图不接管控制，输出与决策层原有目标一致（空路回中）
    assert out.reason == "sel:center" and out.state.value == "CRUISE"


def test_engine_map_decays_without_corridor():
    eng = _engine()
    eng.update(_obs(1), 0.05, executed_lane=0.0,
               corridor=_corridor(), ego_speed_mps=0.0)
    before = eng.lane_map_trace()["sup"]
    for fid in range(2, 40):             # 无路 2 s：图按时间衰减/退役
        eng.update(_obs(fid), 0.05, executed_lane=0.0, corridor=None,
                   ego_speed_mps=0.0)
    after = eng.lane_map_trace()["sup"]
    assert before > 0.0 and after == 0.0


def test_coarse_only_does_not_count_as_support():
    m = LaneMap(LW)
    tr = m.update(bands=[], coarse_bands=[
        _band(20.0, 40.0, 1.0, src="inferred", conf=0.0)], dt_s=0.05)
    assert tr["sup"] == 0.0
    assert m.at(30.0).center == pytest.approx(1.0)
