# -*- coding: utf-8 -*-
"""走廊收益场闸（corridor_reach，2026-10-14 设计稿 §3.4）的回归锁。

锁面：shadow on/off 行为逐位（enabled 只加 reach_report，行为零变化）、
lane_scoring.control 常驻态下 shadow 同样零变化、回滚逐位（control=false=
legacy band/feas 链原样）、加载器互斥（control=true 时 lane_scoring.control
必须为 false）、FSM 契约（select:reach_win 进 CHANGE、moving:reach 滚动重评、
走廊缺席 hold:reach_hold 不臆造）。
场景数值以 decision.json 起值反推（死区 0.15 道、need=0.3+0.4|dx|道+0.28、
attribution 0.75 道×lane_w 3.38），改参数会红——参数与场景是契约面。
"""
from __future__ import annotations

import json
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
# 测试基线必须自持，不随部署漂）
CFG_OFF = replace(_BASE, corridor_reach=replace(_BASE.corridor_reach,
                                                enabled=False, control=False))
# 影子期：enabled=true、control=false（lane_scoring.control 常驻态原样）
CFG_SHADOW = replace(_BASE, corridor_reach=replace(_BASE.corridor_reach,
                                                   enabled=True))
# control 期：行为闸开（lane_scoring.control 归 false——互斥）
CFG_CTRL = replace(_BASE,
                   lane_scoring=replace(_BASE.lane_scoring, control=False),
                   corridor_reach=replace(_BASE.corridor_reach,
                                          enabled=True, control=True))


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
                    "ok", None)


def _BASE_cal_lane_w() -> float:
    from maaracing_master.plugins.speedrush.world_model import load_calib
    return load_calib().lane_w_m


def _out_fields(out):
    """DecisionOutput 行为面（逐位对照的字段集）。"""
    return (out.state, out.reason, out.x_target, out.target_id,
            out.move_allowed, out.reanchor_lane)


# ---------- shadow 逐位（enabled 只加影子列，行为零变化） ----------

def test_shadow_on_off_bit_identical():
    """enabled on/off 同输入逐拍输出逐位相同；on 侧 reach_report 可读、
    off 侧 None（影子列语义）。"""
    g, ms = _group()
    cor = _corridor()
    e_on = DecisionEngine(CFG_SHADOW)
    e_off = DecisionEngine(CFG_OFF)
    o_on = e_on.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                       corridor=cor)
    o_off = e_off.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                         corridor=cor)
    assert _out_fields(o_on) == _out_fields(o_off)
    assert e_on.reach_report is not None       # 影子评定可读
    assert e_on.reach_report.candidates        # 候选非空
    assert e_off.reach_report is None


def test_shadow_with_lane_control_bit_identical():
    """lane_scoring.control=true（部署常驻态）下 shadow 同样零变化：开/关
    两引擎同输入逐位相同（互斥只管 control，影子不并跑约束）。"""
    g, ms = _group()
    cor = _corridor()
    cfg_on = replace(CFG_SHADOW)
    cfg_off = CFG_OFF
    e_on = DecisionEngine(cfg_on)
    e_off = DecisionEngine(cfg_off)
    for fid in (1, 2):
        o_on = e_on.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                           executed_lane=0.0, corridor=cor)
        o_off = e_off.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                             executed_lane=0.0, corridor=cor)
        assert _out_fields(o_on) == _out_fields(o_off)


# ---------- 加载器互斥（两控制器不得并跑） ----------

def test_mutual_exclusion_loader_rejects():
    """corridor_reach.control=true 与 lane_scoring.control=true 并存 → 加载器
    拒绝（设计稿 §3.4 互斥）。"""
    d = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    d["corridor_reach"]["control"] = True      # lane_scoring.control 已是 true
    tmp = CFG_PATH.parent / "_test_mutual.json"
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="corridor_reach"):
            _read_decision(tmp)
    finally:
        tmp.unlink()


# ---------- control 行为闸（FSM 契约） ----------

def test_control_reach_win_enters_change():
    """走廊收益场胜出位离自车超死区 → select:reach_win 进 CHANGE，目标类
    reach（币组 1.5 道≈5.1m、t_miss≈1.57s 可达，胜出位 ≈1 道外——平手阈内
    中心偏好偷不走，胜出位在死区外）。"""
    g, ms = _group(x=1.5, rel=3)
    e = DecisionEngine(CFG_CTRL)
    out = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.state is DecisionState.CHANGE
    assert out.reason == "select:reach_win"
    assert e.target_kind == "reach"


def test_control_next_tick_moving_reach():
    """CHANGE 后报告在场：滚动重评 moving:reach（x_goal 每拍更新）。"""
    g, ms = _group(x=1.5, rel=3)
    e = DecisionEngine(CFG_CTRL)
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    out = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.state is DecisionState.CHANGE
    assert out.reason == "moving:reach"


def test_control_no_corridor_hold_reach():
    """走廊缺席（黑视拍）：hold:reach_hold 不臆造可达性（设计稿 §3.3 诚实弃权）。"""
    e = DecisionEngine(CFG_CTRL)
    out = e.update(_obs(), DT, executed_lane=0.0, corridor=None)
    assert out.state is DecisionState.CRUISE
    assert out.reason == "hold:reach_hold"


def test_control_off_keeps_legacy_select():
    """回滚红线：control=false 时 legacy band/feas 选择链原样——同一良候选
    场景走 select:score_win（金币实体目标），不是 reach_win。"""
    g, ms = _group()
    cfg_legacy = replace(CFG_OFF, lane_scoring=replace(_BASE.lane_scoring,
                                                       control=False))
    e = DecisionEngine(cfg_legacy)
    out = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.reason == "select:score_win"
    assert e.reach_report is None              # 闸关连影子列一起关
