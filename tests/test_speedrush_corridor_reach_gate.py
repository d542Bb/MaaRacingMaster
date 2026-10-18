# -*- coding: utf-8 -*-
"""走廊收益场闸（corridor_reach，2026-10-14 设计稿 §3.4）的回归锁。

锁面：shadow on/off 行为逐位（enabled 只加 reach_report，行为零变化）、
回滚逐位（control=false=legacy band/feas 链原样）、FSM 契约（select:reach_win
进 CHANGE、moving:reach 滚动重评、走廊缺席 hold:reach_hold 不臆造）。
（lane_scoring 互斥锁随 lane_scoring 退役删除 20261015，两控制器前提不存在。）
场景数值以 decision.json 起值反推（死区 0.15 道、need=0.3+0.4|dx|道+0.28、
attribution 0.75 道×lane_w 3.38），改参数会红——参数与场景是契约面。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)

DT = 0.05
_BASE = _read_decision(CFG_PATH)
# 闸状态显式钉死（不继承部署态——部署 json 在 rollout 中会翻转 shadow→control，
# 测试基线必须自持，不随部署漂）。commitment 同钉关：§8 需求面在专门文件测，
# 本文件锁的是 reach 场本身的 FSM 契约。
CFG_OFF = replace(_BASE, corridor_reach=replace(_BASE.corridor_reach,
                                                enabled=False))
# 影子期：enabled=true、control=false（闸态全部钉死不继承部署态）
CFG_SHADOW = replace(_BASE,
                     corridor_reach=replace(_BASE.corridor_reach,
                                            enabled=True))
# control 期：行为闸开（lane_scoring 已退役删除，互斥检查随迁）
CFG_CTRL = replace(_BASE,
                   corridor_reach=replace(_BASE.corridor_reach,
                                          enabled=True))


def _obs(*, fid=1, groups=(), targets=(), presence=None, fid_hz=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(True, True, True, presence, False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=fid_hz)


def _group(gid=1, fid=1, n=3, x=0.8, cy=500, rel=5, conf=0.85):
    """良候选币组（decision 测试同款场景）：t_miss≈0.93s ≤ 窗，conf 满权。"""
    ms = [TrackedTarget(id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i,
                        x_sigma=0.1, cy=cy + i * 3, w=20, h=20, conf=conf,
                        rel_approach=rel, first_seen_fid=fid,
                        last_seen_fid=fid, validity_until_fid=fid + 8)
          for i in range(n)]
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid,
                  validity_until_fid=fid + 8)
    return g, tuple(ms)


def _corridor(left=-4.2, right=4.2):
    """双侧有证据的空气墙走廊（9 带 3~30m，车头系带标）：reach 域即全宽。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, CAM_Z_DATUM_M, Corridor, CorridorBand)
    from maaracing_master.plugins.speedrush.depth_geo import GRID_Z_LO
    from maaracing_master.plugins.speedrush.wall_track import MEASURED
    bands = tuple(
        CorridorBand(GRID_Z_LO + i * 3.0 - CAM_Z_DATUM_M,
                     GRID_Z_LO + (i + 1) * 3.0 - CAM_Z_DATUM_M,
                     BandBoundary(left, MEASURED, 1.0),
                     BandBoundary(right, MEASURED, 1.0))
        for i in range(9))
    return Corridor(bands, 0.0, (right - left) / _BASE_cal_lane_w(), "pair",
                    "ok")


def _BASE_cal_lane_w() -> float:
    from maaracing_master.plugins.speedrush.world_model import load_calib
    return load_calib().lane_w_m


def _out_fields(out):
    """DecisionOutput 行为面（逐位对照的字段集）。"""
    return (out.state, out.reason, out.x_target, out.move_allowed)


# ---------- shadow 逐位（enabled 只加影子列，行为零变化） ----------

def test_enabled_on_follows_field_off_holds():
    """enabled on=场即选择器（follow:reach，报告可读）；off=场评估关，
    场跟随诚实持守（hold:reach_absent，reach_report None）。"""
    g, ms = _group()
    cor = _corridor()
    e_on = DecisionEngine(CFG_SHADOW)
    e_off = DecisionEngine(CFG_OFF)
    o_on = e_on.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                       corridor=cor)
    o_off = e_off.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                         corridor=cor)
    assert o_on.reason == "follow:reach"
    assert e_on.reach_report is not None       # 场评定可读
    assert e_on.reach_report.candidates        # 候选非空
    assert o_off.reason == "hold:reach_absent"
    assert e_off.reach_report is None


# ---------- 加载器 ----------

# （互斥锁测试随 lane_scoring 退役删除 20261015：两控制器并存前提不存在）


# ---------- control 行为闸（FSM 契约） ----------

def test_control_follow_sets_goal():
    """报告在场 → follow:reach，目标=胜出位每拍滚动（2026-10-18 换血：场即
    选择器，无承诺/无 CHANGE——承诺制与目标锁定退役，稳定化=场滚动+采样器）。"""
    g, ms = _group(x=1.5, rel=3)
    e = DecisionEngine(CFG_CTRL)
    out = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.state is DecisionState.CRUISE
    assert out.reason == "follow:reach"
    assert e._reach_goal is not None
    out = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.reason == "follow:reach"
    assert e._reach_goal is not None


def test_control_absent_freezes_then_pauses():
    """走廊缺席：预算内冻结上一拍场选点（不塌回 0，防段状缺席期目标振荡），
    超预算冻结自车当前位（pause:reach_absent）直到报告恢复。"""
    g, ms = _group(x=1.5, rel=3)
    e = DecisionEngine(CFG_CTRL)
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.5,
             corridor=_corridor())
    goal = e._reach_goal
    out = e.update(_obs(fid=2), DT, executed_lane=0.5, corridor=None)
    assert out.reason == "hold:reach_absent"
    assert e._reach_goal == goal                 # 冻结上一拍选点，不塌回 0
    for fid in range(3, 13):                     # 默认预算 0.5s：11 拍=0.55s 超预算
        out = e.update(_obs(fid=fid), DT, executed_lane=0.5, corridor=None)
    assert out.reason == "pause:reach_absent"
    assert out.x_target == pytest.approx(0.5)    # 冻结自车当前位
    out = e.update(_obs(fid=13, groups=(g,), targets=ms), DT, executed_lane=0.5,
                   corridor=_corridor())
    assert out.reason == "follow:reach"          # 报告恢复：清零重计回跟随
    assert e._absence_t == 0.0


def test_enabled_off_field_off_hold():
    """闸关：场评估关（reach_report None），场跟随诚实持守不臆造可达性。"""
    g, ms = _group()
    e = DecisionEngine(CFG_OFF)
    out = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.reason == "hold:reach_absent"
    assert e.reach_report is None
