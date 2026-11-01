# -*- coding: utf-8 -*-
"""并线中无速度的自锁与修复回归锁（20261101_191929#1 对局日志核对）。

自锁链条（trace 逐拍复放已核对，杆值 ±0.005 吻合）：

1. 直道巡航攒不满速度估计的门（横移斜率与车头角都过小），在线车速过期；
2. 并线起意时车速未知 → 计划如实判「无速度」无效，承诺并线期计划短断供
   保持刻意关掉，控制走决策目标的 PD；
3. 车速未知时目标车头角退回保守小角度比例（0.1×误差道，0.36 道误差
   → 0.037 rad）；
4. 车模影子被线角校正拉在实测线角附近（目标固定时不再有新误差 inflow）
   → 朝向误差项把杆值压塌成 ~0.1 的小振荡，远低于并线需要的持续杆量
   （对照：同局第二段并线目标每拍滑动，杆值 0.25~0.42 持续到完成）；
5. 0.1 量级的振荡命令不产生横移（并线段位置读数 5.3 s 不动）→ 无横移
   斜率证据 → 速度估计不恢复 → 计划保持无效 → 并线原地停。

修复（并线承诺 + 车速未知）：位置误差直接驱动**持续杆值**（ψ̂=0、无角
速度，与线断供退化段同一条式子），位置环经 ⑥b 收口；速度估计与计划
判「无速度」的口径不变。**范围只限并线承诺**：保持态下车速未知仍走
原朝向反馈（不贪心扩大）。
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
SUSTAIN_FLOOR = 0.15    # 持续杆值下限（k_psi·k_head·0.36 ≈ 0.22，留阻尼余量）


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


def _run(n_ticks: int, phase: str = "merging"):
    """复放主循环：车速未知（不喂速度估计）+ 固定目标 + 弱响应真车。"""
    pl = LateralPlanner(Planner(**{k: getattr(P, k)
                                   for k in P.__dataclass_fields__}))
    plant = _Plant()
    log = []
    for i in range(n_ticks):
        psi_m, x_m = plant.tick(pl.state.steer_norm if i else 0.0)
        pl.update(
            _out(phase=phase, fid=i + 1), DT, current_fid=i + 1,
            road_offset=x_m, lane_x=x_m, lane_ok=True,
            lane_theta=psi_m, lane_theta_ok=True,
            lane_fid=i + 1, lane_age_ms=25.0, lane_n_locked=3,
            plan=None)
        log.append({
            "tick": i,
            "stick": pl.last_steer_raw,
            "err": abs(TARGET_LANE - pl.state.executed_lane),
            "psi": psi_m,
            "v_scale": pl.head.v_scale,
        })
    return log


def test_merge_without_speed_stick_sustains():
    """并线承诺 + 车速未知 + 目标固定：杆值持续在误差比例量级，不塌缩。"""
    log = _run(60)  # 3 s
    burst = max(e["stick"] for e in log[:6])
    assert burst > SUSTAIN_FLOOR, f"起意拍应有明显杆值（实测 {burst:.3f}）"
    late = log[10:]  # 0.5 s 之后
    weak = min(e["stick"] for e in late)
    assert weak > SUSTAIN_FLOOR, (
        f"0.5 s 后杆值应持续（实测最低 {weak:.3f}；塌缩即自锁复发）")


def test_merge_without_speed_error_persists():
    """车不横移（台架按对局日志核对建模）时误差仍在：杆值归因于位置误差。"""
    log = _run(60)
    assert log[-1]["err"] > 0.3, (
        f"3 s 后误差应不收敛（实测 {log[-1]['err']:.3f} 道）")


def test_merge_without_speed_estimator_never_recovers():
    """车不横移 → 无斜率证据 → 速度估计不恢复（估计口径不变，不兜底）。"""
    log = _run(60)
    assert all(e["v_scale"] is None for e in log), (
        "车不横移时速度估计不应自行恢复（这正是自锁的闭合条件）")


def test_keep_phase_v_unknown_unchanged():
    """范围锁：保持态 + 车速未知仍走原朝向反馈（塌缩行为不变，不贪心）。"""
    log = _run(60, phase="keep")
    late = log[10:]
    assert max(e["stick"] for e in late) < SUSTAIN_FLOOR, (
        f"保持态杆值应维持原朝向反馈行为（实测峰值 "
        f"{max(e['stick'] for e in late):.3f}）")
