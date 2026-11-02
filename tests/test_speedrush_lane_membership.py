# -*- coding: utf-8 -*-
"""车道线定道（双线同行插值）回归锁（2026-11-02 待办 3 phase 1）。

锁定：lane_membership 的插值与弃权口径（单线/不夹住/坏线一律 None）、
跟踪层闸开时 x_lane_line 随 TrackedTarget/FarTarget 契约面世、闸关=字段
恒 None 逐位回今天、对象表 trace 列随闸带值。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.lane_obs import lane_membership
from maaracing_master.plugins.speedrush.object_table import ObjectTable
from maaracing_master.plugins.speedrush.perception import Detection
from maaracing_master.plugins.speedrush.tracking import (
    FarTarget, Tracker, TrackedTarget)


def _line(x_at_ref: float, L: float, *, s: float = 0.0, ref_y: float = 500.0,
          rms: float = 1.0, theta: float = 0.05, accepted: bool = True,
          y0: float = 440.0, y1: float = 545.0, fits=None):
    """构造一条线：x_at(y) = s·y + a（a 由参考行像素位反推）。

    拟合范围 y0~y1 默认远段（实机 FAR_Y）；fits 覆盖时带近/远两段。"""
    if fits is None:
        fits = [{"s": s, "a": x_at_ref - s * ref_y, "y0": y0, "y1": y1}]
    return {"accepted": accepted, "L": L, "rms": rms, "theta": theta,
            "fits": fits}


# ---------- lane_membership 单元 ----------

def test_membership_interpolates_between_bracketing_lines():
    lines = [_line(400.0, -0.5), _line(1200.0, 0.5)]
    # 框心在两线正中 → 车道成员 0.0；1/4 处 → −0.25
    assert lane_membership(800.0, 500.0, lines) == pytest.approx(0.0)
    assert lane_membership(600.0, 500.0, lines) == pytest.approx(-0.25)


def test_membership_evaluates_lines_at_box_bottom_row():
    """线在框底行取像素位（斜率不同的会聚线对在 y_b 处求值，不是在参考行）。"""
    lines = [_line(350.0, -0.5, s=0.5), _line(1150.0, 0.5, s=0.6)]
    # y_b=500：x1=350, x2=1150；框心 750 → 正中
    assert lane_membership(750.0, 500.0, lines) == pytest.approx(0.0)
    # y_b=600 越出远段范围 55px（≤一倍跨度 105）：外推仍可用
    assert lane_membership(805.0, 600.0, lines) == pytest.approx(0.0, abs=0.01)


def test_membership_picks_fit_covering_box_bottom():
    """一条线带近/远两段拟合：吃覆盖框底行的那段（近段不外推）。"""
    pair = [
        _line(0.0, -0.5, fits=[
            {"s": 0.5, "a": 100.0, "y0": 560.0, "y1": 720.0},   # 近段
            {"s": 0.6, "a": 100.0, "y0": 440.0, "y1": 545.0}]), # 远段
        _line(0.0, 0.5, fits=[
            {"s": 0.5, "a": 900.0, "y0": 560.0, "y1": 720.0},
            {"s": 0.6, "a": 900.0, "y0": 440.0, "y1": 545.0}]),
    ]
    # y_b=620 近段覆盖：x1=410, x2=1210，正中 810 → 0.0
    # （若误用远段 x1=472/x2=1272，正中 872——810 会给 −0.078）
    assert lane_membership(810.0, 620.0, pair) == pytest.approx(0.0)
    # y_b=490 远段覆盖：x1=394, x2=1194，正中 794 → 0.0
    assert lane_membership(794.0, 490.0, pair) == pytest.approx(0.0)


def test_membership_abstains_beyond_one_fit_span():
    """框底行越出拟合范围一倍跨度：外推不可信，弃权。"""
    lines = [_line(400.0, -0.5), _line(1200.0, 0.5)]   # 远段 440~545
    assert lane_membership(800.0, 300.0, lines) is None


def test_membership_abstains_single_line_and_non_bracketing():
    lines = [_line(400.0, -0.5), _line(1200.0, 0.5)]
    assert lane_membership(800.0, 500.0, [lines[0]]) is None      # 单线弃权
    assert lane_membership(200.0, 500.0, lines) is None           # 两线左侧
    assert lane_membership(1300.0, 500.0, lines) is None          # 两线右侧


def test_membership_abstains_bad_lines():
    lines = [_line(400.0, -0.5), _line(1200.0, 0.5)]
    bad_rms = [_line(400.0, -0.5, rms=9.0), lines[1]]
    assert lane_membership(800.0, 500.0, bad_rms) is None         # 残差超限
    bad_theta = [_line(400.0, -0.5, theta=0.9), lines[1]]
    assert lane_membership(800.0, 500.0, bad_theta) is None       # 线角过分
    rejected = [_line(400.0, -0.5, accepted=False), lines[1]]
    assert lane_membership(800.0, 500.0, rejected) is None        # 未过门线
    old_fit = [{"accepted": True, "L": -0.5},                     # 旧拟合无 s/a
               {"accepted": True, "L": 0.5}]
    assert lane_membership(800.0, 500.0, old_fit) is None


def test_membership_abstains_degenerate_span():
    """两线近重合（像素间距过小）：插值发散，弃权。"""
    lines = [_line(795.0, -0.5), _line(800.0, 0.5)]
    assert lane_membership(797.0, 500.0, lines) is None


# ---------- 跟踪层接线（闸开/闸关） ----------

_LINES = [_line(400.0, -0.5), _line(1200.0, 0.5)]
_LANE = {"lines": _LINES, "usable": True}


def _far_car():
    """cy=370 在归一发散弃权区（denom<min_denom → x_lane=None）。"""
    return Detection(800, 370, 60, 40, 0.9)


def test_gate_on_fills_membership_on_far_target():
    trk = Tracker(lane_line_enabled=True)
    obs = trk.update(_per(1, cars=[_far_car()]), frame_age_ms=10.0, stage=1,
                     lane=_LANE)
    assert obs.targets == ()                      # x_lane=None → far
    far = obs.far_targets[0]
    assert far.x_lane_line == pytest.approx(0.0)  # 框心 800 → 两线正中


def test_gate_on_fills_membership_on_near_target():
    trk = Tracker(lane_line_enabled=True)
    obs = trk.update(_per(1, cars=[Detection(600, 500, 60, 40, 0.9)]),
                     frame_age_ms=10.0, stage=1, lane=_LANE)
    t = obs.targets[0]
    assert t.x_lane is not None                   # 域内照旧走 A1
    assert t.x_lane_line == pytest.approx(-0.25)  # 线插值独立面世


def test_gate_off_keeps_membership_none():
    """闸关=字段恒 None（逐位回今天）：lane 快照喂了也不算。"""
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_far_car(), Detection(600, 500, 60, 40, 0.9)]),
                     frame_age_ms=10.0, stage=1, lane=_LANE)
    assert all(f.x_lane_line is None for f in obs.far_targets)
    assert all(t.x_lane_line is None for t in obs.targets)


def test_no_lane_snapshot_keeps_membership_none():
    trk = Tracker(lane_line_enabled=True)
    obs = trk.update(_per(1, cars=[_far_car()]), frame_age_ms=10.0, stage=1)
    assert obs.far_targets[0].x_lane_line is None


def test_membership_flows_to_object_table_trace():
    """对象表 trace 第 14 列=车道线成员（闸开带值，闸关 None）。"""
    trk = Tracker(lane_line_enabled=True)
    obs = trk.update(_per(1, cars=[_far_car()]), frame_age_ms=10.0, stage=1,
                     lane=_LANE)
    tab = ObjectTable(safety_age_s=0.6, plan_age_s=5.0)
    tab.step(obs=obs, fps=20.0)
    row = tab.as_trace()["t"][0]
    assert len(row) == 14 and row[13] == pytest.approx(0.0)

    trk = Tracker()
    obs = trk.update(_per(1, cars=[_far_car()]), frame_age_ms=10.0, stage=1,
                     lane=_LANE)
    tab = ObjectTable(safety_age_s=0.6, plan_age_s=5.0)
    tab.step(obs=obs, fps=20.0)
    assert tab.as_trace()["t"][0][13] is None


# ---------- 契约：字段默认 None（旧构造方未供=消费端自决） ----------

def test_contract_fields_default_none():
    assert TrackedTarget(
        id=1, kind="car", x_lane=0.5, x_sigma=0.1, cy=500, w=60, h=40,
        conf=0.9, rel_approach=0.0, first_seen_fid=1, last_seen_fid=1,
        validity_until_fid=9).x_lane_line is None
    assert FarTarget(
        id=1, kind="car", lane_side=1, cy=370, conf=0.9, h=40,
        last_x_lane=None, first_seen_fid=1, last_seen_fid=1,
        validity_until_fid=9).x_lane_line is None


# ---------- 消费接线（phase 2，闸 lane_line.consume） ----------

def _far_target(*, oid=7, x_lane_line=1.0, first=1, last=20, rel=8.0):
    from maaracing_master.plugins.speedrush.world_model import KIND_CAR
    return FarTarget(id=oid, kind=KIND_CAR, lane_side=1, cy=400, conf=0.9,
                     h=30, last_x_lane=None, first_seen_fid=first,
                     last_seen_fid=last, validity_until_fid=99,
                     cx=0, rel_approach=rel, x_lane_line=x_lane_line)


def _engine(consume: bool):
    from dataclasses import replace
    from maaracing_master.plugins.speedrush.config import LaneLine
    from maaracing_master.plugins.speedrush.decision import DecisionEngine
    from maaracing_master.plugins.speedrush import DECISION_FILE
    from maaracing_master.plugins.speedrush.config import _read_decision
    cfg = replace(_read_decision(DECISION_FILE),
                  lane_line=LaneLine(enabled=True, consume=consume))
    e = DecisionEngine(cfg)
    e._views = ()                      # 直调车源链：无车流视图
    return e


def test_lane_line_far_car_enters_car_sources():
    """消费闸开：far 轨线成员顶上（第三循环，src=line），到站走地面流同一
    仪器链。安全沿用现有门（far 条目无横位 → safety_ok=False），语义另行设计。"""
    e = _engine(consume=True)
    e._fid = 20
    src = e._reach_car_sources(0.0, (_far_target(),))
    assert len(src) == 1
    s = src[0]
    assert s.src == "line"
    assert s.x_m == pytest.approx(1.0 * e.cal.lane_w_m)
    assert s.t_meet_s is not None and s.t_meet_s > 0.0
    assert s.safety_ok is False


def test_lane_line_consume_gate_off_keeps_far_out():
    """闸关=逐位回今天：far 轨不进车源（字段有值也不消费）。"""
    e = _engine(consume=False)
    e._fid = 20
    assert e._reach_car_sources(0.0, (_far_target(),)) == []


def test_lane_line_far_car_gates_young_and_horizon():
    """同近场源的年龄门槛与不接近不收；地平线上几何无效不入账。"""
    e = _engine(consume=True)
    e._fid = 20
    assert e._reach_car_sources(0.0, (_far_target(first=18),)) == []  # 年龄不足
    assert e._reach_car_sources(0.0, (_far_target(rel=0.0),)) == []   # 不接近
    from maaracing_master.plugins.speedrush.world_model import KIND_CAR
    horizon = FarTarget(id=9, kind=KIND_CAR, lane_side=1, cy=350, conf=0.9,
                        h=30, last_x_lane=None, first_seen_fid=1,
                        last_seen_fid=20, validity_until_fid=99,
                        cx=0, rel_approach=8.0, x_lane_line=1.0)
    assert e._reach_car_sources(0.0, (horizon,)) == []               # 地平线上


def _per(fid: int, *, cars=()):
    from maaracing_master.plugins.speedrush.perception import PerceptionResult
    return PerceptionResult(frame_id=fid, ts_ns=fid * 33_000_000,
                            cars=list(cars), coins=[], bonuses=[])
