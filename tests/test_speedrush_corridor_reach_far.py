# -*- coding: utf-8 -*-
"""R2 前瞻外方向权（corridor_reach.far，2026-10-16 设计稿 §2.4）的回归锁。

锁面：预选未来通过侧语义（E 复用贴身曲线——正对远车 d̂=0 为 0、贴身窗最大、
窗外衰减）、作用域区间互斥（近场 horizon 内是 R3 的地盘，hunt_horizon 外无
价值，同一辆车不双计）、估计缺位不奖（t_overtake None=不追）、多车聚合三
语义（max/sum/capped 上限=value_limit+pass_value）、w 折权、R2 依赖 R3 贴身
参数（E 曲线前提）、闸关逐位（R2 参数缺=与 R3-only 输出一致）。
几何契约面与 test_speedrush_corridor_reach_hug 同源（lane_w=3.38 反推）。
"""
from __future__ import annotations

import math

import pytest

from maaracing_master.plugins.speedrush.corridor_reach import (
    CarSource, CorridorBandSpan, ReachParams, evaluate)

LANE_W = 3.38
HALF = 0.75 * LANE_W
LO = 0.55 * LANE_W
HI = 0.70 * LANE_W
PC = 0.73 * LANE_W
PS = 0.06 * LANE_W
LIMIT = 120.0
W = 0.3                     # 远权标称起值（标定对象随胜出语义定，影子对比面）


def _p(d: float) -> float:
    return 1.0 / (1.0 + math.exp((d - PC) / PS))


PLATFORM = 30.0 + LIMIT * _p((LO + HI) / 2.0)      # ≈132.23


def _params(**kw) -> ReachParams:
    base = dict(step_m=0.5, horizon_s=2.0, crash_t_s=0.8, crash_penalty=100.0,
                pass_value=30.0, far_coin_weight=0.3, tie_margin=20.0,
                shift_cost_per_m=0.0, coin_value_per_unit=30.0,
                coin_group_min_count=3.0, attribution_half_m=HALF,
                pass_band_m=LANE_W, tau_s=0.13, margin_s=0.15,
                need_of_dx_m=lambda dx: 0.3 + 0.1 * dx,
                hug_d_lo_m=LO, hug_d_hi_m=HI,
                hug_p_center_m=PC, hug_p_scale_m=PS, hug_value_limit=LIMIT,
                far_car_weight=W, hunt_horizon_s=6.0, far_aggregation="max")
    return ReachParams(**{**base, **kw})


def _bands(n_bands=3, left=-6.0, right=6.0, z0=0.0, band_len=3.0):
    return tuple(CorridorBandSpan(z0 + i * band_len, z0 + (i + 1) * band_len,
                                  left, right) for i in range(n_bands))


def _far_car(x, t_overtake=4.0):
    """R2 population：近场窗外的米制锚前车（t_meet=t_overtake 同源同值）。"""
    return CarSource(x_m=x, t_meet_s=t_overtake, t_overtake_s=t_overtake)


def _best_by_x(rep):
    return {round(c.x_m, 6): c for c in rep.candidates}


# ---------- 预选未来通过侧（E 曲线形状） ----------

def test_far_value_peaks_at_hug_window():
    """远车方向权在贴身窗最大、正对远车（d̂<lo）为 0——预选通过侧语义。"""
    car = _far_car(2.2)                 # t_ov=4.0 ∈ (2, 6]
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[4.25].far_value == pytest.approx(W * PLATFORM)   # d̂=2.05 窗内
    assert by_x[1.75].far_value == 0.0                           # d̂=0.45 < lo
    # 正对远车（候选≈车横位）方向权为 0：预选的是"将来能贴身通过它的位置"
    assert by_x[2.25].far_value == 0.0


def test_far_value_decays_above_window():
    """窗外衰减：far_value 随 d̂ 单调降，仍 ≥ w×pass_value。"""
    car = _far_car(2.2)
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    v_in = by_x[4.25].far_value
    v_out = by_x[4.75].far_value        # d̂=2.55 > hi
    assert v_out == pytest.approx(W * (30.0 + LIMIT * _p(2.55)))
    assert 0.0 < v_out < v_in
    assert by_x[5.75].far_value < v_out          # d̂=3.55 更远更低


# ---------- 作用域区间互斥（同一辆车不双计） ----------

def test_scope_mutually_exclusive_by_t_overtake():
    """近场车（t_ov≤horizon）只有 R3 贴身项；远车只有 R2 方向项；
    hunt_horizon 外无价值。E 衰减下限=pass_value（远处横向仍有 w×30 小拉力，
    近常数不翻 argmax——设计语义：E 复用贴身曲线，衰减不到 0）。"""
    near = _far_car(2.2, t_overtake=1.5)         # 近场：R3 地盘
    far = _far_car(-2.2, t_overtake=4.0)         # 远车：R2 地盘
    beyond = _far_car(1.0, t_overtake=7.0)       # hunt_horizon 外
    # 单独近场车：任何候选无 R2 值（近场车不进 R2）
    rep_near = evaluate(_bands(), (), (near,), 0.0, _params())
    assert rep_near is not None
    assert all(c.far_value == 0.0 for c in rep_near.candidates)
    # 单独 beyond 车：t_ov > hunt_horizon → 无 R2 值
    rep_beyond = evaluate(_bands(), (), (beyond,), 0.0, _params())
    assert rep_beyond is not None
    assert all(c.far_value == 0.0 for c in rep_beyond.candidates)
    # 近场+远车并存：近场候选拿 R3 贴身、远车候选拿 R2 方向，各计各的
    rep = evaluate(_bands(), (), (near, far), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c_near = by_x[4.25]                 # d̂(near)=2.05 窗内
    assert c_near.hug_value == pytest.approx(PLATFORM)
    # 该候选的 far_value 只来自远车（d̂=6.45，衰减下限 ≈w×pass_value）
    assert c_near.far_value == pytest.approx(W * (30.0 + LIMIT * _p(6.45)))
    c_far = by_x[-4.25]                 # d̂(far)=2.05 窗内
    assert c_far.far_value == pytest.approx(W * PLATFORM)
    # 该候选的 hug_value 只来自近场车在 d̂=6.45 的衰减值（E 衰减下限=
    # pass_value，设计语义：远处横向仍有小保底，近常数不翻 argmax）；
    # 远车本身（t_ov=4.0 > horizon）不进 R3 ✓（由 far 车单独场景另证）
    assert c_far.hug_value == pytest.approx(30.0 + LIMIT * _p(6.45))
    assert c_near.hug_cars == 1
    # 单独远车：t_ov > horizon → 任何候选无 R3 贴身值（窗内候选也不进）
    rep_far_solo = evaluate(_bands(), (), (far,), 0.0, _params(far_car_weight=0.0))
    assert rep_far_solo is not None
    assert all(c.hug_value == 0.0 and c.far_value == 0.0
               for c in rep_far_solo.candidates)


def test_missing_instrument_not_rewarded():
    """估计缺位（t_overtake=None）不奖：远车无米制锚 → far_value=0（保守默认）。"""
    car = CarSource(x_m=2.2, t_meet_s=4.0, t_overtake_s=None)
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    assert all(c.far_value == 0.0 for c in rep.candidates)


# ---------- 多车聚合三语义 ----------

def test_aggregation_max_vs_sum_vs_capped():
    """双远车同窗：max 取单车上限、sum 求和、capped 封顶 value_limit+pass_value。"""
    a = _far_car(2.2, t_overtake=4.0)            # 候选 4.25：d̂=2.05 窗内
    b = _far_car(1.2, t_overtake=5.0)            # 候选 4.25：d̂=3.05 衰减区
    e_b = 30.0 + LIMIT * _p(3.05)
    for agg, expect in (("max", PLATFORM), ("sum", PLATFORM + e_b),
                        ("capped", min(PLATFORM + e_b, LIMIT + 30.0))):
        rep = evaluate(_bands(), (), (a, b), 0.0, _params(far_aggregation=agg))
        assert rep is not None
        c = _best_by_x(rep)[4.25]
        assert c.far_value == pytest.approx(W * expect), agg


# ---------- 闸依赖与回滚 ----------

def test_far_requires_hug_params():
    """R2 的 E 曲线以 R3 贴身参数为前提：hunt_horizon 在而 hug 参数缺 → 无 R2。"""
    car = _far_car(2.2)
    p = _params(hug_d_lo_m=None, hug_d_hi_m=None, hug_p_center_m=None,
                hug_p_scale_m=None, hug_value_limit=0.0)
    rep = evaluate(_bands(), (), (car,), 0.0, p)
    assert rep is not None
    assert all(c.far_value == 0.0 and c.hug_value == 0.0 for c in rep.candidates)


def test_far_gate_off_matches_hug_only():
    """R2 参数缺（hunt_horizon=None / w=0）：far_value 恒零、R3 贴身面逐位不变，
    候选分恰差 R2 增量（远车贡献消失，其余逐位一致）。"""
    cars = (_far_car(2.2, t_overtake=1.5), _far_car(-2.2, t_overtake=4.0),
            CarSource(x_m=3.0, t_meet_s=1.0, t_overtake_s=None))
    for off_kw in ({"hunt_horizon_s": None}, {"far_car_weight": 0.0}):
        rep_r2 = evaluate(_bands(), (), cars, 0.0, _params())
        rep_off = evaluate(_bands(), (), cars, 0.0, _params(**off_kw))
        assert rep_r2 is not None and rep_off is not None
        for c, c_off in zip(rep_r2.candidates, rep_off.candidates):
            # R3 面（hug 值）不受 R2 闸影响
            assert c_off.hug_value == pytest.approx(c.hug_value), off_kw
            # 闸关：far_value 恒零，候选分恰差 R2 增量
            assert c_off.far_value == 0.0, off_kw
            assert c_off.score == pytest.approx(c.score - c.far_value), off_kw
        # 远车贴邻窗候选：R2 关=只剩近场车的衰减保底（≈30），R2 开=+w×平台
        c_off = _best_by_x(rep_off)[-4.25]
        c_r2 = _best_by_x(rep_r2)[-4.25]
        assert c_off.far_value == 0.0
        assert c_r2.far_value == pytest.approx(W * PLATFORM)
        assert c_r2.score == pytest.approx(c_off.score + W * PLATFORM)


def test_far_value_additive_to_score():
    """R2 值是加分项（进 argmax），与其他收益同秤叠加。"""
    car = _far_car(2.2, t_overtake=4.0)
    rep_on = evaluate(_bands(), (), (car,), 0.0, _params())
    rep_off = evaluate(_bands(), (), (car,), 0.0, _params(far_car_weight=0.0))
    assert rep_on is not None and rep_off is not None
    c_on = _best_by_x(rep_on)[4.25]
    c_off = _best_by_x(rep_off)[4.25]
    assert c_on.score == pytest.approx(c_off.score + W * PLATFORM)


# ---------- config 与决策边界 ----------

def test_config_far_aggregation_enum():
    """far_aggregation 枚举校验（fail-loud）：越界枚举拒载。"""
    import pytest

    from tests.test_speedrush_corridor_reach_hug import _read_cfg
    with pytest.raises(ValueError, match="far_aggregation"):
        _read_cfg({"overtake_hunting": True, "hug_value": True,
                   "far_direction": True, "far_car_weight": 0.3,
                   "far_aggregation": "bogus"})


def test_boundary_attaches_far_params():
    """far_direction 开：参数面挂远权/作用域/聚合；关：字段缺（w=0/None）。"""
    from dataclasses import replace as dreplace

    from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH
    from maaracing_master.plugins.speedrush.config import _read_decision
    from maaracing_master.plugins.speedrush.decision import DecisionEngine

    base = _read_decision(CFG_PATH)
    eng_on = DecisionEngine(dreplace(
        base, corridor_reach=dreplace(base.corridor_reach,
                                      overtake_hunting=True, hug_value=True,
                                      far_direction=True, far_car_weight=0.3,
                                      hunt_horizon_s=6.0,
                                      far_aggregation="capped")))
    p = eng_on._reach_params
    assert p.far_car_weight == 0.3 and p.hunt_horizon_s == 6.0
    assert p.far_aggregation == "capped"
    eng_off = DecisionEngine(dreplace(
        base, corridor_reach=dreplace(base.corridor_reach,
                                      overtake_hunting=False, hug_value=False,
                                      far_direction=False)))
    p_off = eng_off._reach_params
    assert p_off.hunt_horizon_s is None and p_off.far_car_weight == 0.0
