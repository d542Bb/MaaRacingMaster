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
from maaracing_master.plugins.speedrush.planner import (
    _STICK_FULL, HeadingObserver, LateralPlanner, law, step)
from maaracing_master.plugins.speedrush.tracking import (
    DecisionOutput, DecisionState)
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH

DT = 0.05
CFG = _read_decision(CFG_PATH)
P = CFG.planner


def _out(state=DecisionState.CRUISE, x_target=0.0, fid=1, valid=3,
         maneuver=False) -> DecisionOutput:
    return DecisionOutput(
        schema_version=5, state=state, x_target=x_target,
        reason="test", emitted_fid=fid,
        valid_until_fid=valid, maneuver=maneuver)


def _planner(**over) -> LateralPlanner:
    base = {k: getattr(P, k) for k in P.__dataclass_fields__}
    base.update(over)
    return LateralPlanner(Planner(**base))


def test_law_matches_reference_and_v_ref():
    e, v = 0.4, 0.3
    assert law(e, v, P) == pytest.approx(P.k_p * e - P.k_d * v)
    # 前馈参考：实测横速等于参考时不产生阻尼项
    assert law(0.0, v, P, v_ref=v) == pytest.approx(0.0)
    assert law(0.2, v, P, gentle=True, deadband=0.0) == pytest.approx(
        min(P.hold_stick_max, P.k_p * 0.2 - P.k_d * v))


def test_step_matches_reference_equations():
    x, v, steer = 0.3, -0.4, 0.2
    u, dt = 0.7, 0.05
    alpha = 1 - math.exp(-dt / P.tau_steer_s)
    s = steer + alpha * (u - steer)
    vv = v + (P.a_lat_gain * s - v / P.tau_align_s) * dt
    vv = max(-P.v_lat_max, min(P.v_lat_max, vv))
    assert step(x, v, steer, u, dt, P) == pytest.approx(
        (x + vv * dt, vv, s), abs=0.0)


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
        assert cmd.throttle == P.throttle_raw
        assert 0 < P.throttle_raw < 255   # 2026-10-19 起降油门给感知/决策留时间


# ---------- 状态消费表 ----------

def test_conserve_holds_entry_lane():
    """CONSERVE=保持入拍车道（13:02 局锁面变更：旧语义"归零直开"在弯道上=怼护栏）。
    变道中被保守打断：先反打止漂，收敛后停在入拍车道位——不回道 0，末态仍归零
    （直道无观测的旧行为兼容面：平衡点=保持位=杆 0）。"""
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)                 # 变道中：向右漂移且已打舵
    entry = pl.state.executed_lane
    assert entry > 0.05
    prev = pl._last_raw
    cmd = pl.update(_out(state=DecisionState.CONSERVE, x_target=None, fid=99,
                         valid=99), DT, 99)
    assert cmd.steer_x < prev                     # 入拍即接管（旧语义同向缓归零）
    for i in range(10):
        cmd = pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                             fid=100 + i, valid=100 + i), DT, 100 + i)
    assert cmd.steer_x < 0                        # 数拍内反打（止漂+拉回，非归零直开）
    for i in range(100):
        cmd = pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                             fid=200 + i, valid=200 + i), DT, 200 + i)
    assert cmd.steer_x == 0                       # 平衡点=保持位死区内，杆终归零
    # 死区语义（16:44 局后）：|误差|<hold_deadband 不发力——停在保持位死区内即可，
    # 不得回 0（方向判据：entry>0 时若回 0 误差会超死区发力，这里必须同侧）
    assert 0 < pl.state.executed_lane < entry + P.hold_deadband_lane


def test_conserve_hold_refreezes_on_reentry():
    """离开 CONSERVE 再进：hold 重新冻结为新的入拍位（不吃陈旧冻结值）。"""
    pl = LateralPlanner(P)
    pl.state.executed_lane = 1.0
    pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                   fid=1, valid=1), DT, 1)
    assert pl._conserve_hold == pytest.approx(1.0)
    pl.update(_out(state=DecisionState.CRUISE, x_target=0.0,
                   fid=2, valid=2), DT, 2)
    assert pl._conserve_hold is None
    pl.state.executed_lane = -0.5
    pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                   fid=3, valid=3), DT, 3)
    assert pl._conserve_hold == pytest.approx(-0.5)


def test_conserve_with_road_obs_centers_to_road():
    """CONSERVE+有 ro：直连路中心——ro 是直接观测，不经车道参考系。
    13:29 局：参考系陈旧时 hold(−0.13) 与 ro(+1.9) 打架、杆恒 0 蹭右墙 5.5s。
    无 ro 拍退回入拍 hold（c1512bf 语义，直道无观测平衡点仍是归零）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.state.executed_lane = -0.13
    c = pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                       fid=1, valid=1), DT, 1, road_offset=1.5)
    assert c.steer_x < 0                          # 车在路右 1.5 → 向左回中心
    c2 = pl.update(_out(state=DecisionState.CONSERVE, x_target=None,
                        fid=2, valid=2), DT, 2)   # 断供拍：hold=−0.13，exec 未动
    assert c2.steer_x == 0


def test_fault_forces_zero():
    """FAULT=几何不可信，归零直开是诚实兜底（与 CONSERVE 的保持语义相对）。"""
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)
    for i in range(30):
        cmd = pl.update(_out(state=DecisionState.FAULT, x_target=1.0,
                             fid=100 + i, valid=100 + i), DT, 100 + i)
    assert cmd.steer_x == 0


# ---------- 重锚（裁定 1 + 契约细节 1） ----------

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
    cmd = pl.update(_out(state=DecisionState.ABORT, x_target=0.0,
                         fid=300, valid=300), DT, 300)
    assert cmd.steer_x >= 0                       # 不向左打回 0
    # 持续 ABORT：跟踪冻结位（死区内收力），不随 x_target=0 漂移
    for i in range(1, 10):
        pl.update(_out(state=DecisionState.ABORT, x_target=0.0,
                       fid=300 + i, valid=300 + i), DT, 300 + i)
    assert frozen <= pl.state.executed_lane <= frozen + P.hold_deadband_lane
    # 离开 ABORT 后恢复正常跟踪
    pl.update(_out(state=DecisionState.CRUISE, x_target=0.0,
                   fid=400, valid=400), DT, 400)
    assert pl._abort_hold_lane is None


# ---------- 过期保持与优先级（裁定 3 + 契约细节 2） ----------

def test_expired_hold_then_conserve_at_plus_one():
    pl = LateralPlanner(P)
    _run_moving(pl, 1.0, ticks=5)
    held = pl._last_raw
    assert held > 0.4 * P.k_p * 32767             # 大杆移动中（阈值随 k_p 定标）
    # 过期第 1..hold_max_ticks 拍：杆值保持（steer_raw=当前 steer_norm，输出不反打）
    for i in range(1, P.hold_max_ticks + 1):
        cmd = pl.update(_out(x_target=1.0, fid=10 + i, valid=9), DT, 10 + i)
        assert cmd.steer_x > 0.4 * P.k_p * 32767  # 保持方向与幅度（允许限幅缓变）
    # 第 hold_max_ticks+1 拍起按 CONSERVE：明确向 0 走
    cmd = pl.update(_out(x_target=1.0, fid=99, valid=9), DT, 99)
    assert cmd.steer_x < held


def test_conserve_priority_over_expired_hold():
    """CONSERVE/FAULT 的接管优先级高于过期保持（维护者裁定 3；CONSERVE 的接管
    内容=入拍车道保持（13:02 局语义修正），过期保持的"杆值不动"让位于它）。"""
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
    # CRUISE=维护态：u 经 hold_stick_max 封顶（16:44 局后），e=1.0→min(k_p, cap)
    u0 = min(1.0, P.k_p, P.hold_stick_max)
    assert pl1.state.steer_norm == pytest.approx(a1 * u0, rel=1e-6)
    assert pl2.state.steer_norm == pytest.approx(a2 * u0, rel=1e-6)


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
    # 隔离输出链边界：无 lookahead/阻尼/低通/限幅，且横向加速度增益置零
    # （executed 不漂移，PD 稳态恒等于 k_p·x_target）；维护权限全放开，
    # 死区/封顶归上一节测，这里只测杆值输出链的 255/256 边界
    pl = _planner(k_d=0.0, lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, a_lat_gain=0.0,
                  hold_deadband_lane=0.0, hold_stick_max=1.0)
    cmd = None
    for i in range(5):
        cmd = pl.update(_out(x_target=x_target, fid=i, valid=i), DT, i)
    assert (cmd.steer_x == 0) is expect_zero


# ---------- 双积分运动学（v2，2026-09-22 真机证据链：杆是航向指令不是平移速度指令） ----------

def _iso_planner(**over) -> LateralPlanner:
    """隔离运动学的参数组合：杆一拍到位、无限幅死区、PD 不饱和（目标远置）、
    无维护权限封顶（测的是 plant 签名，不是 16:44 后的 gentle 通道）。"""
    base = dict(lookahead_tau_s=0.0, k_d=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                hold_deadband_lane=0.0, hold_stick_max=1.0)
    base.update(over)
    return _planner(**base)


def test_sustained_stick_is_convex_double_integrator():
    """满杆按住：位移增量逐拍**递增**（x∝t² 的差分签名）。
    被证伪的单积分模型（杆→稳态速度）在惯性爬升后增量走平——方向相反。"""
    pl = _iso_planner(a_lat_gain=6.0, tau_align_s=1e9, v_lat_max=1e9)
    xs = []
    for i in range(10):
        pl.update(_out(x_target=100.0, fid=i + 1, valid=i + 1), DT, i + 1)
        xs.append(pl.state.executed_lane)
    d1 = [xs[k + 1] - xs[k] for k in range(len(xs) - 1)]
    assert all(d1[k + 1] > d1[k] for k in range(len(d1) - 1))   # 凸（加速中）
    assert d1[0] > 0 and all(d > 0 for d in d1)
    # 双积分的干净签名：恒杆下**二阶差分恒定 = a·dt²**（单积分会走平=0）
    d2 = [d1[k + 1] - d1[k] for k in range(len(d1) - 1)]
    for s in d2:
        assert s == pytest.approx(6.0 * DT * DT, rel=1e-6)


def test_release_decays_and_settles():
    """回正语义：杆归零后 v_lat 经 tau_align 指数衰减、位移收敛（定值 =v0·τ），
    不是无限漂移也不是瞬时停住。"""
    pl = _iso_planner(a_lat_gain=0.0, tau_align_s=0.5, v_lat_max=1e9)
    pl.state.v_lat_est = 2.0
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1)   # a=0：杆不注入
    assert pl.state.v_lat_est == pytest.approx(2.0 * (1 - DT / 0.5), rel=1e-9)
    for i in range(2, 400):
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i)
    assert pl.state.v_lat_est == pytest.approx(0.0, abs=1e-9)
    # Euler 几何收敛的闭式：Σ v0·(1−dt/τ)^k·dt = v0·τ·(1−dt/τ) = 2.0·0.5·0.9
    assert pl.state.executed_lane == pytest.approx(2.0 * 0.5 * (1 - DT / 0.5),
                                                   abs=1e-6)


def test_v_lat_saturates_no_runaway():
    """持续满杆：v_lat 被 v_lat_max 定圆饱和钉住，位移退化为线性（双积分防发散）。"""
    pl = _iso_planner(a_lat_gain=6.0, tau_align_s=1e9, v_lat_max=1.0)
    for i in range(50):
        pl.update(_out(x_target=100.0, fid=i + 1, valid=i + 1), DT, i + 1)
    assert pl.state.v_lat_est == pytest.approx(1.0)
    x_prev = pl.state.executed_lane
    pl.update(_out(x_target=100.0, fid=52, valid=52), DT, 52)
    assert pl.state.executed_lane - x_prev == pytest.approx(1.0 * DT, rel=1e-6)


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


# ---------- 路中心连续重锚（step 2.5：开环虚胖的闭环解） ----------

def test_road_reanchor_pins_inflated_model():
    """模型虚胖（a_lat_gain 偏大）跑过头：有路观测时 executed 被钉回观测值，
    误差常驻→杆不提前松。对照组：无观测（road_offset=None）executed 一路冲高。"""
    pl_open = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                       rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                       v_lat_max=1e9, tau_align_s=1e9)   # 纯积分，无回正
    pl_obs = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                      rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                      v_lat_max=1e9, tau_align_s=1e9)
    open_x, obs_x = [], []
    for i in range(1, 12):
        d = _out(x_target=1.0, fid=i, valid=i)
        open_x.append(pl_open.update(d, DT, i).steer_x)
        # 车其实纹丝不动：路观测恒 0（锚懒定为首帧 0→obs 恒 0）
        c = pl_obs.update(d, DT, i, road_offset=0.0)
        obs_x.append(c.steer_x)
    # 无观测：模型冲到接近目标→杆回落到 0（提前松杆，V2 病灶；阈值含维护杆幅
    # 封顶后的爬升减速）
    assert pl_open.state.executed_lane > 0.35
    assert abs(open_x[-1]) < abs(open_x[3])
    # 有观测：executed 被钉在 0 附近，杆持续（误差在，杆就在；幅度受维护
    # 杆幅上限约束——16:44 局后 CRUISE 不再满舵）
    assert abs(pl_obs.state.executed_lane) < 0.15
    assert abs(obs_x[-1]) > 10000
    assert abs(obs_x[-1]) <= int(P.hold_stick_max * _STICK_FULL) + 100
    assert abs(obs_x[-1]) >= abs(open_x[-1])


def test_road_reanchor_tracks_real_motion():
    """车真在右移（路观测递增）：executed 跟上，v_lat 学出正速度。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0,
                  obs_position_only=False)  # 本用例锁 α-β（新默认位置-only 另测）
    for i in range(1, 12):
        pl.update(_out(x_target=1.0, fid=i, valid=i), DT, i,
                  road_offset=0.05 * i)          # 每拍右移 0.05 车道
    assert pl.state.executed_lane == pytest.approx(0.05 * 11, abs=0.08)
    assert pl.state.v_lat_est > 0                # 学到正速度


def test_road_jump_rejected():
    """新息超 obs_jump_max_lane：坏检测，本拍弃观测，executed 不被拽飞。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0,
                  obs_jump_max_lane=1.5)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)  # 锚定
    before = pl.state.executed_lane
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=5.0)  # 跳变
    assert pl.state.executed_lane == pytest.approx(before, abs=1e-9)


def test_road_persistent_offset_rebases_after_confirm():
    """持续大新息=参考系跳变：同向同量级连续 obs_jump_confirm_ticks 拍后
    **一步重基**到观测，不再按 α 逐拍追（旧实现在确认后 0.3s 内目标用新帧、
    位置用旧帧，杆继续往错误方向打）。单帧坏检测与断续大新息仍被拒。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0,
                  obs_jump_max_lane=0.8)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    before = pl.state.executed_lane
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=-1.7)
    assert pl.state.executed_lane == pytest.approx(before, abs=1e-9)
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3, road_offset=0.0)
    assert pl.state.executed_lane == pytest.approx(before, abs=1e-9)
    n = P.obs_jump_confirm_ticks
    for i in range(4, 4 + n - 1):                    # 连续 n-1 拍仍拒
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i, road_offset=-1.7)
    assert pl.state.executed_lane == pytest.approx(before, abs=1e-9)
    pl.update(_out(x_target=0.0, fid=4 + n - 1, valid=4 + n - 1), DT,
              4 + n - 1, road_offset=-1.7)           # 第 n 拍：确认并重基
    assert pl.state.executed_lane == pytest.approx(-1.7, abs=1e-6)


def test_hold_offset_is_not_a_position_observation():
    """历史保持值（memory/wall_occl/pair_slot）不是本拍测量：不形成参考系、
    不收 α。保持期模型自走；脏计时照常推进但锚点保留（下一次新鲜大读数
    直接走陈旧重基，不必再等形成门 1s）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.5, road_offset_hold=True)
    assert pl._road_anchor is None                   # 保持值不定义参考系
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=0.0)
    assert pl._road_anchor == 0.0
    pl.state.executed_lane = 0.4
    pl.state.v_lat_est = 0.0
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=1.6, road_offset_hold=True)
    assert pl.state.executed_lane == pytest.approx(0.4, abs=1e-9)   # 无 α 拉回
    n_budget = int(P.anchor_stale_s / DT)
    for i in range(4, 4 + n_budget):
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i,
                  road_offset=1.6, road_offset_hold=True)
    assert pl._road_anchor == 0.0                    # 几何仍在，锚不丢
    # 保持超预算后的第一拍新鲜大读数：陈旧判据立即重基，不等确认/形成门
    pl.update(_out(x_target=0.0, fid=4 + n_budget, valid=4 + n_budget), DT,
              4 + n_budget, road_offset=1.6)
    assert pl.state.executed_lane == pytest.approx(1.6, abs=1e-6)


def test_hold_offset_does_not_reset_stale_clock():
    """保持拍不得把陈旧计时清零：否则 memory 长保持会让锚永不超时，
    模型漂走后仍按旧帧打杆。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl._stale_t = P.anchor_stale_s - DT
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=0.0, road_offset_hold=True)
    assert pl._stale_t >= P.anchor_stale_s


# ---------- 保持期车道线融合（2026-10-25） ----------

def test_lane_fusion_tracks_motion_during_hold():
    """深度保持期用车道线连续横位补运动：车贴墙不动时模型会按打杆量虚走，
    线不会（p2 496~537：线 −1.9 不动，模型从 −1.0 漂到 −0.24）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    for i in range(10):
        pl.update(_out(x_target=0.0, fid=2 + i, valid=2 + i), DT, 2 + i,
                  road_offset=0.0, road_offset_hold=True,
                  lane_x=-0.1 * (i + 1), lane_ok=True)
    assert pl.state.executed_lane < -0.4
    assert pl.state.executed_lane > -1.1       # 平滑+收敛滞后，不外推


def test_lane_fusion_rejects_depth_outlier():
    """深度单侧反推错 0.7 道、线保持原位：深度拍照收 α（供数门已放行），
    帧偏置不更新；随后保持期按线把位置拉回，不带错值。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=0.7, lane_x=0.0, lane_ok=True)     # 错值进 α
    assert pl.state.executed_lane == pytest.approx(P.obs_alpha * 0.7, abs=1e-6)
    for i in range(15):
        pl.update(_out(x_target=0.0, fid=3 + i, valid=3 + i), DT, 3 + i,
                  road_offset=0.7, road_offset_hold=True,
                  lane_x=0.0, lane_ok=True)
    assert abs(pl.state.executed_lane) < 0.05


def test_lane_jump_disables_fusion_until_depth_agrees():
    """线自身跳 1 道 = 换线/重锁：重置并停用融合（不许把旧帧位置拽到新线），
    等新鲜深度与线一致（重基拍）后再启用。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=0.0, road_offset_hold=True,
              lane_x=1.2, lane_ok=True)                 # 线跳 1.2 道
    assert pl._lane_trusted is False
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=0.0, road_offset_hold=True,
              lane_x=1.2, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-9)
    for i in range(P.obs_jump_confirm_ticks):           # 深度同样报 1.2：重基
        pl.update(_out(x_target=0.0, fid=4 + i, valid=4 + i), DT, 4 + i,
                  road_offset=1.2, lane_x=1.2, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(1.2, abs=1e-6)
    assert pl._lane_trusted is True
    pl.update(_out(x_target=0.0, fid=20, valid=20), DT, 20,
              road_offset=1.2, road_offset_hold=True,
              lane_x=1.3, lane_ok=True)
    assert pl.state.executed_lane > 1.201


def test_lane_fusion_rebias_on_rejected_fresh_depth():
    """深度被跳变门拒绝时也要校准线帧（p1 206 的坑）：深度 +1.87 被拒、
    线 +1.32 差 0.55 道——偏置应更新，随后保持期融合把状态拉回真值。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=0.0, road_offset_hold=True,
              lane_x=1.3, lane_ok=True)              # 线跳 1.3：重置+停用
    assert pl._lane_trusted is False
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=1.87, lane_x=1.3, lane_ok=True)   # 深度被跳变门拒
    assert pl.state.executed_lane < 1.0             # 本拍不重基
    assert pl._lane_trusted is True                 # 但帧偏置已校准
    for i in range(12):
        pl.update(_out(x_target=0.0, fid=4 + i, valid=4 + i), DT, 4 + i,
                  road_offset=1.87, road_offset_hold=True,
                  lane_x=1.3, lane_ok=True)
    assert pl.state.executed_lane > 1.5


def test_lane_relock_resets_smoothing():
    """重锁后的第一条锁线可能换了线/换了帧：平滑值直接重置为线值并停用
    融合（0.6 道的换线在 0.8 跳变门内，不能靠跳变阈值发现）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=0.0, road_offset_hold=True,
              lane_x=1.3, lane_ok=False, lane_relock=True)
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=0.0, road_offset_hold=True,
              lane_x=0.6, lane_ok=True)
    assert pl._lane_sm == pytest.approx(0.6, abs=1e-9)
    assert pl._lane_trusted is False
    for i in range(5):                              # 未对账：不融合
        pl.update(_out(x_target=0.0, fid=4 + i, valid=4 + i), DT, 4 + i,
                  road_offset=0.0, road_offset_hold=True,
                  lane_x=0.6, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-9)


def test_corroborated_edge_frame_forms_anchor():
    """|ro|>形成门 但深度与锁线一致：允许建锚并对齐位置（断供丢锚后的
    贴边段原本会一直等形成门/饥饿重基，状态自由漂 2s+）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=1.87, lane_x=1.3, lane_ok=True)
    assert pl._road_anchor == 0.0
    assert pl.state.executed_lane == pytest.approx(1.87, abs=1e-6)
    assert pl._lane_trusted is True


def test_edge_frame_needs_lane_agreement():
    """贴边拍没有锁线佐证时仍守形成门（防垃圾墙定义参考系）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=1.87, lane_x=0.3, lane_ok=True)   # 线差 1.57 道
    assert pl._road_anchor is None
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-9)


def test_lane_fusion_off_without_lock():
    """无锁线（COAST）不融合：退回模型，保持值不得把位置拽到线读数。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    for i in range(10):
        pl.update(_out(x_target=0.0, fid=2 + i, valid=2 + i), DT, 2 + i,
                  road_offset=0.0, road_offset_hold=True,
                  lane_x=1.0, lane_ok=False)
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-9)


def test_starved_frame_rebases_to_road():
    """形成门饿死超时也重基（13:29 局开局：车在左缘 ro=−2.9，形成门拒定锚
    7s、hold:0=杆恒 0 蹭墙）。预算内不定锚不打杆；超时重基 exec:=ro、道0=路中心，
    PD 把车拉回带内。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    n_budget = int(P.anchor_stale_s / DT) - 1
    for i in range(1, n_budget + 1):
        c = pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i, road_offset=-2.9)
        assert pl._road_anchor is None
        assert c.steer_x == 0
    pl.update(_out(x_target=0.0, fid=n_budget + 1, valid=n_budget + 1), DT,
              n_budget + 1, road_offset=-2.9)      # 超预算：重基
    assert pl._road_anchor == 0.0
    assert pl.state.executed_lane == pytest.approx(-2.9)
    c = pl.update(_out(x_target=0.0, fid=n_budget + 2, valid=n_budget + 2), DT,
                  n_budget + 2, road_offset=-2.9)
    assert c.steer_x > 0                           # 车在 −2.9、道0=中心 → 向右回


def test_stale_rejected_corrections_rebase():
    """时间判据陈旧重基（13:29 死亡螺旋：ro=+1.9 全程、exec 冻结 −0.13、0 杆
    5.5s 怼右墙——旧"连续 40 次喂入拒绝"判据只攒到 31 次，永不触发）。
    持续偏移的常规出口是确认放行（收 α 修正，exec 逼近 ro）；超时重基仍保留
    作兜底。重基/收敛后道0=路中心，闭环一拍复活。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.5)  # 锚=0（道0=路心）
    n = int(P.anchor_stale_s / DT)
    for i in range(2, n + 2):                      # ro=+1.9 喂 1s：r≈1.4+
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i, road_offset=1.9)
    assert pl._road_anchor == 0.0
    assert pl.state.executed_lane == pytest.approx(1.9, abs=0.05)
    c = pl.update(_out(x_target=0.0, fid=99, valid=99), DT, 99, road_offset=1.9)
    assert c.steer_x < 0                           # 道0=中心，车在 +1.9 → 向左回


def test_blackout_voids_frame():
    """断供超预算=帧作废（黑视本身是漂移温床）：旧锚不得去拽新路上的车；
    作废后按中带观测重形成——道0=路中心（中轴巡航语义，anchor=0）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    n = int(P.anchor_stale_s / DT)
    for i in range(2, n + 2):                      # 全程 ro=None
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i)
    assert pl._road_anchor is None
    pl.update(_out(x_target=0.0, fid=90, valid=90), DT, 90, road_offset=0.4)
    assert pl._road_anchor == 0.0                  # 重形成：道0=路中心


def test_center_axis_cruise_converges_to_road_center():
    """中轴巡航（2026-10-02 维护者裁定）：x_target=0 且车不在路心时，路观测
    把 executed 收敛到 road_offset（自车相对路心）——道0=路中心，PD 持续
    回轴。对照旧语义（锚=ro−exec）：executed 钉在形成时刻车位、车位即路径。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    for i in range(1, 20):
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i, road_offset=0.6)
    assert pl.state.executed_lane == pytest.approx(0.6, abs=0.05)  # 道上=路心


# ---------- 维护性转向权限（16:44 局：定中心全权 PD=满舵绕桩） ----------

def test_hold_authority_deadband_and_cap():
    """保持态（CRUISE 定中心）：死区内不发力、死区外杆幅封顶（2026-10-18 换血：
    场跟随无 CHANGE 全权分支，gentle 语义无条件生效）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.state.executed_lane = 0.3                       # 误差 0.3 < 死区 0.5
    c = pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1)
    assert c.steer_x == 0                              # 不发力（旧语义会持续拽）
    pl.state.executed_lane = 1.5                       # 误差 1.5：k_p0.7→u=1.05
    c = pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert -int(P.hold_stick_max * _STICK_FULL) - 100 < c.steer_x < 0  # 封顶 0.45


# ---------- 机动态全权（阶段 2，2026-10-18） ----------

def test_maneuver_full_authority_moves_one_lane():
    """maneuver=True：越过 0.45 维护杆幅上限，20 拍（1s）内位移 >0.3 道。"""
    pl = _planner()
    max_stick = 0
    for i in range(1, 21):
        cmd = pl.update(_out(x_target=1.0, fid=i, valid=i, maneuver=True),
                        DT, i)
        max_stick = max(max_stick, abs(cmd.steer_x))
    assert max_stick > int(0.45 * _STICK_FULL)
    assert pl.state.executed_lane > 0.3


def test_maneuver_deadband_and_cap_used():
    """机动态用 maneuver_deadband_lane/maneuver_stick_max：误差 0.1 道
    （低于保持死区 0.5、高于机动态死区 0.05）仍发力。"""
    pl = _planner(maneuver_deadband_lane=0.05, maneuver_stick_max=0.85)
    cmd = pl.update(_out(x_target=0.1, fid=1, valid=1, maneuver=True), DT, 1)
    assert cmd.steer_x > 0


# ---------- ⑥b 速度修正时间基（2026-10-08 判据冻结，台账 §4A） ----------

def test_beta_denominator_is_evidence_interval():
    """稀疏新证据（驻留协议产线节奏 ~200ms 一套）：β 注入分母=自上次速度
    修正以来模型跑过的累计帧钟（Δt_e），不是控制拍 dt——r 的速度承载项是
    (u−v)·Δt_e，按拍间隔归账曾放大 4~6×（v_lat_est ±1 道/s 拍级振荡的
    机制根因）。复用拍只收位置、不重置累计器。"""
    pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=1e9,
                  lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  obs_position_only=False,
                  obs_beta_dv_rate_lane_s2=0.0)   # 隔离时间基（限速另测）
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    for i in range(2, 6):          # 4 个复用拍：位置修正照跑、速度不修正
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i,
                  road_offset=0.0, road_offset_new=False)
    assert pl.state.v_lat_est == 0.0
    pl.update(_out(x_target=0.0, fid=6, valid=6), DT, 6, road_offset=0.5)
    # T_e = 5·DT（锚形成后 5 拍无速度修正）：β·0.5/0.25 = 0.4
    # （旧口径 β·0.5/0.05 = 2.0——本断言在旧代码下红）
    assert pl.state.v_lat_est == pytest.approx(P.obs_beta * 0.5 / (5 * DT))


def test_beta_accumulator_survives_reuse_ticks():
    """复用拍位置修正（α·r 照跑）不重置速度修正累计器——速度承载项按完整
    新证据间隔归账；逐拍新证据的锚点值不变（T_e=dt，旧数学逐位保留）。"""
    pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=1e9,
                  lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  obs_position_only=False,
                  obs_beta_dv_rate_lane_s2=0.0)   # 隔离时间基（限速另测）
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=0.4)
    assert pl.state.v_lat_est == pytest.approx(P.obs_beta * 0.4 / DT)  # T_e=DT
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=0.4, road_offset_new=False)                  # 复用拍
    # 注入干净基线（本文件既有手法）隔离单变量：复用拍之后新证据的 T_e
    # 须含复用拍——否则说明复用错误地清了累计器
    pl.state.executed_lane = 0.0
    pl.state.v_lat_est = 0.0
    pl.update(_out(x_target=0.0, fid=4, valid=4), DT, 4, road_offset=0.4)
    assert pl.state.v_lat_est == pytest.approx(P.obs_beta * 0.4 / (2 * DT))


def test_obs_velocity_injection_respects_cap():
    """⑥b β 注入在 v_lat_max 饱和内（16:44 局 |v_lat| 冲到 11.4 ≫ 2.5，
    阻尼项 −k_d·v 随即满反打喂给极限环）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  a_lat_gain=0.0, tau_align_s=1e9,
                  obs_position_only=False,
                  obs_beta_dv_rate_lane_s2=0.0)      # 隔离 v_lat_max 饱和面
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)   # 锚=0
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=0.7)   # r=0.7
    # 裸 β 注入 = 0.2·0.7/0.05 = 2.8 > v_lat_max=2.5 → 必须被夹住
    assert pl.state.v_lat_est <= P.v_lat_max + 1e-9


# ---------- β 速度注入抑制（阶段 2，2026-10-18） ----------

def _beta_planner(**over):
    base = dict(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=1e9,
                lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                obs_position_only=False,
                obs_beta_deadband_lane=0.05, obs_beta_sign_agree=True,
                obs_beta_dv_rate_lane_s2=3.0)
    base.update(over)
    return _planner(**base)


def test_beta_correction_capped_by_dv_rate():
    pl = _beta_planner()
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.update(_out(fid=2, valid=2), DT, 2, road_offset=0.4)
    assert pl.state.v_lat_est == pytest.approx(3.0 * DT, rel=1e-6)


def test_beta_deadband_ignores_small_residual():
    pl = _beta_planner()
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.update(_out(fid=2, valid=2), DT, 2, road_offset=0.02)
    assert pl.state.v_lat_est == pytest.approx(0.0)


def test_beta_requires_sign_agreement():
    pl = _beta_planner()
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)     # 锚
    pl.update(_out(fid=2, valid=2), DT, 2, road_offset=0.4)     # 正向修正
    v1 = pl.state.v_lat_est
    assert v1 > 0
    pl.update(_out(fid=3, valid=3), DT, 3, road_offset=-0.4)    # 反号：只做位置
    assert pl.state.v_lat_est == pytest.approx(v1)
    pl.update(_out(fid=4, valid=4), DT, 4, road_offset=-0.4)    # 连续同号：恢复注入
    assert pl.state.v_lat_est < v1


def test_v_lat_never_saturates_under_alternating_offsets():
    pl = _beta_planner(v_lat_max=2.5)
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    vmax = 0.0
    for i in range(2, 60):
        ro = 0.5 if i % 2 else -0.5
        pl.update(_out(fid=i, valid=i), DT, i, road_offset=ro)
        vmax = max(vmax, abs(pl.state.v_lat_est))
    assert vmax < 2.5 - 1e-6


def test_position_only_never_injects_velocity():
    """新默认：路心观测只修位置（α），不把噪声注入 v_lat——交替 ±0.5 道
    跑 60 拍，v_lat 始终保持 0（a_lat_gain=0、无模型驱动）。"""
    pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=2.5,
                  lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    assert pl.p.obs_position_only is True
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    for i in range(2, 60):
        pl.update(_out(fid=i, valid=i), DT, i,
                  road_offset=0.5 if i % 2 else -0.5)
        assert abs(pl.state.v_lat_est) < 1e-9


def test_no_road_offset_is_old_behavior():
    """road_offset=None 逐拍=纯模型积分（旧行为不变，兼容红线）。"""
    pl_a = _planner()
    pl_b = _planner()
    for i in range(1, 10):
        d = _out(x_target=0.7, fid=i, valid=i)
        ca = pl_a.update(d, DT, i)                # 默认 None
        cb = pl_b.update(d, DT, i, road_offset=None)
        assert ca.steer_x == cb.steer_x
    assert pl_a.state.executed_lane == pytest.approx(pl_b.state.executed_lane)


def test_velocity_correction_only_on_fresh_reading():
    """β 速度修正只认新证据拍，复用拍只收位置（α）——异步驻留协议下同一套
    旧读数不得反复当速度新证据（重复记账曾把 2026-10-06 局 id17 的 0.36 道
    找边翻跳 3 拍内放大成 v_lat 饱和 ±2.5，lat_veto 被假侧滑触发）。

    锚定后喂一次 +0.2 道新鲜跳变（一次踢足 β·r/dt≈2.0，v_lat_max=2.5 真轨
    之内）。控制律摘除（a_lat_gain=0、无自回正）只留 ⑥b 动力学——否则 PD
    会在小跳变下把放大现象压没；随后 3 拍同值复用：fresh_only 序列 |v_lat|
    只准不增（无重复注入）；对照旧行为（复用拍 road_offset_new=True 直喂）
    同一新息被重复注入、|v_lat| 反超新鲜拍峰值=放大现象（id17 机理）。"""
    def run(new_on_reuse, legacy=False):
        over = {}
        if legacy:   # 对照=阶段 2 之前的裸 β（无死区/无符号门/无限速）
            over = dict(obs_beta_deadband_lane=0.0, obs_beta_sign_agree=False)
        pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=2.5,
                      lookahead_tau_s=0.0, tau_steer_s=1e-9,
                      rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                      obs_position_only=False,
                      obs_beta_dv_rate_lane_s2=0.0, **over)
        pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)   # 锚=0（道0=路心）
        pl.update(_out(fid=2, valid=2), DT, 2, road_offset=0.2,
                  road_offset_new=True)                            # 新鲜跳变
        vs = [pl.state.v_lat_est]
        for i in range(3):                                         # 驻留复用 3 拍
            pl.update(_out(fid=3 + i, valid=3 + i), DT, 3 + i,
                      road_offset=0.2, road_offset_new=new_on_reuse)
            vs.append(pl.state.v_lat_est)
        return vs

    fresh_only = run(False)
    assert abs(fresh_only[0]) > 1.5            # 新鲜拍一次踢足（β·r/dt=2.0）
    assert all(abs(fresh_only[k + 1]) <= abs(fresh_only[k]) + 1e-9
               for k in range(3))              # 复用拍：只衰减，无重复注入
    old = run(True, legacy=True)
    assert max(abs(v) for v in old[1:]) > abs(fresh_only[0]) + 0.3


def test_reuse_ticks_still_converge_position():
    """复用拍 α 位置修正不缺席：跳变后同值复用数拍，executed 应向 0.4 收敛
    （β 停了，位置参考还在——保鲜槽语义=维持位置参考）。"""
    pl = _planner()
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.update(_out(fid=2, valid=2), DT, 2, road_offset=0.4, road_offset_new=True)
    for i in range(6):
        pl.update(_out(fid=3 + i, valid=3 + i), DT, 3 + i,
                  road_offset=0.4, road_offset_new=False)
    assert abs(pl.state.executed_lane - 0.4) < 0.2


# ---------- 航向横速观测（模型状态之外的融合输入） ----------

def _feed_weave(obs, seconds=2.0, v_lane=1.0, theta=0.05):
    """合成来回横移：x 斜率与线角同号，避免慢基线把常值航向吸收掉。"""
    x, t, fid = 0.0, 0.0, 1
    while t < seconds:
        sign = 1.0 if int(t / 0.5) % 2 == 0 else -1.0
        x += sign * v_lane * 0.1
        t += 0.1
        obs.update(0.1, x, True, sign * theta, True, fid, 60.0)
        fid += 1


def test_heading_observer_estimates_speed_and_velocity():
    obs = HeadingObserver(P)
    _feed_weave(obs)
    assert abs(obs.v_line) == pytest.approx(1.0, abs=0.25)
    # 前向速度由线速/线角自标定：量级在赛车档（30~120 m/s），不做精确钉值
    # （合成方波起步时慢基线尚未居中，绝对值随相位摆动）。
    assert 30.0 < obs.speed_mps < 120.0
    assert obs.v_head is not None
    assert (obs.v_head > 0) == (obs.v_line > 0)
    assert 0.4 < abs(obs.v_head) < 1.6


def test_heading_fuse_gain_zero_returns_model_and_half_blends():
    obs = HeadingObserver(P)
    obs.v_head = 1.0
    obs._last_seen = obs._t
    assert obs.fused(0.4, 0.0) == 0.4
    assert obs.fused(0.4, 0.5) == pytest.approx(0.7)


def test_heading_observer_falls_back_when_stale_or_bad():
    obs = HeadingObserver(P)
    _feed_weave(obs)
    assert obs.v_head is not None
    for _ in range(8):                 # 0.8s 无线：超过 0.5s TTL
        obs.update(0.1, None, False, None, False, None, None)
    assert obs.v_head is None
    assert obs.fused(0.4, 0.5) == 0.4
    # 线在锁但航向龄超限：不更新观测
    obs2 = HeadingObserver(P)
    _feed_weave(obs2)
    before = obs2.v_head
    obs2.update(0.1, 99.0, True, 0.05, True, 999, 400.0)
    assert obs2.v_head == before


def test_heading_fuse_opposes_motion_in_control_law():
    """融合横速进入控制律：同样位置误差下，正航向横速产生负（反向）杆值。"""
    cmd = {}
    for gain in (0.0, 0.5):
        pl = _planner(heading_fuse_gain=gain)
        pl.head.v_head = 1.0
        pl.head._last_seen = pl.head._t
        c = pl.update(_out(x_target=0.0, maneuver=True, fid=1, valid=1),
                      DT, 1)
        cmd[gain] = c.steer_x
    assert cmd[0.0] == 0
    assert cmd[0.5] < 0
