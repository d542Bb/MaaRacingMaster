# -*- coding: utf-8 -*-
"""并线中无速度的自锁复现（20261101_191929#1 对局日志核对）。

复现的链条（trace 逐拍复放已核对，杆值 ±0.005 吻合）：

1. 直道巡航攒不满速度估计的门（横移斜率与车头角都过小），在线车速过期；
2. 并线起意时车速未知 → 计划如实判「无速度」无效，承诺并线期计划短断供
   保持刻意关掉，控制走决策目标的 PD；
3. 车速未知时目标车头角退回保守小角度比例（0.1×误差道，0.36 道误差
   → 0.037 rad）；
4. 车模影子被线角校正拉在实测线角附近（目标固定时不再有新误差 inflow）
   → 朝向误差项把杆值压塌成 ~0.1 的小振荡，远低于并线需要的持续杆量
   （对照：同局第二段并线目标每拍滑动，杆值 0.25~0.42 持续到完成）；
5. 真车响应远弱于车模（0.36 杆 0.3 s 实测只转 ~0.01 rad，模型预测
   ~0.05 rad，差 ~5 倍）→ 0.1 量级的振荡命令不产生横移（并线段位置
   读数 5.3 s 不动）→ 无横移斜率证据 → 速度估计不恢复 → 计划保持无效
   → 并线原地停。

同一局里目标每拍滑动的第二段并线不受此困（每次换目标都重算误差、重设
车头角目标，杆值持续到完成）——目标固定才是杆值被朝向反馈封死的条件。

本测试钉住**当前行为**作为修复基线：杆值在并线起意后 ~0.5 s 内衰减到
~0，而位置误差不收敛、速度估计不恢复。修复后应把断言翻转为期望行为
（杆值持续到误差收窄或速度恢复）。
"""

from __future__ import annotations

from maaracing_master.plugins.speedrush.config import Planner, _read_decision
from maaracing_master.plugins.speedrush.planner import LateralPlanner
from maaracing_master.plugins.speedrush.tracking import (
    DecisionOutput, DecisionState)
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH

DT = 0.05
CFG = _read_decision(CFG_PATH)
P = CFG.planner

TARGET_LANE = 0.83      # 起意拍的目标道
START_LANE = 0.47       # 起意拍车所在道（误差 0.36 道）
START_PSI = 0.005       # 起意拍实测线角量级（rad）


def _out(phase="merging", x_target=TARGET_LANE, fid=1) -> DecisionOutput:
    return DecisionOutput(
        schema_version=5, state=DecisionState.CRUISE, x_target=x_target,
        reason="test", emitted_fid=fid, valid_until_fid=fid + 3,
        maneuver=True, phase=phase)


class _Plant:
    """弱响应真车：按对局日志核对建模——车不横移（并线段位置读数不动）、
    车头角几乎不转（实测全程 ≤0.016 rad）。观测带 ~0.15 s 延迟。"""

    def __init__(self, delay_s: float = 0.15):
        self.delay = delay_s
        self.heading = START_PSI
        self.x = START_LANE
        self._t = 0.0
        self._hist = [(0.0, self.heading, self.x)]

    def tick(self, u: float) -> tuple[float, float]:
        # 车不动：位置与车头角保持（对局日志核对的并线段形态）
        self._t += DT
        self._hist.append((self._t, self.heading, self.x))
        cut = self._t - self.delay
        psi, x = self._hist[0][1], self._hist[0][2]
        for t, ph, xx in self._hist:
            if t > cut:
                break
            psi, x = ph, xx
        return psi, x


def _run(n_ticks: int):
    """复放主循环：车速未知（不喂速度估计）+ 并线承诺（目标固定）。"""
    pl = LateralPlanner(Planner(**{k: getattr(P, k)
                                   for k in P.__dataclass_fields__}))
    plant = _Plant()
    log = []
    for i in range(n_ticks):
        psi_m, x_m = plant.tick(pl.state.steer_norm if i else 0.0)
        pl.update(
            _out(fid=i + 1), DT, current_fid=i + 1,
            road_offset=x_m, lane_x=x_m, lane_ok=True,
            lane_theta=psi_m, lane_theta_ok=True,
            lane_fid=i + 1, lane_age_ms=25.0, lane_n_locked=3,
            plan=None)
        log.append({
            "tick": i,
            "stick": pl.last_steer_raw,
            "err": abs(TARGET_LANE - pl.state.executed_lane),
            "psi": psi_m,
            "pm": pl.head.mpsi,
            "v_scale": pl.head.v_scale,
        })
    return log


def test_merge_without_speed_stick_decays_to_zero():
    """并线承诺 + 车速未知 + 目标固定：起意爆发后杆值塌缩成小振荡，
    永不恢复到并线需要的量级（对照：第二段并线靠目标滑动维持 0.25+）。"""
    log = _run(60)  # 3 s
    burst = max(e["stick"] for e in log[:6])
    assert burst > 0.15, f"起意拍应有明显杆值（实测 {burst:.3f}）"
    late = log[10:]  # 0.5 s 之后
    assert max(e["stick"] for e in late) < 0.15, (
        f"0.5 s 后杆值应已塌缩（实测峰值 {max(e['stick'] for e in late):.3f}，"
        f"起意 {burst:.3f}）")


def test_merge_without_speed_error_persists():
    """杆值归零时误差仍在：车不横移，3 s 后仍差 0.3 道以上。"""
    log = _run(60)
    decayed = next(e for e in log if e["stick"] < 0.05)
    assert decayed["err"] > 0.3, (
        f"杆值归零时误差应仍在（实测 {decayed['err']:.3f} 道）")
    assert log[-1]["err"] > 0.3, (
        f"3 s 后误差应不收敛（实测 {log[-1]['err']:.3f} 道）")


def test_merge_without_speed_estimator_never_recovers():
    """自锁闭环：车不横移 → 无斜率证据 → 速度估计不恢复。"""
    log = _run(60)
    assert all(e["v_scale"] is None for e in log), (
        "车不横移时速度估计不应自行恢复（这正是自锁的闭合条件）")


def test_shadow_holds_command_below_response_threshold():
    """朝向反馈把命令压在真车响应以下的振荡：影子车头角被线角校正拉在
    实测线角附近，位置误差换算的车头角目标（0.036 rad）与影子之差决定
    杆值——命令塌缩成 ~0.1 振荡，真车响应该量级不产生横移。"""
    log = _run(20)
    peak_pm = max(e["pm"] for e in log if e["pm"] is not None)
    psi_des = P.k_head_lane * (TARGET_LANE - START_LANE)
    assert peak_pm < psi_des - 0.01, (
        f"影子车头角应远低于位置误差换算的目标（实测 {peak_pm:.4f} vs "
        f"目标 {psi_des:.4f} rad）")
