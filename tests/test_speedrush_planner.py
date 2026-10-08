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
from maaracing_master.plugins.speedrush.planner import _STICK_FULL, LateralPlanner
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
    # 持续 ABORT：跟踪冻结位（死区内收力），不随 x_target=0 漂移
    for i in range(1, 10):
        pl.update(_out(state=DecisionState.ABORT_CHANGE, x_target=0.0,
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
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)  # 模型不动，全靠观测
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
    重基后道0=路中心，闭环一拍复活。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.5)  # 锚=0（道0=路心）
    n = int(P.anchor_stale_s / DT)
    for i in range(2, n + 2):                      # ro=+1.9 喂 1s：r≈1.4+ 全拒
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i, road_offset=1.9)
    assert pl._road_anchor == 0.0
    assert pl.state.executed_lane == pytest.approx(1.9)
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


def test_reanchor_event_resets_road_frame():
    """事件重锚（金币完成）后路观测参考重形成：executed 向路心收敛（中轴
    巡航语义）——事件拍的 executed 是决策系快照，路观测把它拉回道0=路中心
    的参考系，此为期望行为而非"旧帧拽新值"。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.3)
    assert pl._road_anchor == 0.0                  # 形成即道0=路中心
    pl.update(_out(x_target=0.0, fid=2, valid=2, reanchor=1.0), DT, 2,
              road_offset=0.3)                    # 重锚拍：executed:=1.0，路锚重置
    assert pl.state.executed_lane == pytest.approx(1.0, abs=1e-9)  # 重锚拍不被拽
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3, road_offset=0.3)
    assert pl._road_anchor == 0.0                  # 重形成
    assert pl.state.executed_lane == pytest.approx(1.0 + P.obs_alpha * (0.3 - 1.0))
    assert pl.state.executed_lane < 1.0            # 向路心收敛


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
    """保持态（CRUISE 定中心）：死区内不发力、死区外杆幅封顶；CHANGE 全权。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, tau_align_s=1e9, a_lat_gain=0.0)
    pl.state.executed_lane = 0.3                       # 误差 0.3 < 死区 0.5
    c = pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1)
    assert c.steer_x == 0                              # 不发力（旧语义会持续拽）
    pl.state.executed_lane = 1.5                       # 误差 1.5：k_p0.7→u=1.05
    c = pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert -int(P.hold_stick_max * _STICK_FULL) - 100 < c.steer_x < 0  # 封顶 0.45
    pl.state.executed_lane = 0.0                       # 同误差 CHANGE 机动：全权
    c = pl.update(_out(state=DecisionState.CHANGE, x_target=1.5,
                       fid=3, valid=3), DT, 3)
    assert c.steer_x > int(P.hold_stick_max * _STICK_FULL) + 100


# ---------- ⑥b 速度修正时间基（2026-10-08 判据冻结，台账 §4A） ----------

def test_beta_denominator_is_evidence_interval():
    """稀疏新证据（驻留协议产线节奏 ~200ms 一套）：β 注入分母=自上次速度
    修正以来模型跑过的累计帧钟（Δt_e），不是控制拍 dt——r 的速度承载项是
    (u−v)·Δt_e，按拍间隔归账曾放大 4~6×（v_lat_est ±1 道/s 拍级振荡的
    机制根因）。复用拍只收位置、不重置累计器。"""
    pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=1e9,
                  lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
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
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
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


def test_beta_accumulator_resets_on_reanchor():
    """重锚（v:=0）清零累计器：重锚同拍来新证据按单拍归账（T_e=dt，
    判据冻结的边界语义）；黑视期累计器继续走（模型在无修正地跑）。"""
    pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=1e9,
                  lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.update(_out(x_target=0.0, fid=2, valid=2, reanchor=0.0), DT, 2,
              road_offset=0.4)
    assert pl.state.v_lat_est == 0.0                       # 重锚拍 v 清零
    # 黑视 2 拍（ro=None）→ 新证据：T_e = 3·DT
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3)
    pl.update(_out(x_target=0.0, fid=4, valid=4), DT, 4)
    pl.update(_out(x_target=0.0, fid=5, valid=5), DT, 5, road_offset=0.4)
    assert pl.state.v_lat_est == pytest.approx(P.obs_beta * 0.4 / (3 * DT))


def test_obs_velocity_injection_respects_cap():
    """⑥b β 注入在 v_lat_max 饱和内（16:44 局 |v_lat| 冲到 11.4 ≫ 2.5，
    阻尼项 −k_d·v 随即满反打喂给极限环）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  a_lat_gain=0.0, tau_align_s=1e9)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)   # 锚=0
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=0.7)   # r=0.7
    # 裸 β 注入 = 0.2·0.7/0.05 = 2.8 > v_lat_max=2.5 → 必须被夹住
    assert pl.state.v_lat_est <= P.v_lat_max + 1e-9


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
    def run(new_on_reuse):
        pl = _planner(a_lat_gain=0.0, tau_align_s=1e9, v_lat_max=2.5,
                      lookahead_tau_s=0.0, tau_steer_s=1e-9,
                      rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
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
    old = run(True)
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
