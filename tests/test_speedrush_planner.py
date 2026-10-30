# -*- coding: utf-8 -*-
"""planner 层回归锁（planner 设计稿 v1 §九 step 1；维护者 2026-09-22 四裁定上锁）。

锁面：状态消费表全矩阵、重锚有符号且同拍生效、ABORT 冻结值归属、过期保持与
CONSERVE/FAULT 优先级、时间基低通、每 tick 限幅语义、死区边界 255/256、
恒油门、dt 非法 fail-loud、回放一致性（C3）。
"""

from __future__ import annotations

import math
import types
from dataclasses import replace

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


def test_law_targets_heading_then_stick():
    """控制律：位置误差 → 目标车头角 → 杆值（ψ 误差与角速度阻尼）。"""
    e, psi, r = 0.4, 0.02, 0.05
    psi_des = min(P.heading_max_rad, P.k_head_lane * e)
    assert law(e, psi, r, P) == pytest.approx(
        P.k_psi * (psi_des - psi) - P.k_r * r)
    # 朝向已到位且无角速度 → 不出杆
    assert law(e, P.k_head_lane * e, 0.0, P) == pytest.approx(0.0)
    # 维护态：目标朝向被压到 heading_hold_rad、杆幅受 hold_stick_max
    u = law(3.0, 0.0, 0.0, P, gentle=True, deadband=0.0)
    assert u == pytest.approx(min(P.hold_stick_max,
                                  P.k_psi * P.heading_hold_rad))
    # 死区：误差小于死区时不转向目标
    assert law(P.hold_deadband_lane * 0.5, 0.0, 0.0, P,
               gentle=True) == pytest.approx(0.0)


def test_step_is_heading_kinematics():
    """横移由朝向积分：x' = v·tanψ；朝向一阶接近指令。"""
    x, psi, v, dt = 0.3, 0.0, 45.0, 0.05
    x1, p1 = step(x, psi, 0.05, v, dt, P)
    a = 1 - math.exp(-dt / P.tau_heading_s)
    assert p1 == pytest.approx(a * 0.05, abs=1e-12)
    assert x1 == pytest.approx(x, abs=1e-12)      # 首拍 ψ 还很小
    # 朝向到位后的稳态横速 = v·tanψ/LANE_W
    for _ in range(80):
        x1, p1 = step(x1, p1, 0.05, v, dt, P)
    assert p1 == pytest.approx(0.05, abs=1e-3)
    # 无前向速度：不产生横移（物理事实，不是兜底）
    assert step(0.0, 0.1, 0.1, 0.0, dt, P) == (0.0, pytest.approx(0.1))

class _Rig:
    """闭环测试台：合成车（杆→航向→横移），planner 只从「标线角」看到朝向。

    生产里 ψ 是量出来的；测试没有相机，就用一台显式的合成车——它和产物走
    **同一条信息路**（lane_theta 进观测器、位置进 α 修正），所以测的是
    「控制器能不能把车开到目标」，不是「模型积分数值对不对」。
    """

    def __init__(self, pl: LateralPlanner, v_mps: float = 45.0,
                 tau: float = 0.25, k_yaw: float = 0.25):
        self.pl, self.v, self.tau, self.k_yaw = pl, v_mps, tau, k_yaw
        self.heading = 0.0
        self.x = 0.0
        self.fid = 1
        # 比例（≈车速）在本台架里是既定条件：给它一组自洽的累积量，
        # 免得把「刚起步还没估出比例」和「控制律对不对」混在一起测。
        pl.head._spd_den = 0.2
        pl.head._spd_num = 0.2 * v_mps / 3.5

    def tick(self, out, *, pos_obs: bool = True):
        cmd = self.pl.update(
            out, DT, current_fid=self.fid,
            road_offset=self.x if pos_obs else None,
            lane_x=self.x, lane_ok=True,
            lane_theta=self.heading, lane_theta_ok=True,
            lane_fid=self.fid, lane_age_ms=5.0, lane_n_locked=3)
        u = cmd.steer_x / _STICK_FULL
        a = 1.0 - math.exp(-DT / self.tau)
        self.heading += a * (self.k_yaw * u - self.heading)
        self.x += (self.v * math.tan(self.heading) / 3.5) * DT
        self.fid += 1
        return cmd


def _run_moving(pl: LateralPlanner, target: float, ticks: int = 3, fid0: int = 1):
    """瞬态：闭环跑几拍（合成车载着车动起来），返回最后一拍命令。"""
    rig = _Rig(pl)
    cmd = None
    for i in range(ticks):
        cmd = rig.tick(_out(x_target=target, fid=fid0 + i, valid=fid0 + i))
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
    """CONSERVE=保持入拍车道：变道中被保守打断 → 反打止漂，停在入拍车道附近。"""
    pl = _planner()
    rig = _Rig(pl)
    for i in range(20):
        rig.tick(_out(x_target=1.0, fid=i + 1, valid=i + 1, maneuver=True))
    entry = rig.x
    assert entry > 0.3
    for i in range(120):
        rig.tick(_out(state=DecisionState.CONSERVE, x_target=None,
                      fid=40 + i, valid=40 + i))
    assert abs(rig.x - entry) < 0.25          # 停在入拍道附近，不回 0

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
                  v_lat_max=1e9, a_lat_gain=0.0)
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
    """ABORT 冻结的是入拍实测位置，不消费决策层 x_target（其 ABORT 路径回落 0）。"""
    pl = _planner()
    rig = _Rig(pl)
    for i in range(20):
        rig.tick(_out(x_target=1.0, fid=i + 1, valid=i + 1, maneuver=True))
    entry = rig.x
    assert entry > 0.3
    for i in range(120):
        rig.tick(_out(state=DecisionState.ABORT, x_target=0.0,
                      fid=40 + i, valid=40 + i))
    assert abs(rig.x - entry) < 0.25          # 停在冻结位，不被拉回 0

def test_expired_hold_then_conserve_at_plus_one():
    """过期保持：杆值不动；第 hold_max_ticks+1 拍起按 CONSERVE 接管。"""
    pl = _planner()
    rig = _Rig(pl)
    held = None
    for i in range(20):
        cmd = rig.tick(_out(x_target=1.0, fid=i + 1, valid=i + 1, maneuver=True))
    held = cmd.steer_x
    assert held > 0
    for i in range(1, P.hold_max_ticks + 1):
        cmd = rig.tick(_out(x_target=1.0, fid=40 + i, valid=39))
        assert cmd.steer_x == held            # 过期保持：杆值不动
    cmd = rig.tick(_out(x_target=1.0, fid=99, valid=39))
    assert cmd.steer_x != held                # CONSERVE 接管

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
    def one(dt):
        pl = LateralPlanner(P)
        pl.update(_out(x_target=1.0, fid=1, valid=1), dt, 1)
        return pl.state.steer_norm
    a1 = 1 - math.exp(-0.05 / P.tau_steer_s)
    a2 = 1 - math.exp(-0.10 / P.tau_steer_s)
    # 无朝向证据时的比例段：u = min(k_psi·k_head·e, hold_stick_max)
    u0 = min(P.k_psi * P.k_head_lane, P.hold_stick_max)
    assert one(0.05) == pytest.approx(a1 * u0, rel=1e-6)
    assert one(0.10) == pytest.approx(a2 * u0, rel=1e-6)

def test_rate_limit_per_tick_semantics():
    pl = LateralPlanner(P)
    prev = 0
    for i in range(6):
        cmd = pl.update(_out(x_target=1.0, fid=i, valid=i), DT, i)
        assert abs(cmd.steer_x - prev) <= int(round(P.rate_limit_raw)) + 1
        prev = cmd.steer_x
    assert prev > 0                               # 确实在向目标爬


@pytest.mark.parametrize("x_target,expect_zero", [
    (255 / 32767 / (P.k_psi * P.k_head_lane), True),                  # raw=255 → 死区归 0
    (256 / 32767 / (P.k_psi * P.k_head_lane), False),                 # raw=256 → 保留
])
def test_deadzone_boundary_255_256(x_target, expect_zero):
    # 隔离输出链边界：无 lookahead/阻尼/低通/限幅，且横向加速度增益置零
    # （executed 不漂移，PD 稳态恒等于 k_p·x_target）；维护权限全放开，
    # 死区/封顶归上一节测，这里只测杆值输出链的 255/256 边界
    pl = _planner(k_r=0.0, lookahead_tau_s=0.0, tau_steer_s=1e-9,
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
    base = dict(lookahead_tau_s=0.0, k_r=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                hold_deadband_lane=0.0, hold_stick_max=1.0)
    base.update(over)
    return _planner(**base)





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
    pl.state.v_lat = 3.0                      # 向右快速接近 0.5
    cmd = pl.update(_out(x_target=0.5, fid=1, valid=1), DT, 1)
    # x_pred = 0.4 + 3.0×0.25 = 1.15 > 0.5 → 误差为负 → 收力（不继续右打）
    assert cmd.steer_x <= 0


# ---------- 路中心连续重锚（step 2.5：开环虚胖的闭环解） ----------

def test_road_reanchor_pins_inflated_model():
    """模型虚胖（a_lat_gain 偏大）跑过头：有路观测时 executed 被钉回观测值，
    误差常驻→杆不提前松。对照组：无观测（road_offset=None）executed 一路冲高。"""
    pl_open = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                       rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                       v_lat_max=1e9, )   # 纯积分，无回正
    pl_obs = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                      rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                      v_lat_max=1e9, )
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
                  v_lat_max=1e9, a_lat_gain=0.0,
                  )  # 本用例锁 α-β（新默认位置-only 另测）
    for i in range(1, 12):
        pl.update(_out(x_target=1.0, fid=i, valid=i), DT, i,
                  road_offset=0.05 * i)          # 每拍右移 0.05 车道
    assert pl.state.executed_lane == pytest.approx(0.05 * 11, abs=0.08)
    assert pl.state.v_lat > 0                # 学到正速度


def test_road_jump_rejected():
    """新息超 obs_jump_max_lane：坏检测，本拍弃观测，executed 不被拽飞。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0,
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
                  v_lat_max=1e9, a_lat_gain=0.0,
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
        assert pl.last_rebase is None                # 被拒不等于重基
    assert pl.state.executed_lane == pytest.approx(before, abs=1e-9)
    pl.update(_out(x_target=0.0, fid=4 + n - 1, valid=4 + n - 1), DT,
              4 + n - 1, road_offset=-1.7)           # 第 n 拍：确认并重基
    assert pl.state.executed_lane == pytest.approx(-1.7, abs=1e-6)
    # 诊断面：记的是实际执行的分支，不是候选条件
    assert pl.last_rebase == "jump_confirm"


def test_hold_offset_is_not_a_position_observation():
    """历史保持值（memory/wall_occl/pair_slot）不是本拍测量：不形成参考系、
    不收 α。保持期模型自走；脏计时照常推进但锚点保留（下一次新鲜大读数
    直接走陈旧重基，不必再等形成门 1s）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.5, road_offset_hold=True)
    assert pl._road_anchor is None                   # 保持值不定义参考系
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=0.0)
    assert pl._road_anchor == 0.0
    pl.state.executed_lane = 0.4
    pl.state.v_lat = 0.0
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=1.6, road_offset_hold=True)
    assert pl.state.executed_lane == pytest.approx(0.4, abs=1e-9)   # 无 α 拉回
    assert pl.last_rebase is None                    # 保持拍未超预算：不重基
    n_budget = int(P.anchor_stale_s / DT)
    # 只攒到预算差两拍：让「超预算」正好落在下面那一拍新鲜大读数上，
    # 而不是在保持循环里就触发（保持拍自身也会重基，那是另一条路径）。
    for i in range(4, 4 + n_budget - 2):
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i,
                  road_offset=1.6, road_offset_hold=True)
    assert pl.last_rebase is None and pl._road_anchor == 0.0   # 几何仍在，锚不丢
    # 保持超预算后的第一拍新鲜大读数：陈旧判据立即重基，不等确认/形成门
    pl.update(_out(x_target=0.0, fid=4 + n_budget, valid=4 + n_budget), DT,
              4 + n_budget, road_offset=1.6)
    assert pl.state.executed_lane == pytest.approx(1.6, abs=1e-6)
    assert pl.last_rebase == "stale_reject"          # 有锚、持续被拒的陈旧重基


def test_hold_offset_rebases_when_stale_budget_expires():
    """保持拍把陈旧计时攒满即按手边路心读数重基——墙边新鲜深度只占约 1/4
    拍，若只在新鲜拒绝拍检查预算，状态会带着假位置继续按目标向外打杆。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 0.7                 # 盲期内模型漂出 0.7 道
    pl._stale_t = P.anchor_stale_s - DT
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=1.9, road_offset_hold=True)
    assert pl.state.executed_lane == pytest.approx(1.9, abs=1e-6)
    assert pl._stale_t == pytest.approx(0.0)
    assert pl.last_rebase == "hold_stale"            # 保持拍按手边读数重基


def test_hold_rebase_blocked_by_disagreeing_line():
    """线仍在锁（合理范围）且与保持深度差超一致门：不拿这份深度重基——
    单侧墙 + W/2 反推会错 1 道以上（20261026 局 s1#1 506：深度 1.8、线 0.5）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    pl._stale_t = P.anchor_stale_s - DT
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=1.8, road_offset_hold=True,
              lane_x=0.4, lane_ok=True)
    assert abs(pl.state.executed_lane) < 0.1
    assert pl._stale_t >= P.anchor_stale_s        # 预算保留，等两源收敛


def test_hold_rebase_blocked_by_recent_coast_line():
    """短断链（COAST）的线读数仍算反证：线刚测过 1.2、深度保持值 −0.2，
    差 1.4 道 → 不重基；线龄超对照窗后才让深度单独重基。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=1.2, lane_x=1.2, lane_ok=True)
    pl._stale_t = P.anchor_stale_s - DT
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=-0.2, road_offset_hold=True,
              lane_x=1.2, lane_ok=False, lane_age_ms=100.0)
    assert pl.state.executed_lane == pytest.approx(1.2, abs=0.05)
    assert pl._stale_t >= P.anchor_stale_s
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=-0.2, road_offset_hold=True,
              lane_x=1.2, lane_ok=False, lane_age_ms=500.0)
    assert pl.state.executed_lane == pytest.approx(-0.2, abs=1e-6)


def test_blackout_keeps_stale_budget_for_next_edge_reading():
    """断供丢锚不清零陈旧计时：下一条新鲜贴边读数直接重基，不再白等 1s。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 0.7
    pl._stale_t = P.anchor_stale_s - DT
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)     # 断供拍
    assert pl._road_anchor is None
    pl.update(_out(x_target=0.0, fid=3, valid=3), DT, 3,
              road_offset=1.9)                   # 新鲜贴边读数 → 立即重基
    assert pl.state.executed_lane == pytest.approx(1.9, abs=1e-6)
    assert pl._road_anchor == 0.0
    assert pl.last_rebase == "form_starve"           # 丢锚后按形成门饿死路径重基


def test_rebase_source_field_distinguishes_paths():
    """诊断面能分辨三条重基来源，且正常小新息不产生事件（None）。

    form_align：形成门外的贴边拍与锁线互证 → 直接对齐；
    正常修正：已在帧内的小新息只走 α，不落重基事件。
    字段只记录**实际执行**的分支，供离线归因（trace `lat_ctrl.rebase`）。
    """
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=1.6, lane_x=1.6, lane_ok=True)
    assert pl.last_rebase == "form_align"
    assert pl.state.executed_lane == pytest.approx(1.6, abs=1e-6)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=1.6, lane_x=1.6, lane_ok=True)
    assert pl.last_rebase is None                    # 无新息：不是重基拍


# ---------- 保持期车道线融合（2026-10-25） ----------

def test_lane_fusion_tracks_motion_during_hold():
    """深度保持期用车道线连续横位补运动：车贴墙不动时模型会按打杆量虚走，
    线不会（p2 496~537：线 −1.9 不动，模型从 −1.0 漂到 −0.24）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    for i in range(10):
        pl.update(_out(x_target=0.0, fid=2 + i, valid=2 + i), DT, 2 + i,
                  road_offset=0.0, road_offset_hold=True,
                  lane_x=-0.1 * (i + 1), lane_ok=True)
    assert pl.state.executed_lane < -0.4
    assert pl.state.executed_lane > -1.1       # 平滑+收敛滞后，不外推


def test_lane_fusion_rejects_depth_outlier():
    """已对账锁线可以否决不一致的新鲜深度（差 0.7 > 0.5）：单帧不拉位置，
    防单侧/绿区坏深度把车从墙边拽回；持续 3 次新证据才重基。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.0, lane_x=0.0, lane_ok=True)
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2,
              road_offset=0.7, lane_x=0.0, lane_ok=True)     # 线否决单帧
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-6)
    for i in range(15):
        pl.update(_out(x_target=0.0, fid=3 + i, valid=3 + i), DT, 3 + i,
                  road_offset=0.7, road_offset_hold=True,
                  lane_x=0.0, lane_ok=True)
    assert abs(pl.state.executed_lane) < 0.05
    for i in range(3):                        # 持续 3 次新证据 → 重基到深度
        pl.update(_out(x_target=0.0, fid=20 + i, valid=20 + i), DT, 20 + i,
                  road_offset=0.7, lane_x=0.0, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(0.7, abs=1e-6)


def test_trusted_line_vetoes_wall_depth_override():
    """墙边反例：线报 −1.7、深度报 −0.33，若照收 α 会把状态拽回路中。
    已对账锁线否决它；深度持续 3 次新证据才重基（线错时仍能纠正）。"""
    pl = _jump_planner()
    fid = _anchor(pl)
    pl.state.executed_lane = -0.4
    pl._lane_sm = -1.7
    pl._lane_bias = 0.0
    pl._lane_trusted = True
    fid += 1
    pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=-0.33,
              lane_x=-1.7, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(-0.4, abs=1e-6)
    for _ in range(3):                        # 深度持续 3 次新证据 → 重基
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=-0.33,
                  lane_x=-1.7, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(-0.33, abs=1e-6)


def test_lane_bias_clamped_and_smoothed():
    """帧偏置限幅 ±0.25 + 慢滤波：单帧 0.5 道差异不能一步坐实，也不能
    把线帧拽出物理带（墙边 +0.5 偏置会正好抵消贴墙信号）。"""
    pl = _jump_planner()
    fid = _anchor(pl)
    fid += 1
    pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=0.5,
              lane_x=0.0, lane_ok=True)
    assert 0.0 < pl._lane_bias <= 0.25
    assert pl._lane_bias < 0.1                 # 单帧只走 EMA 一小步
    for _ in range(6):
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=0.5,
                  lane_x=0.0, lane_ok=True)
    assert pl._lane_bias <= 0.25 + 1e-9
    for _ in range(6):
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=-0.5,
                  lane_x=0.0, lane_ok=True)
    assert pl._lane_bias >= -0.25 - 1e-9


def test_lane_jump_disables_fusion_until_depth_agrees():
    """线自身跳 1 道 = 换线/重锁：重置并停用融合（不许把旧帧位置拽到新线），
    等新鲜深度与线一致（重基拍）后再启用。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
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
    线 +1.32——偏置应按限幅/慢滤波更新（不再一步坐实 0.55），随后保持期
    融合把状态拉向线帧；墙边坏深度的 0.5 道偏移不再被当帧偏置吃进去。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
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
    assert abs(pl._lane_bias) <= 0.25               # 限幅内
    for i in range(12):
        pl.update(_out(x_target=0.0, fid=4 + i, valid=4 + i), DT, 4 + i,
                  road_offset=1.87, road_offset_hold=True,
                  lane_x=1.3, lane_ok=True)
    assert pl.state.executed_lane > 1.2


def test_lane_relock_resets_smoothing():
    """重锁后的第一条锁线可能换了线/换了帧：平滑值直接重置为线值并停用
    融合（0.6 道的换线在 0.8 跳变门内，不能靠跳变阈值发现）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
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
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=1.87, lane_x=1.3, lane_ok=True)
    assert pl._road_anchor == 0.0
    assert pl.state.executed_lane == pytest.approx(1.87, abs=1e-6)
    assert pl._lane_trusted is True


def test_edge_frame_needs_lane_agreement():
    """贴边拍没有锁线佐证时仍守形成门（防垃圾墙定义参考系）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=1.87, lane_x=0.3, lane_ok=True)   # 线差 1.57 道
    assert pl._road_anchor is None
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-9)


def test_line_beyond_physical_range_is_ignored():
    """贴墙错锁远端边线（|x|>2.6 道）不参与平滑/融合：路面半宽只有 1.9~2.4
    道，3.1~3.5 的读数是邻道/远端边线（20261026 局 s1#3 连续 0.8s）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1,
              road_offset=0.6, lane_x=0.6, lane_ok=True)
    pl.state.executed_lane = 0.6
    for i in range(6):
        pl.update(_out(x_target=0.0, fid=2 + i, valid=2 + i), DT, 2 + i,
                  road_offset=0.6, road_offset_hold=True,
                  lane_x=3.2, lane_ok=True)
    assert pl._lane_sm == pytest.approx(0.6, abs=1e-6)
    assert pl.state.executed_lane == pytest.approx(0.6, abs=1e-6)
    assert pl._lane_trusted                          # 越界读数不能撤销信任


def test_lane_fusion_off_without_lock():
    """无锁线（COAST）不融合：退回模型，保持值不得把位置拽到线读数。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
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
                  v_lat_max=1e9, a_lat_gain=0.0)
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
                  v_lat_max=1e9, a_lat_gain=0.0)
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
                  v_lat_max=1e9, a_lat_gain=0.0)
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
                  v_lat_max=1e9, a_lat_gain=0.0)
    for i in range(1, 20):
        pl.update(_out(x_target=0.0, fid=i, valid=i), DT, i, road_offset=0.6)
    assert pl.state.executed_lane == pytest.approx(0.6, abs=0.05)  # 道上=路心


# ---------- 维护性转向权限（16:44 局：定中心全权 PD=满舵绕桩） ----------

def test_hold_authority_deadband_and_cap():
    """保持态（CRUISE 定中心）：死区内不发力、死区外杆幅封顶（2026-10-18 换血：
    场跟随无 CHANGE 全权分支，gentle 语义无条件生效）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  v_lat_max=1e9, a_lat_gain=0.0)
    pl.state.executed_lane = 0.1                       # 误差 0.1 < 死区 0.15
    c = pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1)
    assert c.steer_x == 0                              # 不发力（旧语义会持续拽）
    pl.state.executed_lane = 1.2                       # 带内（≤1.4）：k_p0.7→u=0.84
    c = pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert -int(P.hold_stick_max * _STICK_FULL) - 100 < c.steer_x < 0  # 封顶 0.45


# ---------- 机动态全权（阶段 2，2026-10-18） ----------

def test_maneuver_full_authority_moves_one_lane():
    """maneuver=True：闭环 1s 内位移 >0.3 道（越过维护杆幅上限）。"""
    pl = _planner()
    rig = _Rig(pl)
    max_stick = 0
    for i in range(1, 21):
        cmd = rig.tick(_out(x_target=1.0, fid=i, valid=i, maneuver=True))
        max_stick = max(max_stick, abs(cmd.steer_x))
    assert max_stick > int(P.hold_stick_max * _STICK_FULL)
    assert rig.x > 0.3

def test_small_maneuver_error_stays_gentle():
    """maneuver 按安全区间（约 0.17 道）置位，但 0.2 道的日常修正不得用全权：
    执行层全权门槛仍是 0.3 道。计划因参考系分歧退回 PD 时尤其要守住。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 0.2
    c = pl.update(_out(x_target=0.0, fid=2, valid=2, maneuver=True), DT, 2)
    assert c.steer_x == 0 or abs(c.steer_x) <= int(P.hold_stick_max * _STICK_FULL)
    # 同一拍误差放大到 0.8 道：才允许越过维护杆幅
    pl.state.executed_lane = 0.8
    c = pl.update(_out(x_target=0.0, fid=3, valid=3, maneuver=True), DT, 3)
    assert abs(c.steer_x) > int(P.hold_stick_max * _STICK_FULL)


def test_wall_escape_uses_full_authority():
    """贴边逃生：|executed|>1.4 道时不再走 gentle 0.45 杆幅，直接全权回带；
    带内仍保持 gentle（防画龙）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 1.6
    pl.state.v_lat = 0.0
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert pl.last_steer_raw < -0.7
    pl2 = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                   rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl2.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl2.state.executed_lane = 1.2
    pl2.state.v_lat = 0.0
    pl2.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert pl2.last_steer_raw >= -P.hold_stick_max - 0.01



def test_brake_budget_requires_lateral_speed():
    """车几乎静止、位置偏 1.2 道：没有可刹的动量，不得满舵回带；
    交给 gentle PD（≤0.45）。"""
    base = dict(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl = _planner(**base)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 1.2
    pl.state.v_lat = 0.1
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert abs(pl.last_steer_raw) <= P.hold_stick_max + 0.01


def test_brake_budget_not_triggered_braking_toward_target():
    """正在朝目标刹（停止点未越带）时保持 gentle 权限，不画龙。"""
    base = dict(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl = _planner(**base)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 1.2
    pl.state.v_lat = -0.3       # 停止点 1.165：还差得远，反打是过量修正
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert pl.last_steer_raw >= -P.hold_stick_max - 0.01


def test_brake_budget_disabled_without_decel():
    """brake_decel_lane_s2=0 关闭预算：旧行为逐位恢复（gentle 封顶）。"""
    base = dict(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                )
    pl = _planner(**base)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 1.2
    pl.state.v_lat = 0.3
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2)
    assert pl.last_steer_raw >= -P.hold_stick_max - 0.01




def test_corridor_infeasible_plan_commands_escape():
    """计划判定压在走廊边距内（无效）时执行它的逃逸候选杆值：
    不能退回保持死区——目标差 0.26 道 < 0.5，PD 会完全不修，车停在离栏
    0.25m（20261026 局 s2#4 21.6s：目标 0.93、车位 1.19、杆 −0.18）。"""
    base = dict(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl = _planner(**base)
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = 1.19
    pl.state.v_lat = 0.0
    plan = types.SimpleNamespace(
        valid=False, reason="plan:infeasible_corridor", psi_feedforward=-0.6,
        x_preview_lane=0.8, v_preview_lane_s=0.0)
    pl.update(_out(x_target=0.93, fid=2, valid=2), DT, 2, plan=plan)
    assert pl.last_steer_raw == pytest.approx(-0.6, abs=1e-6)
    pl2 = _planner(**base)
    pl2.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)
    pl2.state.executed_lane = 1.19
    pl2.state.v_lat = 0.0
    stale = types.SimpleNamespace(
        valid=False, reason="plan:stale", psi_feedforward=-0.6,
        x_preview_lane=0.8, v_preview_lane_s=0.0)
    pl2.update(_out(x_target=0.93, fid=2, valid=2), DT, 2, plan=stale)
    # 不用无效计划的杆值；新死区 0.15 下 0.26 道误差会经 gentle PD 轻修正，
    # 但幅度远小于计划候选的 −0.6。
    assert abs(pl2.last_steer_raw) < 0.3


def test_maneuver_deadband_and_cap_used():
    """机动态用 maneuver_deadband_lane/maneuver_stick_max：误差超全权门槛
    0.3 道后发力，机动态死区 0.05 小于保持死区 0.15。"""
    pl = _planner(maneuver_deadband_lane=0.05, maneuver_stick_max=0.85)
    cmd = pl.update(_out(x_target=0.5, fid=1, valid=1, maneuver=True), DT, 1)
    assert cmd.steer_x > 0


# ---------- ⑥b 速度修正时间基（2026-10-08 判据冻结，台账 §4A） ----------



def test_obs_velocity_injection_respects_cap():
    """⑥b β 注入在 v_lat_max 饱和内（16:44 局 |v_lat| 冲到 11.4 ≫ 2.5，
    阻尼项 −k_d·v 随即满反打喂给极限环）。"""
    pl = _planner(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                  a_lat_gain=0.0, )      # 隔离 v_lat_max 饱和面
    pl.update(_out(x_target=0.0, fid=1, valid=1), DT, 1, road_offset=0.0)   # 锚=0
    pl.update(_out(x_target=0.0, fid=2, valid=2), DT, 2, road_offset=0.7)   # r=0.7
    # 裸 β 注入 = 0.2·0.7/0.05 = 2.8 > v_lat_max=2.5 → 必须被夹住
    assert abs(pl.state.heading) <= P.heading_max_rad + 1e-9


# ---------- β 速度注入抑制（阶段 2，2026-10-18） ----------

def _beta_planner(**over):
    base = dict(a_lat_gain=0.0, v_lat_max=1e9,
                lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                )
    base.update(over)
    return _planner(**base)



def test_beta_deadband_ignores_small_residual():
    pl = _beta_planner()
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.update(_out(fid=2, valid=2), DT, 2, road_offset=0.02)
    assert pl.state.v_lat == pytest.approx(0.0)



def test_v_lat_never_saturates_under_alternating_offsets():
    pl = _beta_planner(v_lat_max=2.5)
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    vmax = 0.0
    for i in range(2, 60):
        ro = 0.5 if i % 2 else -0.5
        pl.update(_out(fid=i, valid=i), DT, i, road_offset=ro)
        vmax = max(vmax, abs(pl.state.v_lat))
    assert vmax < 2.5 - 1e-6



def test_beta_velocity_tracks_persistent_offset():
    """持续同号新息（贴墙：深度恒 −1.9、模型却以为 +1.0 在回中）必须
    把 v_lat 拉回观测侧——222557 局 s1 38.1~39.7s 骑缘躺平的修正通路。"""
    pl = _planner(a_lat_gain=0.0, v_lat_max=2.5,
                  lookahead_tau_s=0.0, tau_steer_s=1e-9,
                  rate_limit_raw=999999.0, stick_deadzone_raw=0.0)
    pl.update(_out(fid=1, valid=1), DT, 1, road_offset=0.0)
    pl.state.executed_lane = -1.6
    pl.state.v_lat = 1.0
    vs = []
    for i in range(2, 22):
        pl.update(_out(fid=i, valid=i), DT, i, road_offset=-1.9)
        vs.append(pl.state.v_lat)
    assert abs(vs[-1]) < 0.3    # 假的 +1.0 被拉回观测侧（车实际近乎不动）
    assert max(vs[:3]) <= 1.0   # 变化率上限：不许一步打满


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


# ---------- 横速观测（模型状态之外的融合输入） ----------

def _feed_weave(obs, seconds=2.0, v_lane=1.0, theta=0.05):
    """合成来回横移：x 斜率与线角同号，避免慢基线把常值航向吸收掉。"""
    x, t, fid = 0.0, 0.0, 1
    while t < seconds:
        sign = 1.0 if int(t / 0.5) % 2 == 0 else -1.0
        x += sign * v_lane * 0.1
        t += 0.1
        obs.update(0.1, x, True, sign * theta, True, fid, 60.0)
        fid += 1





def test_line_jump_is_not_lateral_velocity():
    """线横位整道跳（k 重锚/换线）不是位移：不得积成横速。"""
    obs = HeadingObserver(P)
    for fid in range(1, 6):
        obs.update(0.1, 0.2, True, 0.0, True, fid, 40.0)
    assert obs.v_line is not None
    obs.update(0.1, 1.2, True, 0.0, True, 6, 40.0)   # 跳 1 道
    assert obs.v_line is None




def test_merge_commit_tracks_decision_goal_not_preview():
    """承诺并线按决策目标出力，而不是 0.2s 预览。

    实测一段 2.4s 的并线只走了 0.16 道：计划前馈 p≈计划预览≈当前位置时
    跟踪项恒接近 0。承诺期（phase=merging 且 plan.layer=4）改用决策目标。"""
    plan = types.SimpleNamespace(
        valid=True, reason="plan:ok", psi_feedforward=0.0, layer=4,
        x_preview_lane=0.5, v_preview_lane_s=0.0, value_total=0.0)
    cmd = {}
    for phase in ("keep", "merging"):
        pl = _planner(a_lat_gain=0.0, v_lat_max=1e9)
        pl.state.executed_lane = 0.5
        pl.state.v_lat = 0.0
        pl.state.steer_norm = 0.0
        out = replace(_out(x_target=0.83, maneuver=True, fid=1, valid=10),
                      phase=phase)
        cmd[phase] = pl.update(out, DT, 1, plan=plan).steer_x
    assert abs(cmd["keep"]) < 200          # 预览=当前位置：几乎不出力
    assert cmd["merging"] > 2500           # 目标差 0.33 道：朝目标出力


# ---------- 大新息确认：新证据计数，hold/复用不清零、不重复计 ----------

def _jump_planner(**over):
    base = dict(lookahead_tau_s=0.0, tau_steer_s=1e-9,
                rate_limit_raw=999999.0, stick_deadzone_raw=0.0,
                v_lat_max=1e9, a_lat_gain=0.0,
                obs_jump_max_lane=0.8, obs_jump_confirm_ticks=3,
                anchor_stale_s=10.0)
    base.update(over)
    return _planner(**base)


def _anchor(pl, fid=1):
    pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=0.0)
    return fid


def test_hold_ticks_do_not_reset_jump_streak():
    """墙边 150/227 拍是 hold：缺证据不是反证，不能把大新息确认清零。"""
    pl = _jump_planner()
    fid = _anchor(pl)
    seq = [(1.5, False, True), (1.5, True, False),   # 新 / hold
           (1.5, False, True), (1.5, True, False),   # 新 / hold
           (1.5, False, True)]                       # 第 3 次新证据
    for i, (ro, hold, new) in enumerate(seq):
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=ro,
                  road_offset_hold=hold, road_offset_new=new)
    assert pl.state.executed_lane == pytest.approx(1.5, abs=1e-6)


def test_reuse_ticks_do_not_advance_jump_streak():
    """同一读数的复用拍不是新证据：不得凭 3 拍复用把单帧大新息坐实。"""
    pl = _jump_planner()
    fid = _anchor(pl)
    for i in range(2):                                # 新 + 两次复用
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=1.5,
                  road_offset_new=(i == 0))
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-6)
    for _ in range(2):                                # 再两次新证据 → 第 3 次确认
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=1.5,
                  road_offset_new=True)
    assert pl.state.executed_lane == pytest.approx(1.5, abs=1e-6)


def test_corroborated_large_offset_rebases_immediately():
    """锁线与深度同拍一致的大新息：两源互证，不等 3 拍。"""
    pl = _jump_planner()
    fid = _anchor(pl)
    # 线已在 1.5 道稳定跟踪且帧偏置已对账（墙边常见：线逐步走到墙，深度跳变）
    pl._lane_sm = 1.5
    pl._lane_bias = 0.0
    pl._lane_trusted = True
    fid += 1
    pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=1.5,
              lane_x=1.5, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(1.5, abs=1e-6)


def test_uncorroborated_large_offset_still_needs_three_new():
    """线与深度不一致的大新息不走近道：仍按 3 次新证据确认，防单帧坏深度。"""
    pl = _jump_planner()
    fid = _anchor(pl)
    for _ in range(2):
        fid += 1
        pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=1.5,
                  lane_x=-1.5, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(0.0, abs=1e-6)
    fid += 1
    pl.update(_out(fid=fid, valid=fid), DT, fid, road_offset=1.5,
              lane_x=-1.5, lane_ok=True)
    assert pl.state.executed_lane == pytest.approx(1.5, abs=1e-6)


def test_frame_shift_holds_planner_frame_not_scene_target():
    """计划判参考系分歧时只认 planner 帧：目标在错帧里，不追它。"""
    fs = types.SimpleNamespace(valid=False, reason="plan:frame_shift",
                               layer=4, value_total=0.0, psi_feedforward=0.0,
                               x_preview_lane=0.0, v_preview_lane_s=0.0)
    pl = _planner()
    rig = _Rig(pl)
    for _ in range(P.frame_hold_max_ticks):
        rig.tick(_out(x_target=-1.2, plan=fs))
    assert abs(rig.x) < 0.05          # 没有朝错帧目标 -1.2 冲过去

def _feed_ramp(obs, seconds=6.0, rate=0.06, dt=0.1, n_locked=2):
    """线角匀速上升：稳态下 ψ≈2·rate（2s 慢基线滞后），r≈rate。"""
    t, fid, th = 0.0, 1, 0.0
    while t < seconds:
        obs.update(dt, 0.0, True, th, True, fid, 60.0, n_locked)
        th += rate * dt
        t += dt
        fid += 1


def test_heading_psi_and_rate_follow_line_angle():
    """ψ 在稳态等于 2·rate（2s 基线滞后）；r 只在建立期为正。

    注：相对朝向对「缓慢的常值转速」与「路面曲率」不可分辨——那是 2s 慢基线
    的定义带来的，r 捕捉的是 ψ 的变化而不是绝对转速。"""
    obs = HeadingObserver(P)
    rate, dt, th, fid, t = 0.06, 0.1, 0.0, 1, 0.0
    r_build = None
    while t < 8.0:
        obs.update(dt, 0.0, True, th, True, fid, 60.0, 2)
        th += rate * dt
        t += dt
        fid += 1
        if r_build is None and t > 1.5:
            r_build = obs.r
    assert r_build is not None and r_build > 0.02       # 建立期：ψ 在涨
    assert obs.psi == pytest.approx(2.0 * rate, abs=0.02)
    assert abs(obs.r) < 0.01                            # 稳态：ψ 不再变化
    assert obs.debug()["psi_age"] < 200.0


def test_single_line_angle_is_not_a_heading():
    """单条线会给出 20°+ 假角度：n_locked<2 的拍不得更新 ψ，也不得推进基线。"""
    obs = HeadingObserver(P)
    obs.update(0.1, 0.0, True, 0.0, True, 1, 60.0, 2)
    assert obs.psi == 0.0
    base = obs._psi_base
    obs.update(0.1, 0.0, True, 0.39, True, 2, 60.0, 1)   # 实测假值量级
    assert obs.psi == 0.0                                # 不吸收假角度
    assert obs._psi_base == base                         # 也不推进基线
    obs.update(0.1, 0.0, True, 0.05, True, 3, 60.0, 2)
    assert obs.psi is not None and obs.psi > 0.0


def test_scale_is_causal_bounded_and_has_no_fallback():
    obs = HeadingObserver(P)
    # 真值 v=40：num/den 与之一致时直接发布，置信度到顶
    tan = 0.1
    obs._spd_num = tan * (40.0 / 3.5) * tan * 20.0
    obs._spd_den = tan * tan * 20.0
    obs.update(0.05, None, False, None, False, None, None)
    assert obs.v_scale == pytest.approx(40.0, abs=0.5)
    assert obs.v_scale_conf == 1.0
    # 证据衰减到下限以下：不发布（不兜底任何常数）
    for _ in range(600):                     # 60s 无新证据：20s 记忆衰减到 ~5%
        obs.update(0.1, None, False, None, False, None, None)
    assert obs.v_scale is None
    assert obs.v_scale_conf == 0.0
    # 物理域外：宁缺勿错
    obs2 = HeadingObserver(P)
    obs2._spd_num = 200.0 / 3.5 * 0.01 * 10
    obs2._spd_den = 0.01 * 10
    obs2.update(0.05, None, False, None, False, None, None)
    assert obs2.v_scale is None


def test_scale_accum_ignores_small_heading_and_slow_motion():
    obs = HeadingObserver(P)
    obs.v_line = 0.5
    obs._scale_accum(0.005)                  # |tanψ| 太小：条件数不足
    assert obs._spd_den == 0.0
    obs._scale_accum(0.1)                    # 正常样本
    assert obs._spd_den == pytest.approx(math.tan(0.1) ** 2)
    obs.v_line = 0.01                        # 车几乎没动：不可用
    obs._scale_accum(0.1)
    assert obs._spd_den == pytest.approx(math.tan(0.1) ** 2)




def test_line_frame_reset_keeps_heading_shadow():
    """线横位整道跳只清线帧：ψ/r 与在线比例必须活下来。

    20261030_181838 实测：两处线帧重置共用一个 reset 时，比例累积被反复清零，
    置信度 0→1→0.3 来回跳，`p1h` 只有 9% 的拍有值。"""
    obs = HeadingObserver(P)
    obs.update(0.1, 0.0, True, 0.05, True, 1, 60.0, 2)
    obs.psi = 0.05
    obs._psi_t = obs._t
    obs.v_scale = 33.0
    obs._spd_den = 0.2
    obs._spd_num = 0.2 * 33.0 / 3.5
    obs.v_line = 0.7
    obs.reset(keep_heading=True)
    assert obs.psi == pytest.approx(0.05)        # 朝向状态保留
    assert obs.v_scale == pytest.approx(33.0)    # 跨秒的比例累积保留
    assert obs._spd_den == pytest.approx(0.2)
    assert obs.v_line is None                    # 线帧的东西照旧清掉
    obs.reset()
    assert obs.psi is None and obs.v_scale is None and obs._spd_den == 0.0
