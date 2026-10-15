# -*- coding: utf-8 -*-
"""轨迹模式接线锁（阶段二 §2.3）：配置闸、输出形成换血、破判 ABORT、诚实降级。

兼容红线：闸关（默认）时新入参不改变任何输出（逐拍相同）；闸开时 FSM/校验/
选择保留，只换「计划」的形态与输出点。d 坐标系=路心系（道0=路心）。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import Commitment, _read_decision
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
              metric_cars=replace(_CFG_BASE.metric_cars, fusion=False),
              # 走廊收益场闸显式钉死（不继承部署态，rollout 翻转不漂）
              corridor_reach=replace(_CFG_BASE.corridor_reach,
                                     enabled=False, control=False),
              # 承诺制闸自钉关：traj_blocked 旧行为（立即作废）是本文件回归锁
              commitment=Commitment())
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
    """闸开：blocked 墙立在 d=1.0 目标路径 → 输出点不再朝目标推进
    （压墙候选被否决，选择退守轴侧）。"""
    g, ms = _group(3, fid=1, x=0.5)
    cfg_on = replace(CFG_TRAJ, grid_veto=replace(CFG_TRAJ.grid_veto,
                                                 enabled=True))
    e_off = DecisionEngine(CFG_TRAJ)
    e_on = DecisionEngine(cfg_on)
    o_off = e_off.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                         traffic=((), ()), road_offset=0.5, road_width=None)
    o_on = e_on.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                        traffic=((), ()), road_offset=0.5, road_width=None,
                        grid=_synth_grid(blocked_ix=[48]))
    assert o_off.x_target is not None and o_on.x_target is not None
    assert o_on.x_target < o_off.x_target


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


# ---------- 承诺制 Track 1（2026-10-15 设计稿 v2 §3：traj_blocked 暂停不弃追） ----------

def _cfg_pause(budget: float = 0.3):
    return replace(CFG_TRAJ, commitment=replace(CFG_TRAJ.commitment,
                                                pause_enabled=True,
                                                pause_budget_s=budget))


def _block_cars():
    """合围车流（沿用 test_traj_persistent_block_aborts 的几何）：目标位驻留 +
    贴身退路，两车都在扫掠恶化判据外（不触发 lat_veto，纯采样器破判）。"""
    return _Car(x_lane=0.2, cy=650, rel=0.0), _Car(x_lane=-0.8, cy=650, rel=0.0)


def test_commitment_config_roundtrip(tmp_path) -> None:
    # 缺段安全（老 json）：整段缺失=全默认=关
    d = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    d.pop("commitment", None)
    old = tmp_path / "decision_old.json"
    old.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    cfg = _read_decision(old)
    assert cfg.commitment.pause_enabled is False
    assert cfg.commitment.pause_budget_s == pytest.approx(1.0)
    assert CFG.commitment.pause_enabled is False       # 语义基座自钉关


def test_commitment_pause_holds_within_budget_then_aborts() -> None:
    """连续破判：预算内暂停不弃追（state 保持 CHANGE、x_target 冻结在
    executed、target_id 不丢）；连续阻塞跨过预算才作废（旧行为）。
    DT=0.05/预算 0.3 → 1 拍防抖 + 5 拍暂停（0.05..0.25s），第 6 个失败拍
    （0.30s）作废。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause())
    o = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((), ()), road_offset=0.5, road_width=None)
    assert o.state is DecisionState.CHANGE             # 干净拍先选中
    pauses = []
    for fid in range(2, 12):
        o = e.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                     executed_lane=0.2,
                     traffic=((car_t, car_n), ()), road_offset=0.5,
                     road_width=None)
        if o.state is DecisionState.ABORT_CHANGE:
            break
        if o.reason == "pause:blocked":
            pauses.append(o)
    assert len(pauses) == 5
    assert all(p.state is DecisionState.CHANGE for p in pauses)
    assert all(p.x_target == pytest.approx(0.2) for p in pauses)
    assert all(p.target_id == 3 for p in pauses)       # 原改道任务保留
    assert o.state is DecisionState.ABORT_CHANGE
    assert "traj_blocked" in o.reason


def test_commitment_pause_freeze_syncs_smoother() -> None:
    """§3.3 冻结生效点：暂停拍 x_target 直通 executed 绕开平滑器，_x_smooth
    同步到冻结位——随后降级拍（ro 缺席走平滑器流）从冻结位小步出发，不朝
    旧 goal 跳。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause())
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((), ()), road_offset=0.5, road_width=None)
    e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    out = e.update(_obs(fid=3, groups=(g,), targets=ms), DT, executed_lane=0.2,
                   traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert out.reason == "pause:blocked"
    assert e._x_smooth == pytest.approx(0.2)           # 同步，无旧平滑残留
    out2 = e.update(_obs(fid=4, groups=(g,), targets=ms), DT, executed_lane=0.2,
                    traffic=((car_t, car_n), ()), road_offset=None,
                    road_width=None)
    # 平滑器从 0.2 朝 goal=0.5 走一步：alpha=1−exp(−1/3)≈0.28 → ≈0.285
    assert out2.x_target == pytest.approx(0.2, abs=0.1)


def test_commitment_pause_recovery_resumes_and_recounts() -> None:
    """恢复优先 + 预算=单次连续阻塞（§3.4）：traj 恢复一拍即续跑原需求并清零
    重计——阻断→恢复→再阻断累计超预算不作废，只有第二段连续阻塞跨线才作废。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause())
    o = e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((), ()), road_offset=0.5, road_width=None)
    assert o.state is DecisionState.CHANGE
    # 第一段阻塞：1 拍防抖 + 1 拍暂停（0.05s）
    o = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.reason != "pause:blocked"                 # 防抖拍不撕计划
    o = e.update(_obs(fid=3, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.reason == "pause:blocked"
    # 恢复拍：续跑原需求（仍 CHANGE，输出回到轨迹前瞻推进）
    o = e.update(_obs(fid=4, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((), ()), road_offset=0.5, road_width=None)
    assert o.state is DecisionState.CHANGE and o.reason == "moving:feedback"
    assert o.x_target > 0.2
    # 第二段阻塞：防抖 1 拍 + 暂停 0.05..0.25s（整集累计 0.30s 若累计早已越线）
    for fid in range(5, 11):
        o = e.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                     executed_lane=0.2,
                     traffic=((car_t, car_n), ()), road_offset=0.5,
                     road_width=None)
        if fid == 5:
            assert o.reason != "pause:blocked"         # 新段从防抖重新起算
        else:
            assert o.reason == "pause:blocked" \
                and o.state is DecisionState.CHANGE
    # 第二段连续阻塞跨过 0.3s（第 6 个失败拍）才作废
    o = e.update(_obs(fid=11, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.state is DecisionState.ABORT_CHANGE
    assert "traj_blocked" in o.reason


def test_commitment_pause_does_not_delay_lat_veto() -> None:
    """横向安全 veto 先行（§3.2 硬边界）：暂停期间机动走廊被街车占据 → 立即
    既有回稳，不等预算（安全面不承诺）。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause())
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((), ()), road_offset=0.5, road_width=None)
    e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    o = e.update(_obs(fid=3, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.reason == "pause:blocked"                 # 暂停中
    veto_car = _Car(x_lane=0.35, cy=650, rel=0.0)      # 扫掠走廊 [0.2→0.5] 内
    o = e.update(_obs(fid=4, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((car_t, car_n, veto_car), ()), road_offset=0.5,
                 road_width=None)
    assert o.state is DecisionState.ABORT_CHANGE
    assert o.reason == "cancel:lat_veto"


def test_commitment_pause_does_not_delay_fatal() -> None:
    """致命校验先行（§3.2 硬边界）：暂停期间帧过旧 → 既有 validate_fail 流程
    立即接管，不因预算未耗尽而等待。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause())
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((), ()), road_offset=0.5, road_width=None)
    e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    o = e.update(_obs(fid=3, groups=(g,), targets=ms), DT, executed_lane=0.2,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.reason == "pause:blocked"
    stale = replace(_obs(fid=4, groups=(g,), targets=ms),
                    health=PerceptionHealth(True, False, True, True, False))
    o = e.update(stale, DT, executed_lane=0.2,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.state is DecisionState.ABORT_CHANGE
    assert o.reason == "validate_fail:frame_stale"


def test_commitment_pause_does_not_protect_expired_target() -> None:
    """目标过期先行（§3.2 硬边界）：暂停期间金币组过宽限期 → 既有
    cancel:target_lost 立即接管，预算未耗尽也不保留过期任务。"""
    g, ms = _group(3, fid=1, x=0.5)                    # 组有效期至 fid=41
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause(budget=5.0))         # 预算远长于过期窗
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((), ()), road_offset=0.5, road_width=None)
    n_pause = 0
    o = None
    for fid in range(2, 45):
        o = e.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                     executed_lane=0.2,
                     traffic=((car_t, car_n), ()), road_offset=0.5,
                     road_width=None)
        if o.reason == "pause:blocked":
            n_pause += 1
        if o.state is DecisionState.ABORT_CHANGE:
            break
    assert n_pause > 0                                 # 过期前确实在暂停
    assert o.state is DecisionState.ABORT_CHANGE
    assert o.reason == "cancel:target_lost"


def test_commitment_pause_absent_executed_no_pause() -> None:
    """executed 缺席拍不进暂停（§3.3）：回执缺位走既有降级流（诚实降级），
    不发 pause 语义。"""
    g, ms = _group(3, fid=1, x=0.5)
    car_t, car_n = _block_cars()
    e = DecisionEngine(_cfg_pause())
    e.update(_obs(groups=(g,), targets=ms), DT, executed_lane=0.2,
             traffic=((), ()), road_offset=0.5, road_width=None)
    o = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=None,
                 traffic=((car_t, car_n), ()), road_offset=0.5, road_width=None)
    assert o.state is DecisionState.CHANGE
    assert o.reason != "pause:blocked"


def test_commitment_pause_on_clean_traj_bit_identical() -> None:
    """回归锁（含正常场景）：pause_enabled=true 但 traj 全程 ok → 与闸关输出
    逐位相同（承诺制只在破判拍生效，正常改道零干扰）。"""
    g, ms = _group(3, fid=1, x=0.5)
    e1 = DecisionEngine(CFG_TRAJ)                      # 闸关
    e2 = DecisionEngine(_cfg_pause())                  # 闸开
    for fid in (1, 2, 3, 4):
        o1 = e1.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                       executed_lane=0.2 + 0.05 * fid,
                       traffic=((), ()), road_offset=0.5, road_width=None)
        o2 = e2.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                       executed_lane=0.2 + 0.05 * fid,
                       traffic=((), ()), road_offset=0.5, road_width=None)
        assert o1 == o2
