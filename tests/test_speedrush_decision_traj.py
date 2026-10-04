# -*- coding: utf-8 -*-
"""轨迹模式接线锁（阶段二 §2.3）：配置闸、输出形成换血、破判 ABORT、诚实降级。

兼容红线：闸关（默认）时新入参不改变任何输出（逐拍相同）；闸开时 FSM/校验/
选择保留，只换「计划」的形态与输出点。d 坐标系=路心系（道0=路心）。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)

DT = 0.05
CFG = _read_decision(DECISION_FILE)
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


def test_config_gate_default_off_and_roundtrip() -> None:
    assert CFG.mode.trajectory_sampling is False       # 缺键安全（老 json）
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


def test_traj_coin_change_output_tracks_road_frame_target() -> None:
    """金币 A1 x=0.5、ro=0.5 → d_target=1.0（路心系）：输出点向 1.0 推进。"""
    g, ms = _group(3, fid=1, x=0.5)
    e = DecisionEngine(CFG_TRAJ)
    out = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                   traffic=((), ()), road_offset=0.5, road_width=None)
    assert out.state is DecisionState.CHANGE
    assert 0.2 < out.x_target < 1.0                    # 沿轨迹前瞻推进
    out2 = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.25,
                    traffic=((), ()), road_offset=0.5, road_width=None)
    assert out2.x_target > out.x_target                # 续拍继续向目标推进


def test_traj_persistent_block_aborts() -> None:
    """选中后车流合围（目标位与退路全被驻留车占据）→ 连续破判达防拍数 →
    ABORT。合围车在选中拍之后出现——选择门的 legacy veto 不拦当时不存在
    的车，滚动重评的采样器破判才是本测试对象。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t = _Car(x_lane=0.2, cy=650, rel=0.0)          # d_road=0.7 目标位
    car_n = _Car(x_lane=-0.8, cy=650, rel=0.0)         # d_road=-0.3 贴身
    e = DecisionEngine(CFG_TRAJ)
    o1 = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                  traffic=((), ()), road_offset=0.5, road_width=None)
    assert o1.state is DecisionState.CHANGE            # 干净拍先选中
    o2 = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.2,
                  traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o2.state is DecisionState.CHANGE            # 首拍破判：防抖不撕计划
    o3 = e.update(_obs(fid=3, groups=(g,), targets=ms), DT, executed_lane=0.2,
                  traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o3.state is DecisionState.ABORT_CHANGE
    assert "traj_blocked" in o3.reason


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


def test_traj_width_consumed_in_plans() -> None:
    """W 收窄可用终点：目标 0.5 在 W=1.2（界 ±0.45）之外 → 输出点被界拉回
    轴侧、显著小于无 W 的贴目标输出（终点钳制进入引擎链路）。"""
    g, ms = _group(3, fid=1, x=0.5)
    e_narrow = DecisionEngine(CFG_TRAJ)
    e_free = DecisionEngine(CFG_TRAJ)
    o_n = e_narrow.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                          traffic=((), ()), road_offset=0.0, road_width=1.2)
    o_f = e_free.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                        traffic=((), ()), road_offset=0.0, road_width=None)
    assert o_n.state is DecisionState.CHANGE
    assert o_n.x_target < 0.45                         # 界内
    assert o_n.x_target < o_f.x_target                 # 窄界把终点拉向轴


def test_traj_completion_in_road_frame() -> None:
    """反馈完成判据走路心系：executed 到达滚动 d_target（ro+x=0.7）→ done。"""
    g, ms = _group(3, fid=1, x=0.5)
    e = DecisionEngine(CFG_TRAJ)
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((), ()), road_offset=0.2, road_width=None)
    out = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.65,
                   traffic=((), ()), road_offset=0.65, road_width=None)
    assert out.state is DecisionState.CRUISE
    assert out.reason.startswith("done:")
