# -*- coding: utf-8 -*-
"""横向轨迹采样评分器（阶段二 §二）单测。

d 坐标系=路心系（道0=路中心，中轴巡航定案）：全部入参出参同系。街车
碰撞过滤沿用第六轮判据（静态+到站截断，f1f0259：CV 外推=幻影碰撞）。"""
from __future__ import annotations

import numpy as np
import pytest

from maaracing_master.plugins.speedrush.latsample import (
    LatTrajectorySampler, quintic)


class _Car:
    """CarView 鸭子（采样器消费 x_lane/cy/rel_approach 三字段）。"""

    def __init__(self, x_lane: float, cy: int = 600, rel: float = 50.0):
        self.x_lane = x_lane
        self.cy = cy
        self.rel_approach = rel


def _sampler(**kw) -> LatTrajectorySampler:
    base = dict(v_lat_max=2.5, v_ego_row=716.0, horizon_s=2.5, gap_lane=0.6)
    base.update(kw)
    return LatTrajectorySampler(**base)


def test_quintic_satisfies_boundary_conditions() -> None:
    for d0, vd, ad, d1, T in [(0.0, 0.0, 0.0, 2.0, 1.2),
                              (1.3, -0.8, 0.4, -1.0, 1.8),
                              (-2.0, 1.5, -0.3, 0.0, 0.8)]:
        c = quintic(d0, vd, ad, d1, T)
        t = np.linspace(0.0, T, 200)
        d = np.polyval(c[::-1], t)                 # coeffs 升幂 → polyval 降幂
        dd = np.polyval(np.polyder(c[::-1], 1), t)
        ddd = np.polyval(np.polyder(c[::-1], 2), t)
        assert d[0] == pytest.approx(d0, abs=1e-9)
        assert dd[0] == pytest.approx(vd, abs=1e-9)
        assert ddd[0] == pytest.approx(ad, abs=1e-9)
        assert d[-1] == pytest.approx(d1, abs=1e-9)
        assert dd[-1] == pytest.approx(0.0, abs=1e-6)
        assert ddd[-1] == pytest.approx(0.0, abs=1e-6)


def test_quintic_matches_linear_system_solution() -> None:
    """闭式系数 = 三约束线性方程组的直接解（防闭式推导笔误）。"""
    d0, vd, ad, d1, T = 0.4, 0.9, -0.2, 2.6, 1.5
    c = quintic(d0, vd, ad, d1, T)
    A = np.array([[T**3, T**4, T**5],
                  [3*T**2, 4*T**3, 5*T**4],
                  [6*T, 12*T**2, 20*T**3]], float)
    # 终端约束（扣除初态基线 d0+vd·t+ad/2·t² 的贡献）
    b = np.array([
        d1 - (d0 + vd*T + 0.5*ad*T*T),
        0.0 - (vd + ad*T),
        0.0 - ad])
    a3, a4, a5 = np.linalg.solve(A, b)
    assert c[3] == pytest.approx(a3, abs=1e-9)
    assert c[4] == pytest.approx(a4, abs=1e-9)
    assert c[5] == pytest.approx(a5, abs=1e-9)


def test_jerk_integral_matches_numeric() -> None:
    from maaracing_master.plugins.speedrush.latsample import jerk_integral
    c = quintic(0.0, 0.0, 0.0, 2.0, 1.2)
    t = np.linspace(0.0, 1.2, 20001)
    j = np.polyval(np.polyder(c[::-1], 3), t)
    assert jerk_integral(c, 1.2) == pytest.approx(float(np.trapezoid(j*j, t)),
                                                  rel=1e-4)


def test_reachable_endpoint_selected_for_target() -> None:
    """无车无界：k_d 项把终点拉到目标（网格含精确目标点），T 尽量短。"""
    s = _sampler()
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=2.0, cars=(), width=None)
    assert r.ok and r.best.d1 == pytest.approx(2.0)


def test_cruise_guards_axis() -> None:
    s = _sampler()
    r = s.update(d0=0.3, vd0=0.1, ad0=0.0, d_target=0.0, cars=(), width=None)
    assert r.ok and r.best.d1 == pytest.approx(0.0)


def test_unreachable_pair_dropped() -> None:
    """|d1−d0|/T > v_lat_max 的 (d1,T) 剔除（实测 p90 2.2~2.9 道/s 背书）。"""
    s = _sampler(v_lat_max=2.5)
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=3.0, cars=(), width=None)
    assert r.ok
    # 目标 3 道只有 T≥1.2 可达（3/2.5=1.2）；T=0.8 档的 ±3 终点不可达
    assert r.best.T >= 1.2


def test_width_clamps_endpoints() -> None:
    """W=4 道 → 界=±2 道：±3 终点出局（终点钳制；width None=不钳）。"""
    s = _sampler()
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=0.0, cars=(), width=4.0)
    assert r.ok
    assert r.best.d1 <= 2.0 and r.best.d1 >= -2.0


def test_cut_in_front_of_approaching_car_vetoed() -> None:
    """切入逼近街车的车位 → 候选淘汰（veto=机动使距离恶化才拦，第七轮）。"""
    s = _sampler(gap_lane=0.6)
    # 车 d_road=1.5（x_lane=1.0, ro=0.5），0.66s 后到本车行
    car = _Car(x_lane=1.0, cy=650, rel=5.0)
    r = s.update(d0=0.5, vd0=0.0, ad0=0.0, d_target=0.0, cars=(car,), width=None,
                 ro=0.5, hz=20.0)
    assert r.ok and r.best.d1 == pytest.approx(0.0)   # 避开侧中选
    assert r.n_reject_collision > 0                   # 朝车位的候选被拦


def test_moving_away_and_holding_not_self_vetoed() -> None:
    """保持/远离不自否（第七轮：机动使距离恶化才拦）——近距并行拍仍出计划。"""
    s = _sampler(gap_lane=0.6)
    car = _Car(x_lane=-0.5, cy=600, rel=0.0)          # 静止并行（全视野）
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=0.0, cars=(car,), width=None,
                 ro=0.0, hz=20.0)
    assert r.ok and r.best.d1 == pytest.approx(0.0)   # 保持位不 veto


def test_stationary_car_on_target_pushes_plan_aside() -> None:
    s = _sampler(gap_lane=0.6)
    car = _Car(x_lane=-0.5, cy=600, rel=0.0)          # 目标位被占（全视野）
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=-0.5, cars=(car,), width=None,
                 ro=0.0, hz=20.0)
    assert r.ok and r.best.d1 != pytest.approx(-0.5)  # 不站进车位


def test_far_car_beyond_horizon_still_guards_hold_position() -> None:
    """到站晚于视野的车按驻停位判：终点撞其 lateral 位的候选淘汰。"""
    s = _sampler(horizon_s=2.5)
    car = _Car(x_lane=2.0, cy=650, rel=2.0)           # d_road=2.0，3.3s 到站
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=2.0, cars=(car,), width=None,
                 ro=0.0, hz=20.0)
    assert r.ok and abs(r.best.d1 - 2.0) >= 0.6       # 站进其位的候选被淘汰


def test_car_passing_now_does_not_block() -> None:
    """已到本车行的车（pass 制语义接管）：不拦（与 LateralSafety t_eff=0 同）。"""
    s = _sampler(gap_lane=0.6)
    car = _Car(x_lane=1.9, cy=716, rel=100.0)         # t_pass=0
    r = s.update(d0=0.0, vd0=0.0, ad0=0.0, d_target=2.0, cars=(car,), width=None,
                 ro=0.1, hz=20.0)
    assert r.ok and r.n_reject_collision == 0


def test_start_state_continuity() -> None:
    """初态进轨迹：d(0)=d0、d'(0)=vd0（上一拍解续接，无跳变）。"""
    s = _sampler()
    r = s.update(d0=1.0, vd0=-0.6, ad0=0.0, d_target=0.0, cars=(), width=None)
    assert r.ok
    d_now = np.polyval(r.best.coeffs[::-1], 0.0)
    v_now = np.polyval(np.polyder(r.best.coeffs[::-1], 1), 0.0)
    assert d_now == pytest.approx(1.0, abs=1e-9)
    assert v_now == pytest.approx(-0.6, abs=1e-9)
