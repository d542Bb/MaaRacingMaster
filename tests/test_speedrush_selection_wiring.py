# -*- coding: utf-8 -*-
"""四层选择器接线回归锁（DecisionEngine + Corridor，2026-10-18 走廊重建）。

锁定：layer 开关生效后，输出目标与层一一对应（不再有加性场/轨迹否决），
中心带内不修正、带外只去带边。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.corridor import (
    BandBoundary, Corridor, CorridorBand)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, PerceptionHealth, TrackedTarget, WorldObservation)

LW = 3.38
_CFG_BASE = _read_decision(DECISION_FILE)
CFG = _CFG_BASE


def _corridor(left=-6.76, right=6.76, *, executed=0.0):
    """走廊带在自车系；路心随 executed 平移（与 road_offset 契约一致）。"""
    c = -executed * LW
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(c + left, "measured"),
                     BandBoundary(c + right, "measured"))
        for i in range(9))
    return Corridor(bands, -executed, (right - left) / LW, "pair", "ok")


def _obs(fid, *, groups=(), targets=(), presence=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, presence, False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=20.0)


def _group(fid, gid=1, n=3, x=0.4, cy=470, rel=2.5, conf=0.85):
    members = tuple(TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n))
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in members),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy,
                  cy_max=cy + 3 * (n - 1), conf_min=conf,
                  partial_observation=False, first_seen_fid=fid,
                  last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, members


def test_layered_coin_targets_coin_line():
    eng = DecisionEngine(CFG)
    g, ms = _group(1)
    out = None
    for fid in (1, 2, 3):
        out = eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                         executed_lane=0.0, corridor=_corridor())
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
    assert out is not None
    assert out.reason == "sel:coin"
    assert out.maneuver is True
    assert eng.selection_report is not None
    assert eng.selection_report.layer == 3
    # 币组心 0.4 道 → 选择目标约 0.4 道（币线，不是路心 0）；输出另有 slew
    assert eng.selection_report.target_x_m / LW == pytest.approx(0.4, abs=0.1)


def test_layered_reward_wins_over_coin():
    eng = DecisionEngine(CFG)
    g, ms = _group(1)
    bonus = TrackedTarget(
        id=900, kind="bonus", x_lane=0.1, x_sigma=0.1, cy=430, w=30, h=30,
        conf=0.99, rel_approach=2.5, first_seen_fid=1, last_seen_fid=4,
        validity_until_fid=20)
    out = None
    for fid in (1, 2, 3, 4):
        out = eng.update(_obs(fid, groups=(g,), targets=(bonus,)),
                         0.05, executed_lane=0.0, corridor=_corridor())
    assert out is not None
    assert out.reason == "sel:reward"
    assert eng.selection_report.layer == 1
    assert eng.selection_report.target_x_m / LW == pytest.approx(0.1, abs=0.1)


def test_layered_center_band_holds_current_lane():
    eng = DecisionEngine(CFG)
    out = eng.update(_obs(1), 0.05, executed_lane=0.3,
                     corridor=_corridor(executed=0.3))
    assert out.reason == "sel:center"
    assert eng.selection_report.layer == 4
    assert out.x_target == pytest.approx(0.3, abs=0.02)   # 带内不修正
    assert out.maneuver is False


def test_layered_center_outside_band_targets_edge_not_center():
    eng = DecisionEngine(CFG)
    out = eng.update(_obs(1), 0.05, executed_lane=1.5,
                     corridor=_corridor(executed=1.5))
    assert out.reason == "sel:center"
    assert out.x_target == pytest.approx(0.3, abs=0.02)   # 近侧带边
    assert out.maneuver is True


def test_layered_no_corridor_holds():
    eng = DecisionEngine(CFG)
    out = eng.update(_obs(1), 0.05, executed_lane=0.4, corridor=None)
    assert out.reason == "sel:hold:no_road"
    assert out.x_target == pytest.approx(0.4, abs=0.02)
