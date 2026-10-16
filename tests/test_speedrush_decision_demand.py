# -*- coding: utf-8 -*-
"""需求面承诺制（§8，commitment.demand/memory 闸）的回归锁。

锁面 = §8.4 四条转移各一例 + §8.3 完成判据对齐 d0（fid2348 形态拦截）+
重锚变换/断裂 + 缺席记忆预算出口 + demand-off 基线对照（坍缩假完成是
旧行为的实证，锁它防止 off 路径语义漂移）。场景数值：dead_zone 0.15 道、
LANE_HALF 0.75 道、basis_grace 0.5s、k_* 2、tie_margin 20 分、币值
30×max(n,3)×conf——改参数会红，参数与场景是契约面。
"""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH
from maaracing_master.plugins.speedrush.config import (
    Commitment, _read_decision)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.corridor_reach import (
    LANE_HALF as LANE_HALF_TEST)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)

DT = 0.05
_BASE = _read_decision(CFG_PATH)
_C = _BASE.commitment
# 闸态显式钉死（不继承部署态）：demand+memory 开=被测面；demand 单开=memory
# 归因面；全关=off 基线对照。trajectory_sampling 钉开=生产链路（trace 实证
# traj 在跑）：滚动 _traj_last_dt 正是坍缩假完成的载体，off 基线必须走同链路
_TM = replace(_BASE.mode, trajectory_sampling=True)
CFG_DM = replace(_BASE, mode=_TM,
                 corridor_reach=replace(_BASE.corridor_reach,
                                        enabled=True, control=True),
                 commitment=replace(_C, demand_enabled=True,
                                    memory_enabled=True))
CFG_D = replace(CFG_DM, commitment=replace(_C, demand_enabled=True,
                                           memory_enabled=False))
CFG_OFF = replace(CFG_DM, commitment=Commitment())


def _obs(*, fid=1, groups=(), targets=(), presence=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(True, True, True, presence, False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=None)


def _group(gid=1, fid=1, n=3, x=0.8, cy=500, rel=5, conf=0.85):
    """良候选币组（corridor_reach 闸测试同款）：t_miss≤窗、conf 满权；
    conf<conf_floor(0.5) 时场评分归 None——「带内可见但场不值钱」的
    fid2348 造数面。"""
    ms = [TrackedTarget(id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i,
                        x_sigma=0.1, cy=cy + i * 3, w=20, h=20, conf=conf,
                        rel_approach=rel, first_seen_fid=fid,
                        last_seen_fid=fid, validity_until_fid=fid + 40)
          for i in range(n)]
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid,
                  validity_until_fid=fid + 40)
    return g, tuple(ms)


def _corridor(center=0.0):
    """双侧有证据的空气墙走廊（corridor_reach 闸测试同款；center 可注入
    重锚信号跳变）。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, CAM_Z_DATUM_M, Corridor, CorridorBand)
    from maaracing_master.plugins.speedrush.depth_geo import GRID_Z_LO
    from maaracing_master.plugins.speedrush.wall_track import MEASURED
    bands = tuple(
        CorridorBand(GRID_Z_LO + i * 3.0 - CAM_Z_DATUM_M,
                     GRID_Z_LO + (i + 1) * 3.0 - CAM_Z_DATUM_M,
                     BandBoundary(-4.2, MEASURED, 1.0),
                     BandBoundary(4.2, MEASURED, 1.0))
        for i in range(9))
    return Corridor(bands, center, 8.4 / _BASE_cal_lane_w(), "pair",
                    "ok", None)


def _BASE_cal_lane_w() -> float:
    from maaracing_master.plugins.speedrush.world_model import load_calib
    return load_calib().lane_w_m


def _run(e, obs, dt=DT, **kw):
    """生产链路包装：road_offset=−corridor.center_lane（module 同式）——轨迹
    分支要求 ro 在场，缺它整条 traj 链静默降级，假完成载体（_traj_last_dt）
    就不是生产语义。"""
    cor = kw.pop("corridor", None)
    return e.update(obs, dt,
                    executed_lane=kw.pop("executed_lane", 0.0),
                    road_offset=None if cor is None else -cor.center_lane,
                    corridor=cor, **kw)


# ---------- select 意图锁定（§8.2） ----------

def test_demand_select_locks_intent():
    """select:reach_win 拍锁定意图：d0=本场选点、basis=带内币组、计数归零；
    demand 关=无意图（观测面 None，行为走旧判据）。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    out = _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
                   corridor=_corridor())
    assert out.reason == "select:reach_win"
    it = e.intent_state
    assert it is not None
    assert it["d0"] == pytest.approx(
        e.reach_report.best_x_m / _BASE_cal_lane_w(), abs=1e-3)  # =select 场选点
    assert it["basis_ids"] == [1]                       # 带内审计起点
    assert it["basis"] == "alive" and it["absence_s"] == 0.0
    assert it["reanchors"] == 0
    e2 = DecisionEngine(CFG_OFF)
    _run(e2,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
              corridor=_corridor())
    assert e2.intent_state is None


# ---------- 完成判据对齐 d0（§8.3，fid2348 拦截主锁） ----------

def test_demand_completion_aligns_d0_not_rolling_pick():
    """坍缩假到达拦截：币组仍在带内可见但场不值钱（conf 地板）→ 场选点塌回
    自车，滚动判据会宣「到达」——d0 判据不认（位移远未吃掉需求，主判据不
    成立；次判据被近旁币要求+守门双拦）。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    d0 = e.intent_state["d0"]
    dead = _group(gid=1, x=1.5, rel=3, conf=0.3)   # 可见但不入场分
    for fid in (2, 3, 4):
        out = _run(e,_obs(fid=fid, groups=(dead[0],), targets=dead[1]),
                       DT, executed_lane=0.0, corridor=_corridor())
        assert out.state is DecisionState.CHANGE
        assert out.reason == "moving:reach"        # 不假完成、不持位（带内活）
    it = e.intent_state
    assert it["d0"] == pytest.approx(d0)           # 意图未被滚动选点侵蚀
    assert it["basis"] == "alive"                  # 币组仍在带内=依据活着


def test_demand_off_collapse_false_arrival_baseline():
    """off 基线对照：同坍缩形态下旧判据（滚动 _traj_last_dt）第三拍即宣
    done——这是 demand 闸要治的病灶实证，锁它防 off 路径语义漂移。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_OFF)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    dead = _group(gid=1, x=1.5, rel=3, conf=0.3)
    out = _run(e,_obs(fid=2, groups=(dead[0],), targets=dead[1]), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "moving:reach"            # 第一拍 _traj_last_dt 尚未塌
    out = _run(e,_obs(fid=3, groups=(dead[0],), targets=dead[1]), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "done:converged:feedback"  # 假到达（旧行为）


def test_demand_secondary_converge_needs_near_coin():
    """次判据（场说「留在这」）：贴车 k_stay 序列 + 自车近旁有币组 →
    done:converged:proxy（band 空窗期守门不拦——原带已无依据）。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
              corridor=_corridor())
    near, ms2 = _group(gid=2, x=0.1, rel=5)
    out = _run(e,_obs(fid=2, groups=(near,), targets=ms2), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "hold:reach_gap"          # 原带已空：贴车第 1 拍+持位
    out = _run(e,_obs(fid=3, groups=(near,), targets=ms2), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "done:converged:proxy"    # 第 2 拍达标（k_stay=2）
    assert out.state is DecisionState.CRUISE


def test_demand_secondary_guard_blocks_fid2348_shape():
    """守门：场选点贴车、自车近旁也有币，但原带依据仍活且场选点已离带 →
    不是留驻是坍缩，次判据不得放行（fid2348 的近旁币变体）。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    dead = _group(gid=1, x=1.5, rel=3, conf=0.3)   # 原带内可见（依据活）
    near, ms2 = _group(gid=2, x=0.1, rel=5)        # 自车近旁有币、场选点贴车
    for fid in (2, 3, 4):
        out = _run(e,_obs(fid=fid, groups=(dead[0], near), targets=dead[1] + ms2),
                       DT, executed_lane=0.0, corridor=_corridor())
        assert out.reason == "moving:reach"


# ---------- 报告缺席记忆（§8.4.4，Track 2 并入） ----------

def test_demand_absence_memory_holds_then_recovers():
    """缺席预算内：保留意图、x_target 冻结 executed（reason=hold:reach_absent）；
    恢复拍清零 absence_s、意图不动。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    d0 = e.intent_state["d0"]
    for fid in (2, 3):
        out = _run(e,_obs(fid=fid), DT, executed_lane=0.2, corridor=None)
        assert out.reason == "hold:reach_absent"
        assert out.x_target == pytest.approx(0.2)  # 冻结位=executed
        assert e.intent_state["d0"] == pytest.approx(d0)
        assert e.intent_state["absence_s"] == pytest.approx((fid - 1) * DT)
    out = _run(e,_obs(fid=4, groups=(g,), targets=ms), DT,
                   executed_lane=0.2, corridor=_corridor())
    assert out.reason == "moving:reach"
    assert e.intent_state["absence_s"] == 0.0      # 恢复拍清零
    assert e.intent_state["d0"] == pytest.approx(d0)   # 意图不动


def test_demand_absence_exceeds_budget_cancels():
    """缺席超 memory_budget_s → 既有取消（reason 仍 cancel:reach_lost，
    与瞬断的区分靠 absence 列 ≈ 预算）。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    reason = None
    for fid in range(2, 14):
        out = _run(e,_obs(fid=fid), DT, executed_lane=0.2, corridor=None)
        reason = out.reason
        if out.state is not DecisionState.CHANGE:
            break
    assert reason == "cancel:reach_lost"
    assert out.state is DecisionState.ABORT_CHANGE
    assert e.intent_state["absence_s"] == pytest.approx(0.55)


def test_demand_absence_without_receipt_cancels_immediately():
    """回执缺席=诚实降级（不冻结无可信前提的位）：首个缺席拍即既有取消。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    out = _run(e,_obs(fid=2), DT, executed_lane=None, corridor=None)
    assert out.reason == "cancel:reach_lost"
    assert out.state is DecisionState.ABORT_CHANGE


def test_demand_absence_memory_off_single_tick_cancel():
    """memory 关（demand 单开）：缺席照旧单拍取消——两旋钮独立归因。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_D)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    out = _run(e,_obs(fid=2), DT, executed_lane=0.2, corridor=None)
    assert out.reason == "cancel:reach_lost"


# ---------- 收益依据死亡（§8.4.2，边界 B） ----------

def test_demand_basis_dead_after_grace_and_pick_out():
    """带内空窗满 basis_grace_s 且场选点连续带外 k_out 拍 → cancel:basis_dead；
    空窗未满期间持位等下一组（hold:reach_gap、x_target=executed）。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
              corridor=_corridor())
    far, ms2 = _group(gid=9, x=-1.5, rel=5)   # 带外诱饵：撑 presence、场选点离带
    reasons = []
    out = None
    for fid in range(2, 14):
        out = _run(e,_obs(fid=fid, groups=(far,), targets=ms2), DT,
                       executed_lane=0.0, corridor=_corridor())
        reasons.append(out.reason)
        if out.reason == "cancel:basis_dead":
            break
    assert reasons[0] == "hold:reach_gap"
    assert set(reasons[:-1]) == {"hold:reach_gap"}      # 死亡前全在持位
    assert reasons[-1] == "cancel:basis_dead"
    assert out.state is DecisionState.ABORT_CHANGE
    # 死亡拍=空窗满 grace（0.5s≈10-11 拍，浮点累计边界）且带外拍早已 ≥2
    assert 10 <= len(reasons) <= 12
    assert e.intent_state["basis"] == "gap"


def test_demand_basis_gap_recovers_alive():
    """空窗未满 grace 即恢复带内币组 → 计数归零回 alive（短暂看不到 ≠
    不存在），持位结束恢复移动。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    far, ms2 = _group(gid=9, x=-1.5, rel=5)
    for fid in (2, 3):
        out = _run(e,_obs(fid=fid, groups=(far,), targets=ms2), DT,
                       executed_lane=0.0, corridor=_corridor())
        assert out.reason == "hold:reach_gap"
    out = _run(e,_obs(fid=4, groups=(g,), targets=ms), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "moving:reach"
    assert e.intent_state["basis"] == "alive"
    assert e.intent_state["band_empty_s"] == 0.0


# ---------- 目标升级（§8.4.3） ----------

def test_demand_upgrade_replaces_intent():
    """带外更优目的地连续 k_up 拍（同拍同场分差 ≥ tie_margin）→
    switch:reach_upgrade 整体替换意图；单拍不够不升级。"""
    g, ms = _group(gid=1, x=1.5, rel=3)                 # 在场意图带（n=3）
    big, big_ms = _group(gid=2, x=-1.5, rel=3, n=8)     # 带外更优（n=8）
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    d0 = e.intent_state["d0"]
    out = _run(e,_obs(fid=2, groups=(g, big), targets=ms + big_ms), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "moving:reach"            # 第 1 拍只计数
    assert e.intent_state["up"] == 1
    out = _run(e,_obs(fid=3, groups=(g, big), targets=ms + big_ms), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "switch:reach_upgrade"    # 第 2 拍达标（k_up=2）
    it = e.intent_state
    assert it["d0"] != pytest.approx(d0)           # d0 整体替换
    assert it["d0"] == pytest.approx(
        e.reach_report.best_x_m / _BASE_cal_lane_w(), abs=1e-3)
    assert it["basis_ids"] == [2]                  # 新带内依据
    assert it["up"] == 0 and it["reanchors"] == 0  # 新承诺计数清零
    out = _run(e,_obs(fid=4, groups=(g, big), targets=ms + big_ms), DT,
                   executed_lane=0.0, corridor=_corridor())
    assert out.reason == "moving:reach"            # 升级后不连环翻面


# ---------- 坐标系重锚（§8.3，边界 A） ----------

def test_demand_reanchor_transform_then_frame_break():
    """center_lane 跳变 eps~break 间 → d0'=d0−Δc；回执与币组同帧平移
    （生产里 executed/观测随 road_offset 重表达）后相对需求不变——reason 仍
    moving、币仍在带内；超 break 判坐标系断裂 → cancel:frame_break。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    d0 = e.intent_state["d0"]
    # 同一物理世界在新坐标系（center +0.3）下的重表达：币 1.5−0.3、车 0−0.3
    g2, ms2 = _group(gid=1, x=1.2, rel=3)
    out = _run(e,_obs(fid=2, groups=(g2,), targets=ms2), DT,
                   executed_lane=-0.3, corridor=_corridor(center=0.3))
    assert out.reason == "moving:reach"
    assert e.intent_state["d0"] == pytest.approx(d0 - 0.3, abs=1e-3)
    # 相对需求保持：|executed − d0'| == |0 − d0|（物理指向不变的判定面）
    assert abs(e.intent_state["d0"] - (-0.3)) == pytest.approx(abs(d0), abs=1e-3)
    assert e.intent_state["reanchors"] == 1
    g3, ms3 = _group(gid=1, x=0.5, rel=3)
    out = _run(e,_obs(fid=3, groups=(g3,), targets=ms3), DT,
                   executed_lane=-1.0, corridor=_corridor(center=1.0))
    assert out.reason == "cancel:frame_break"      # Δ=0.7 > break 0.5
    assert out.state is DecisionState.ABORT_CHANGE


def test_demand_reanchor_small_jumps_ignored():
    """跳变 ≤ reanchor_eps 视为同坐标系噪声：不换版不变换。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e,_obs(groups=(g,), targets=ms), DT, executed_lane=0.0,
             corridor=_corridor())
    d0 = e.intent_state["d0"]
    out = _run(e,_obs(fid=2, groups=(g,), targets=ms), DT,
                   executed_lane=0.0, corridor=_corridor(center=0.15))
    assert out.reason == "moving:reach"
    assert e.intent_state["d0"] == pytest.approx(d0)
    assert e.intent_state["reanchors"] == 0


# ---------- 20261016 A/B 收案修复锁（P1 时间线诊断） ----------

def _corridor_blind(center=0.0):
    """center 在场、全带缺证据的走廊（黑视形态）：evaluate 诚实弃权 None
    （rep 缺席）但 center_lane 仍供数——缺席期重锚跟踪的信号源。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, CAM_Z_DATUM_M, Corridor, CorridorBand)
    from maaracing_master.plugins.speedrush.depth_geo import GRID_Z_LO
    from maaracing_master.plugins.speedrush.wall_track import MEASURED
    bands = tuple(
        CorridorBand(GRID_Z_LO + i * 3.0 - CAM_Z_DATUM_M,
                     GRID_Z_LO + (i + 1) * 3.0 - CAM_Z_DATUM_M,
                     BandBoundary(None, MEASURED, 1.0),
                     BandBoundary(None, MEASURED, 1.0))
        for i in range(9))
    return Corridor(bands, center, 8.4 / _BASE_cal_lane_w(), "pair",
                    "ok", None)


def test_demand_timeout_exits_gap_hold():
    """超时出口（20261016 A/B 缺口修复）：t_change_max_s 原本只查 executed
    缺席的降级分支，意图生命周期拉长集后持位无期（P1 实测 gap 持位
    25.1s/30s）——executed 在场的集同享超时强制落位。构造：车在 −0.5 道、
    币在 1.2 道（归属半宽罩不到车旁候选、t_miss 够变道耗时）→ 锁 d0≈路心；
    空场后场选点塌回车旁（带内，pick_out 恒 0，dead 门盲区）→ 持位到 2s
    被超时收场。"""
    g, ms = _group(gid=1, x=1.2, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e, _obs(groups=(g,), targets=ms), executed_lane=-0.5,
         corridor=_corridor())
    d0 = e.intent_state["d0"]
    assert abs(d0) < LANE_HALF_TEST                # 带含塌回点（dead 门盲区）
    # 带外远币（cy 大到 t_miss 无穷：位置在场撑 presence、进不了场分）
    decoy, dms = _group(gid=9, x=2.5, cy=1500, rel=1)
    reason = None
    for fid in range(2, 60):
        out = _run(e, _obs(fid=fid, groups=(decoy,), targets=dms),
                   executed_lane=-0.5, corridor=_corridor())
        reason = out.reason
        if out.state is not DecisionState.CHANGE:
            break
    assert reason == "done:change_timeout"
    assert out.state is DecisionState.CRUISE


def test_demand_reanchor_tracked_during_absence():
    """缺席期重锚跟踪（20261016 A/B 修复）：收益报告缺席 ≠ 坐标系停表——
    P1 实测 frame_break 7 次中 4 次发生在缺席恢复拍（跨基线一次性比对）。
    缺席期间 center 跳变照常变换/判裂，恢复拍不误判。"""
    g, ms = _group(gid=1, x=1.5, rel=3)
    e = DecisionEngine(CFG_DM)
    _run(e, _obs(groups=(g,), targets=ms), corridor=_corridor())
    d0 = e.intent_state["d0"]
    out = _run(e, _obs(fid=2), executed_lane=0.2,
               corridor=_corridor_blind(center=0.3))
    assert out.reason == "hold:reach_absent"       # eps~break 间：照常重锚
    assert e.intent_state["d0"] == pytest.approx(d0 - 0.3, abs=1e-3)
    assert e.intent_state["reanchors"] == 1
    g2, ms2 = _group(gid=1, x=1.2, rel=3)          # 新帧下的同一物理世界
    out = _run(e, _obs(fid=3, groups=(g2,), targets=ms2), executed_lane=-0.3,
               corridor=_corridor(center=0.3))
    assert out.reason == "moving:reach"            # 恢复拍不误判断裂
    e2 = DecisionEngine(CFG_DM)
    _run(e2, _obs(groups=(g,), targets=ms), corridor=_corridor())
    out = _run(e2, _obs(fid=2), executed_lane=0.2,
               corridor=_corridor_blind(center=1.0))
    assert out.reason == "cancel:frame_break"      # 缺席期跳超 break：直接判裂
    assert out.state is DecisionState.ABORT_CHANGE


# ---------- 加载器 ----------

def test_demand_config_roundtrip(tmp_path):
    """部署 json：demand/memory 开、k/预算/重锚面齐；缺段安全（老 json）=
    全默认=关。"""
    c = _read_decision(CFG_PATH).commitment
    assert c.demand_enabled and c.memory_enabled
    assert (c.memory_budget_s, c.basis_grace_s) == (0.5, 0.5)
    assert (c.reanchor_eps_lane, c.reanchor_break_lane) == (0.2, 0.5)
    assert (c.k_stay, c.k_out, c.k_up) == (2, 2, 2)
    d = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    d.pop("commitment", None)
    old = tmp_path / "decision_old.json"
    old.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    c2 = _read_decision(old).commitment
    assert not c2.demand_enabled and not c2.memory_enabled
    assert not c2.pause_enabled    # 缺段=整段全默认=关（Track 1 同款）
