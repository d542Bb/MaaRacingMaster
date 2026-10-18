# -*- coding: utf-8 -*-
"""轨迹模式接线锁（阶段二 §2.3）：配置闸、输出形成换血、破判 ABORT、诚实降级。

兼容红线：闸关（默认）时新入参不改变任何输出（逐拍相同）；闸开时 FSM/校验/
选择保留，只换「计划」的形态与输出点。d 坐标系=路心系（道0=路心）。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, DistanceSource, MetricCarState,
    PerceptionHealth, TrackedTarget, WorldObservation)

DT = 0.05
# 本文件锁的是轨迹模式接线语义：关闸基座自钉，不随部署 decision.json 的
# 运行态漂（验收期部署文件开闸，语义测试不跟着翻）。
_CFG_BASE = _read_decision(DECISION_FILE)
CFG = replace(_CFG_BASE, mode=replace(_CFG_BASE.mode, trajectory_sampling=False),
              grid_veto=replace(_CFG_BASE.grid_veto, enabled=False),
              metric_cars=replace(_CFG_BASE.metric_cars, fusion=False))
CFG_TRAJ = replace(CFG, mode=replace(CFG.mode, trajectory_sampling=True))


def _obs(*, fid=1, groups=(), targets=(), presence=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(True, True, True, presence, False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups)


def _group(gid, fid, x, cy=500, rel=5.0, conf=0.85, n=3):
    """过门场景沿用 test_speedrush_decision.GOOD 的算式（n=3、cy=500、rel=5）。"""
    ms = tuple(TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 40)
        for i in range(n))
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid,
                  validity_until_fid=fid + 40)
    return g, ms


class _Car:
    def __init__(self, x_lane, cy=600, rel=50.0):
        self.id = 7
        self.x_lane = x_lane
        self.cy = cy
        self.rel_approach = rel
        self.d_min = 1.0
        self.age_ticks = 1


def test_config_gate_default_off_and_roundtrip(tmp_path) -> None:
    # 缺键安全（老 json）：不带 trajectory_sampling 键 → 闸默认关
    d = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    d["mode"].pop("trajectory_sampling", None)
    old = tmp_path / "decision_old.json"
    old.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    old_cfg = _read_decision(old)
    assert old_cfg.mode.trajectory_sampling is False
    assert CFG.mode.trajectory_sampling is False
    assert CFG_TRAJ.mode.trajectory_sampling is True


def test_gate_off_new_kwargs_change_nothing() -> None:
    """兼容红线：闸关时喂 road_offset/width 与不喂，输出逐字段相同。"""
    g, ms = _group(3, fid=1, x=1.2)
    e1 = DecisionEngine(CFG)
    e2 = DecisionEngine(CFG)
    o1 = e1.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                   traffic=((), ()))
    o2 = e2.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                   traffic=((), ()), road_offset=0.2, road_width=4.5)
    assert o1 == o2


def test_traj_cruise_output_moves_toward_axis() -> None:
    """巡航守轴：executed=0.6（路心系），输出前瞻点 < 0.6 且有限。"""
    e = DecisionEngine(CFG_TRAJ)
    out = e.update(_obs(), DT, executed_lane=0.6, traffic=((), ()),
                   road_offset=0.6, road_width=None)
    assert out.x_target is not None and out.x_target < 0.6
    # 逐拍重评：同状态下输出持续指向轴（不回弹成位置常量漂移）
    out2 = e.update(_obs(fid=2), DT, executed_lane=0.6, traffic=((), ()),
                    road_offset=0.6, road_width=None)
    assert out2.x_target is not None and out2.x_target < 0.6


def test_traj_without_road_offset_degrades_to_legacy() -> None:
    """ro 缺席拍诚实降级：输出与闸关引擎同流一致（不臆造路心系）。"""
    g, ms = _group(3, fid=1, x=0.5)
    e_traj = DecisionEngine(CFG_TRAJ)
    e_leg = DecisionEngine(CFG)
    o_t = e_traj.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                        traffic=((), ()), road_offset=None, road_width=None)
    o_l = e_leg.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                       traffic=((), ()))
    assert o_t.x_target == o_l.x_target and o_t.state == o_l.state


# ---------- S2-B 栅格裁决（2026-10-05 三检验全绿后接线，方案稿见实验 README） ----------

import numpy as np

from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush.latsample import LatTrajectorySampler
from maaracing_master.plugins.speedrush.world_model import load_calib

_LW = load_calib().lane_w_m


def _synth_grid(blocked_ix=(), unknown_ix=()):
    """合成近场栅格：blocked/unknown 铺在 z 3~9m 带（近场裁决域），其余绿。"""
    state = np.zeros((dg.GRID_NZ, dg.GRID_NX), np.int8)
    for ix in blocked_ix:
        state[:24, ix] = dg.GRID_BLOCKED
    for ix in unknown_ix:
        state[:24, ix] = dg.GRID_UNKNOWN
    return dg.DrivableGrid(
        state=state, coef=(0.0, 0.0, -2.0),
        counts=np.ones((dg.GRID_NZ, dg.GRID_NX), np.int32),
        dig_cells=0, latency_ms=0.0)


def _sampler(**kw):
    base = dict(v_lat_max=2.0, v_ego_row=500.0, horizon_s=2.5, gap_lane=0.3)
    base.update(kw)
    return LatTrajectorySampler(**base)


def test_grid_veto_config_roundtrip(tmp_path) -> None:
    # 缺段安全（老 json）：整段缺失=全默认=关
    d = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    d.pop("grid_veto", None)
    old = tmp_path / "decision_old.json"
    old.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    cfg = _read_decision(old)
    assert cfg.grid_veto.enabled is False
    assert cfg.grid_veto.margin_lane == 0.5 and cfg.grid_veto.k_unknown == 0.05
    assert CFG.grid_veto.enabled is False   # 语义测试基座自钉关（部署运行态不随之漂）


def test_sampler_grid_blocks_path_to_wall() -> None:
    """blocked 竖墙立在 +1 道路径的近场带内 → 该候选一票否决。"""
    s = _sampler()
    rep0 = s.update(0.0, 0.0, 0.0, d_target=1.0, cars=(), width=None,
                    ro=0.0, hz=20.0)
    assert rep0.ok and rep0.best.d1 == pytest.approx(1.0)
    assert rep0.n_reject_grid == 0
    ix = int((1.0 * _LW + dg.GRID_X_MAX) / dg.GRID_CELL)   # d=1 道的格列
    rep = s.update(0.0, 0.0, 0.0, d_target=1.0, cars=(), width=None,
                   ro=0.0, hz=20.0,
                   grid=_synth_grid(blocked_ix=[ix]), lane_w_m=_LW)
    assert rep.n_reject_grid >= 1
    assert not rep.ok or rep.best.d1 != pytest.approx(1.0)


def test_sampler_grid_unknown_cost_flips_choice() -> None:
    """近场全 unknown：带越宽的候选 unknown 格越多 → 代价把选择从 +1 翻回轴。"""
    s = _sampler(k_grid_unknown=0.2)
    rep0 = s.update(0.0, 0.0, 0.0, d_target=1.0, cars=(), width=None,
                    ro=0.0, hz=20.0)
    assert rep0.best.d1 == pytest.approx(1.0)
    rep = s.update(0.0, 0.0, 0.0, d_target=1.0, cars=(), width=None,
                   ro=0.0, hz=20.0,
                   grid=_synth_grid(unknown_ix=range(dg.GRID_NX)),
                   lane_w_m=_LW)
    assert rep.ok and rep.best.d1 != pytest.approx(1.0)


def test_sampler_grid_margin_keeps_center_off_wall() -> None:
    """内收语义：墙在 +0.9 道（> 车半宽+margin 0.77 道包络）不挡中心候选，
    但 +1 道候选压墙被灭——贴墙机动被挤走、守轴不受伤。"""
    ix = int((0.9 * _LW + dg.GRID_X_MAX) / dg.GRID_CELL)
    s = _sampler()
    rep = s.update(0.0, 0.0, 0.0, d_target=1.0, cars=(), width=None,
                   ro=0.0, hz=20.0,
                   grid=_synth_grid(blocked_ix=[ix]), lane_w_m=_LW)
    assert rep.ok and rep.best.d1 == pytest.approx(0.0)
    assert rep.n_reject_grid >= 1


def test_sampler_grid_all_veto_aborts() -> None:
    """墙贴身（+0.55 道 < 半宽+margin 包络）：任何横移都压墙 → 全灭诚实弃权
    （ABORT 语义交 FSM），不假装能走。"""
    ix = int((0.55 * _LW + dg.GRID_X_MAX) / dg.GRID_CELL)
    s = _sampler()
    rep = s.update(0.0, 0.0, 0.0, d_target=0.0, cars=(), width=None,
                   ro=0.0, hz=20.0,
                   grid=_synth_grid(blocked_ix=[ix]), lane_w_m=_LW)
    assert not rep.ok and rep.n_reject_grid > 0


def test_decision_grid_gate_off_ignores_grid() -> None:
    """兼容红线（decision 级）：grid_veto 闸关时喂 blocked 栅格与不喂，输出
    逐字段相同——闸关不触栅格代价。"""
    g, ms = _group(3, fid=1, x=0.5)
    wall = _synth_grid(blocked_ix=[48])
    e1 = DecisionEngine(CFG_TRAJ)
    e2 = DecisionEngine(CFG_TRAJ)
    o1 = e1.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                   traffic=((), ()), road_offset=0.5, road_width=None,
                   grid=wall)
    o2 = e2.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                   traffic=((), ()), road_offset=0.5, road_width=None)
    assert o1 == o2


def test_decision_grid_gate_on_blocks_path_to_wall() -> None:
    """闸开：全 blocked 栅格 → 采样全灭（diag ok=False、n_grid 显形）；
    闸关同拍不触栅格（ok=True）——grid 进轨迹评分的接线锁。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand)
    bands = tuple(CorridorBand(
        z0=3.0 + i * 3.0, z1=6.0 + i * 3.0,
        left=BandBoundary(-4.2, "measured"),
        right=BandBoundary(4.2, "measured")) for i in range(2))
    cor = Corridor(bands, 0.0, 8.4 / _lane_w(), "pair", "ok")
    cfg_on = replace(CFG_TRAJ, grid_veto=replace(CFG_TRAJ.grid_veto,
                                                 enabled=True))
    e_off = DecisionEngine(CFG_TRAJ)
    e_on = DecisionEngine(cfg_on)
    o_off = e_off.update(_obs(), DT, executed_lane=0.2, traffic=((), ()),
                         road_offset=0.5, road_width=None, corridor=cor)
    o_on = e_on.update(_obs(), DT, executed_lane=0.2, traffic=((), ()),
                       road_offset=0.5, road_width=None, corridor=cor,
                       grid=_synth_grid(blocked_ix=list(range(dg.GRID_NX))))
    assert e_off.traj_diag is not None and e_off.traj_diag["ok"] is True
    assert e_on.traj_diag is not None and e_on.traj_diag["ok"] is False
    assert e_on.traj_diag["n_grid"] >= 1
    assert o_off.x_target is not None and o_on.x_target is not None


def _lane_w() -> float:
    from maaracing_master.plugins.speedrush.world_model import load_calib
    return load_calib().lane_w_m


# ---------- V2 行为闸：道收益接 CRUISE 轨迹目标 ----------
# （整段随 lane_scoring 退役删除 20261015：corridor_reach 取代，闸锁在
# test_speedrush_corridor_reach_gate.py）

def _mc(tid, *, z=6.0, x_m=0.0, v=10.0):
    """米制车：t_meet=z/v（v≤0 → None=pacing 占用）；x_m 是自车系米
    （executed=0.5 时 x_m=0 → 路心系 0.5=持守道）。"""
    return MetricCarState(target_id=tid, source=DistanceSource.ANCHOR,
                          z_m=z, x_m=x_m, v_close_mps=v,
                          t_meet_s=z / v if v > 0 else None,
                          anchor_age_ms=200.0)


def test_traj_metric_map_wired_with_fusion() -> None:
    """V2 接线：fusion 开时轨迹采样器吃同一米制索引（危险窗截断融合面）；
    闸关=None（纯像素逐位不变）。"""
    cfg = replace(CFG_TRAJ, metric_cars=replace(CFG_TRAJ.metric_cars,
                                                fusion=True))
    e = DecisionEngine(cfg)
    mc = _mc(7)
    e.update(replace(_obs(fid=1), metric_cars=(mc,)), DT, executed_lane=0.5,
             traffic=((), ()), road_offset=0.0, road_width=4.4)
    assert e.traj.metric_map == {7: mc}
    e2 = DecisionEngine(CFG_TRAJ)                  # 闸关
    e2.update(replace(_obs(fid=1), metric_cars=(mc,)), DT, executed_lane=0.5,
              traffic=((), ()), road_offset=0.0, road_width=4.4)
    assert e2.traj.metric_map is None


# （承诺制 Track 1 段随承诺制退役删除 2026-10-18）


