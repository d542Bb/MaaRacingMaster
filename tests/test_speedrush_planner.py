# -*- coding: utf-8 -*-
"""planner 层回归锁（planner 设计稿 v1 §九 step 1；维护者 2026-09-22 四裁定上锁）。

锁面：状态消费表全矩阵、重锚有符号且同拍生效、ABORT 冻结值归属、过期保持与
CONSERVE/FAULT 优先级、时间基低通、每 tick 限幅语义、死区边界 255/256、
恒油门、dt 非法 fail-loud、回放一致性（C3）。
"""

from __future__ import annotations

import math

import pytest

from maaracing_master.plugins.speedrush.config import Planner, _read_decision
from maaracing_master.plugins.speedrush.planner import LateralPlanner
from maaracing_master.plugins.speedrush.tracking import (
    DecisionOutput, DecisionState)
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH

DT = 0.05
CFG = _read_decision(CFG_PATH)
P = CFG.planner


def _out(state=DecisionState.CRUISE, x_target=0.0, fid=1, valid=3,
         reanchor=None, move=True) -> DecisionOutput:
    return DecisionOutput(
        schema_version=2, state=state, target_id=None, x_target=x_target,
        move_allowed=move, reason="test", emitted_fid=fid,
        valid_until_fid=valid, reanchor_lane=reanchor)


def _planner(**over) -> LateralPlanner:
    base = {k: getattr(P, k) for k in P.__dataclass_fields__}
    base.update(over)
    return LateralPlanner(Planner(**base))


def _run_moving(pl: LateralPlanner, target: float, ticks: int = 3, fid0: int = 1):
    """瞬态：移动中打舵（稳态会收敛归零，测保持/归零必须取移动中的拍）。"""
    cmd = None
    for i in range(ticks):
        cmd = pl.update(_out(x_target=target, fid=fid0 + i, valid=fid0 + i),
                        DT, fid0 + i)
    return cmd


# ---------- 非法输入 fail-loud ----------

@pytest.mark.parametrize("bad", [0.0, -0.05, float("nan"), float("inf")])
def test_bad_dt_fail_loud(bad):
    pl = LateralPlanner(P)
    with pytest.raises(ValueError):
        pl.update(_out(), bad, 1)


def test_nan_x_target_fail_loud():
    pl = LateralPlanner(P)
    with pytest.raises(ValueError):
        pl.update(_out(x_target=float("nan")), DT, 1)


# ---------- 恒油门（V0 通道） ----------

def test_throttle_constant_all_states():
    pl = LateralPlanner(P)
    for st in DecisionState:
        cmd = pl.update(_out(state=st, x_target=0.5), DT, 1)
        assert cmd.throttle == P.throttle_raw == 255


# ---------- 状态消费表 ----------

def test_conserve_forces_zero():
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)                 # 移动中已打舵
    prev = pl._last_raw
    assert abs(prev) > 20000
    cmd = pl.update(_out(state=DecisionState.CONSERVE, x_target=None, fid=99,
                         valid=99), DT, 99)
    assert abs(cmd.steer_x) < abs(prev)
    # 持续 CONSERVE → 归零（经限幅时间）
    for i in range(20):
        cmd = pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                             fid=100 + i, valid=100 + i), DT, 100 + i)
    assert cmd.steer_x == 0


def test_fault_forces_zero_same_as_conserve():
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)
    for i in range(30):
        cmd = pl.update(_out(state=DecisionState.FAULT, x_target=1.0,
                             fid=100 + i, valid=100 + i), DT, 100 + i)
    assert cmd.steer_x == 0


# ---------- 重锚（裁定 1 + 契约细节 1） ----------

def test_reanchor_signed_and_same_tick_effect():
    pl = LateralPlanner(P)
    pl.state.executed_lane = 0.3
    pl.state.v_lat_est = 2.0
    cmd = pl.update(_out(reanchor=-0.5, fid=1, valid=1), DT, 1)
    # 锚点先于控制：executed 被有符号覆写、v_lat 清零；随后积分一拍才变化
    assert pl.state.executed_lane == pytest.approx(-0.5, abs=0.05)
    assert cmd.steer_x > 0                        # 目标 0、自车 −0.5 → 向右回


def test_reanchor_none_means_noop():
    pl = LateralPlanner(P)
    pl.state.executed_lane = 0.3
    pl.update(_out(fid=1, valid=1), DT, 1)
    assert pl.state.executed_lane == pytest.approx(0.3, abs=0.02)


# ---------- ABORT 冻结值归属（契约细节 3） ----------

def test_abort_freezes_executed_not_decision_x():
    pl = LateralPlanner(P)
    _run_moving(pl, 0.8, ticks=6)                 # 变道中：executed>0 且仍在向右打
    assert pl.state.executed_lane > 0.05 and pl._last_raw > 0
    frozen = pl.state.executed_lane
    # 决策层 ABORT 路径 x_target 回落 0——规划层不得跟它回中（反打舵）
    cmd = pl.update(_out(state=DecisionState.ABORT_CHANGE, x_target=0.0,
                         fid=300, valid=300), DT, 300)
    assert cmd.steer_x >= 0                       # 不向左打回 0
    # 持续 ABORT：跟踪冻结位，不随 x_target=0 漂移
    for i in range(1, 10):
        pl.update(_out(state=DecisionState.ABORT_CHANGE, x_target=0.0,
                       fid=300 + i, valid=300 + i), DT, 300 + i)
    assert pl.state.executed_lane == pytest.approx(frozen, abs=0.15)
    # 离开 ABORT 后恢复正常跟踪
    pl.update(_out(state=DecisionState.CRUISE, x_target=0.0,
                   fid=400, valid=400), DT, 400)
    assert pl._abort_hold_lane is None


# ---------- 过期保持与优先级（裁定 3 + 契约细节 2） ----------

def test_expired_hold_then_conserve_at_plus_one():
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)
    held = pl._last_raw
    assert held > 20000
    # 过期第 1..hold_max_ticks 拍：杆值保持（steer_raw=当前 steer_norm，输出不反打）
    for i in range(1, P.hold_max_ticks + 1):
        cmd = pl.update(_out(x_target=1.0, fid=10 + i, valid=9), DT, 10 + i)
        assert cmd.steer_x > 20000                # 保持方向与幅度（允许限幅缓变）
    # 第 hold_max_ticks+1 拍起按 CONSERVE：明确向 0 走
    cmd = pl.update(_out(x_target=1.0, fid=99, valid=9), DT, 99)
    assert cmd.steer_x < held


def test_conserve_priority_over_expired_hold():
    """CONSERVE/FAULT 的强制归零优先级高于过期保持（维护者裁定 3）。"""
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)
    held = pl._last_raw
    cmd = pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                         fid=99, valid=9), DT, 99)   # 过期 + CONSERVE 同时成立
    assert cmd.steer_x < held - 2000 or cmd.steer_x == 0


def test_valid_until_inclusive_boundary():
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)
    # current_fid == valid_until_fid：有效（含边界），过期计数清零
    pl.update(_out(x_target=1.0, fid=50, valid=50), DT, 50)
    assert pl._expired_ticks == 0
    # current_fid == valid+1：过期第 1 拍
    pl.update(_out(x_target=1.0, fid=51, valid=50), DT, 51)
    assert pl._expired_ticks == 1


# ---------- 时间基低通（行为稿 §五纪律） ----------

def test_lowpass_is_time_based_not_fixed_alpha():
    """定值 α 会让 dt=0.1 单拍与 dt=0.05 单拍同效——时间基必须不同。"""
    pl1 = LateralPlanner(P)
    pl1.update(_out(x_target=1.0, fid=1, valid=1), 0.05, 1)
    pl2 = LateralPlanner(P)
    pl2.update(_out(x_target=1.0, fid=1, valid=1), 0.10, 1)
    assert pl2.state.steer_norm > pl1.state.steer_norm
    a1 = 1 - math.exp(-0.05 / P.tau_steer_s)
    a2 = 1 - math.exp(-0.10 / P.tau_steer_s)
    assert pl1.state.steer_norm == pytest.approx(a1 * 1.0, rel=1e-6)
    assert pl2.state.steer_norm == pytest.approx(a2 * 1.0, rel=1e-6)


# ---------- 限幅与死区（旧栈验证值的接口语义） ----------

def test_rate_limit_per_tick_semantics():
    pl = LateralPlanner(P)
    prev = 0
    for i in range(6):
        cmd = pl.update(_out(x_target=1.0, fid=i, valid=i), DT, i)
        assert abs(cmd.steer_x - prev) <= int(round(P.rate_limit_raw)) + 1
        prev = cmd.steer_x
    assert prev > 0                               # 确实在向目标爬


@pytest.mark.parametrize("x_target,expect_zero", [
    (255 / 32767 / P.k_p, True),                  # raw=255 → 死区归 0
    (256 / 32767 / P.k_p, False),                 # raw=256 → 保留
])
def test_deadzone_boundary_255_256(x_target, expect_zero):
    # 隔离输出链边界：无 lookahead/阻尼/低通/限幅，且 v_lat 增益置零
    # （executed 不漂移，PD 稳态恒等于 k_p·x_target）
    pl = _planner(k_d=0.0, lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, v_lat_gain=0.0)
    cmd = None
    for i in range(5):
        cmd = pl.update(_out(x_target=x_target, fid=i, valid=i), DT, i)
    assert (cmd.steer_x == 0) is expect_zero


# ---------- 回放一致性（C3） ----------

def test_replay_reproduces_stick_stream():
    def run():
        pl = LateralPlanner(P)
        seq = [_out(x_target=t, fid=i, valid=i + 1)
               for i, t in enumerate([0.0, 0.5, 0.5, 0.0, -0.8, 1.0, 0.2], 1)]
        return [pl.update(d, DT, d.emitted_fid).steer_x for d in seq]
    assert run() == run()


# ---------- 前瞻 PD 的方向语义 ----------

def test_lookahead_damps_overshoot():
    """大提前量外推：高速接近目标时反向收力（x_pred 越过 target → steer 变号）。"""
    pl = LateralPlanner(P)
    pl.state.executed_lane = 0.4
    pl.state.v_lat_est = 3.0                      # 向右快速接近 0.5
    cmd = pl.update(_out(x_target=0.5, fid=1, valid=1), DT, 1)
    # x_pred = 0.4 + 3.0×0.25 = 1.15 > 0.5 → 误差为负 → 收力（不继续右打）
    assert cmd.steer_x <= 0
