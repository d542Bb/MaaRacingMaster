# -*- coding: utf-8 -*-
"""走廊收益场选路径（corridor_reach，2026-10-14 设计稿 §3.2/§3.3）的回归锁。

锁面：综合计分（沿线多币组累加，不盯一个目标）、中心偏好（平手选离走廊
中心近者）、碰撞候选大负罚、可达性即门槛（远组可达预占位/近组不追——
feas 病理的反面）、币遮挡（赶到前已过的车不挡）、错身价值、远币远权、
走廊界外拒、组下限复用（n=1 按整组计）、路径插值（近端当前/远端胜出/逐带
过渡）、黑视诚实弃权、空旷路合法结果、候选数=span/步进。
数值场景以参数默认值反推（horizon 2.0/crash_t 0.8/pass 30/远权 0.3/步进
0.5/归属半宽 1.7），改参数会红——参数与场景是契约面。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.corridor_reach import (
    CarSource, CoinSource, CorridorBandSpan, ReachParams, evaluate)

UNIT = 30.0          # 单枚币值
CONF = 0.85
VAL = UNIT * 3 * CONF        # 整组值 76.5（n=3 满组）


def _params(**kw) -> ReachParams:
    """need 线性模型 need(dx)=0.3+0.1·dx（秒）：起步 0.3 + 0.1/米。"""
    base = dict(step_m=0.5, horizon_s=2.0, crash_t_s=0.8, crash_penalty=100.0,
                pass_value=30.0, far_coin_weight=0.3, tie_margin=20.0,
                shift_cost_per_m=0.0, coin_value_per_unit=UNIT,
                coin_group_min_count=3.0, attribution_half_m=1.7,
                pass_band_m=3.38, tau_s=0.13, margin_s=0.15,
                need_of_dx_m=lambda dx: 0.3 + 0.1 * dx)
    return ReachParams(**{**base, **kw})


def _bands(n_bands=3, left=-4.0, right=4.0, z0=0.0, band_len=3.0):
    return tuple(CorridorBandSpan(z0 + i * band_len, z0 + (i + 1) * band_len,
                                  left, right) for i in range(n_bands))


def _coin(x, t_miss=1.5, n=3, conf=CONF):
    return CoinSource(x_m=x, observed_count=n, conf=conf, t_miss_s=t_miss)


def _car(x, t_meet=0.6):
    return CarSource(x_m=x, t_meet_s=t_meet)


def _best_by_x(rep):
    return {c.x_m: c for c in rep.candidates}


# ---------- 综合计分（不盯一个目标） ----------

def test_two_coin_groups_accumulate_along_path():
    """沿线两个币组都可收：候选拿两组整组值累加（不盯一个目标锁）。"""
    rep = evaluate(_bands(), (_coin(0.0), _coin(0.5)), (), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[0.25]                      # 两个组的归属半宽都罩住它
    assert c.coin_value == pytest.approx(2 * VAL)
    assert c.reachable_coins == 2
    assert c.score == pytest.approx(2 * VAL)


def test_crash_candidate_drops_all_benefits():
    """碰撞候选 = −crash_penalty 平额，币收益不累加（lane_score continue 语义）。"""
    # 车与币同位：币归属内的候选全部判碰撞
    rep = evaluate(_bands(), (_coin(1.0),), (_car(1.0, t_meet=0.6),), 0.0,
                   _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[1.25]
    assert c.crash
    assert c.score == pytest.approx(-100.0)
    assert c.coin_value == 0.0
    # 胜出位不在碰撞带内
    assert abs(rep.best_x_m - 1.0) > 1.7


# ---------- 可达性即门槛（feas 病理的反面） ----------

def test_far_group_reachable_at_three_lanes():
    """远侧一组（t_miss 大）3 车道宽外仍可达：候选预占位拿全值。"""
    rep = evaluate(_bands(), (_coin(3.38, t_miss=1.8),), (), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[1.75]                      # 归属半宽内最近的候选
    assert c.coin_value == pytest.approx(VAL)
    assert c.reachable_coins == 1


def test_near_group_unreachable_not_chased():
    """近时刻组（t_miss 0.5 < need+τ+margin）：不追——不计入也不降权。"""
    rep = evaluate(_bands(), (_coin(3.38, t_miss=0.5),), (), 0.0, _params())
    assert rep is not None
    assert all(c.coin_value == 0.0 for c in rep.candidates)
    assert all(c.reachable_coins == 0 for c in rep.candidates)
    assert all(c.far_coins == 0 for c in rep.candidates)


# ---------- 中心偏好（六点 1） ----------

def test_center_preference_by_tie_margin():
    """同场景三档平手阈：胜出位随阈放宽逐步偏向走廊中心。

    币在 x=1.5、自车也在 1.5：argmax=1.25（位移最小）；阈 20 分时中心侧
    0.25（差 2 分，位移代价内）偷走胜出位。"""
    coin = _coin(1.5, t_miss=1.5)
    p = _params(shift_cost_per_m=2.0)
    rep = evaluate(_bands(), (coin,), (), 1.5, p)
    assert rep is not None
    assert rep.best_x_m == pytest.approx(0.25)      # tie_margin=20：中心胜
    rep = evaluate(_bands(), (coin,), (), 1.5,
                   _params(shift_cost_per_m=2.0, tie_margin=1.0))
    assert rep is not None
    assert rep.best_x_m == pytest.approx(0.75)      # 阈 1：中带胜
    rep = evaluate(_bands(), (coin,), (), 1.5,
                   _params(shift_cost_per_m=2.0, tie_margin=1e-9))
    assert rep is not None
    assert rep.best_x_m == pytest.approx(1.25)      # 阈 0：纯 argmax（位移最小）


def test_exact_tie_prefers_center():
    """候选间完全同分（空旷路）：胜出=离走廊中心最近者。"""
    rep = evaluate(_bands(), (), (), 1.0, _params())
    assert rep is not None
    assert rep.best_x_m == pytest.approx(0.25, abs=1e-6) or \
        rep.best_x_m == pytest.approx(-0.25, abs=1e-6)


# ---------- 币遮挡与错身 ----------

def test_car_arriving_before_coin_blocks_it():
    """车先于币到达（t=0.9 ≤ t_miss=1.0）把币挡住；错身后（t=1.2）不挡。"""
    rep = evaluate(_bands(), (_coin(0.0, t_miss=1.0),),
                   (_car(0.0, t_meet=0.9),), 0.0, _params())
    assert rep is not None
    assert all(c.coin_value == 0.0 for c in rep.candidates)
    rep = evaluate(_bands(), (_coin(0.0, t_miss=1.0),),
                   (_car(0.0, t_meet=1.2),), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[0.25].coin_value == pytest.approx(VAL)


def test_pacing_car_blocks_coins_whole_window():
    """pacing 车（t=None）整窗占用：币不可收。"""
    rep = evaluate(_bands(), (_coin(0.0),), (CarSource(x_m=0.0, t_meet_s=None),),
                   0.0, _params())
    assert rep is not None
    assert all(c.coin_value == 0.0 for c in rep.candidates)


def test_car_passing_before_we_get_there_does_not_block():
    """车在我们赶到前已过（t < need）：不挡币也不判碰撞（lane_score 同判）。"""
    rep = evaluate(_bands(), (_coin(3.38, t_miss=1.8),),
                   (_car(3.38, t_meet=0.2),), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[1.75].coin_value == pytest.approx(VAL)
    assert not by_x[1.75].crash


def test_pass_value_for_nearby_car_within_horizon():
    """错身带内的车（归属半宽外一圈，窗内来得及）记 pass_value。"""
    rep = evaluate(_bands(), (), (_car(3.5, t_meet=1.0),), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[0.25]                      # d=3.25 ∈ (1.7, 5.08]
    assert c.pass_value_sum == pytest.approx(30.0)
    assert c.score == pytest.approx(30.0)


# ---------- 远币远权 / 界外拒 / 组下限 ----------

def test_far_coin_gets_reduced_weight():
    """前瞻窗外（t_miss=3.0 > horizon 2.0）与仪器无读数（None）都记远权。"""
    for t_miss in (3.0, None):
        rep = evaluate(_bands(), (_coin(0.0, t_miss=t_miss),), (), 0.0,
                       _params())
        assert rep is not None
        by_x = _best_by_x(rep)
        c = by_x[0.25]
        assert c.coin_value == pytest.approx(0.3 * VAL)
        assert c.far_coins == 1
        assert c.reachable_coins == 0


def test_coin_outside_corridor_reach_excluded():
    """走廊横向够不着的币（|x−best| > 归属半宽 ∀候选）：不入账。"""
    rep = evaluate(_bands(), (_coin(8.0),), (), 0.0, _params())
    assert rep is not None
    assert all(c.coin_value == 0.0 for c in rep.candidates)


def test_group_floor_partial_view_counts_full_group():
    """组下限复用（7c28ce9）：观测 1 枚按整组 ≥3 枚计（n=1 → 90·conf）。"""
    rep = evaluate(_bands(), (_coin(0.0, n=1),), (), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[0.25].coin_value == pytest.approx(UNIT * 3 * CONF)


# ---------- 候选集与位移代价 ----------

def test_candidate_count_matches_span_over_step():
    """span 8m / 步进 0.5m → 16 候选（设计稿 §3.2 算力锚）。"""
    rep = evaluate(_bands(), (), (), 0.0, _params())
    assert rep is not None
    assert len(rep.candidates) == 16


def test_shift_cost_penalizes_lateral_distance():
    """位移代价（分/米）：远离自车的候选扣 |x−executed|×单价。"""
    rep = evaluate(_bands(), (), (), 0.0, _params(shift_cost_per_m=2.0))
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[0.25].shift_cost == pytest.approx(0.5)
    assert by_x[0.25].score == pytest.approx(-0.5)


# ---------- 路径插值（六点 6 的数据面） ----------

def test_path_interpolates_from_executed_to_best():
    """路径：近端=自车当前 x，远端=胜出位，逐带线性过渡。"""
    rep = evaluate(_bands(n_bands=3), (), (), -1.0, _params(tie_margin=1e-9))
    assert rep is not None
    path = rep.path
    assert path[0] == pytest.approx((0.0, -1.0))
    assert path[-1] == pytest.approx((9.0, rep.best_x_m))
    by_x = _best_by_x(rep)
    goal = by_x[0.25].x_m if rep.best_x_m == pytest.approx(0.25) \
        else rep.best_x_m
    assert path[1] == pytest.approx((3.0, -1.0 + (goal + 1.0) / 3.0))
    assert path[2] == pytest.approx((6.0, -1.0 + 2 * (goal + 1.0) / 3.0))


# ---------- 黑视与空旷路（安全默认） ----------

def test_no_readable_band_honest_abstain():
    """全带缺证据（弯道帧）：诚实弃权 None，不臆造路宽。"""
    bands = (CorridorBandSpan(0.0, 3.0, None, None),)
    assert evaluate(bands, (), (), 0.0, _params()) is None


def test_one_sided_band_not_usable():
    """只有单侧证据的带不构成可行驶域（双侧有证据才算 span）。"""
    bands = (CorridorBandSpan(0.0, 3.0, -4.0, None),)
    assert evaluate(bands, (), (), 0.0, _params()) is None


def test_empty_road_is_legal_result():
    """空旷路=合法结果：候选间只有位移代价差，胜出仍产出路径。"""
    rep = evaluate(_bands(), (), (), 0.0, _params())
    assert rep is not None
    assert rep.candidates
    assert rep.path[0] == pytest.approx((0.0, 0.0))
