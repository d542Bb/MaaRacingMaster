# -*- coding: utf-8 -*-
"""R5 层 1 奖励车项（corridor_reach.bonus_value，2026-10-17 设计稿 §9.1）的回归锁。

锁面：闸关逐位（bonus_value=0 或 bonuses 空=与无奖励车基线逐位一致）、评分
与币同门同式（横向归属半宽/可达门 need+τ+margin<t_miss≤前瞻窗/先到的车
遮挡）、优先级排序（奖励车 120 压过金币组 90，argmax 即「排最前面的胜出」）、
顺手捎上（同候选分项求和）、审计面（bonus_value/bonus_cars）。
行为收益不在此锁（那是实机 A/B 与离线回放的验收面）。

场景几何：need(dx)=0.3+0.1·dx（秒）、attribution_half=2.535m、
horizon=2.0s、tau+margin=0.28——t_miss∈(need(dx)+0.28, 2.0] 才计。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.corridor_reach import (
    BonusSource, CarSource, CoinSource, CorridorBandSpan, ReachParams,
    evaluate)

LANE_W = 3.38
HALF = 0.75 * LANE_W               # 归属半宽 2.535m
BONUS_V = 120.0                    # 起值=一次极限动作分（设计稿 §9.1）


def _params(**kw) -> ReachParams:
    """need 线性模型 need(dx)=0.3+0.1·dx（秒），与 hug 锁同式。"""
    base = dict(step_m=0.5, horizon_s=2.0, crash_t_s=0.8, crash_penalty=100.0,
                pass_value=30.0, far_coin_weight=0.3, tie_margin=20.0,
                shift_cost_per_m=0.0, coin_value_per_unit=30.0,
                coin_group_min_count=3.0, attribution_half_m=HALF,
                pass_band_m=LANE_W, tau_s=0.13, margin_s=0.15,
                need_of_dx_m=lambda dx: 0.3 + 0.1 * dx,
                bonus_value=BONUS_V)
    return ReachParams(**{**base, **kw})


def _bands(n_bands=3, left=-6.0, right=6.0, z0=0.0, band_len=3.0):
    return tuple(CorridorBandSpan(z0 + i * band_len, z0 + (i + 1) * band_len,
                                  left, right) for i in range(n_bands))


def _bonus(x, t_miss=1.5, conf=0.9):
    return BonusSource(x_m=x, t_miss_s=t_miss, conf=conf)


def _best_by_x(rep):
    return {round(c.x_m, 6): c for c in rep.candidates}


# ---------- 闸关逐位 ----------

def test_gate_off_identical_to_no_bonus():
    """bonus_value=0：带奖励车源与不带逐位一致（闸关=回今天）。"""
    b = _bonus(1.0)
    rep_with = evaluate(_bands(), (), (), 0.0, _params(bonus_value=0.0), (b,))
    rep_without = evaluate(_bands(), (), (), 0.0, _params(bonus_value=0.0), ())
    assert rep_with is not None and rep_without is not None
    for c1, c2 in zip(rep_with.candidates, rep_without.candidates):
        assert c1 == c2


def test_empty_bonuses_no_change():
    """bonuses 空：评分与无奖励车逐位一致。"""
    rep = evaluate(_bands(), (), (), 0.0, _params(), ())
    rep0 = evaluate(_bands(), (), (), 0.0, _params(bonus_value=0.0), ())
    assert rep is not None and rep0 is not None
    for c1, c2 in zip(rep.candidates, rep0.candidates):
        assert c1.score == pytest.approx(c2.score)


# ---------- 评分与门（与币同门同式） ----------

def test_bonus_attracts_nearby_candidates():
    """奖励车 120 计入归属半宽内候选，审计面 bonus_value/bonus_cars。"""
    rep = evaluate(_bands(), (), (), 0.0, _params(), (_bonus(1.0),))
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[1.25]                      # d=0.25 ∈ 半宽；need=0.325，门 0.605<1.5 过
    assert c.bonus_cars == 1
    assert c.bonus_value == pytest.approx(BONUS_V)
    assert c.score == pytest.approx(BONUS_V)   # 无其他项：score=bonus
    out = by_x[5.75]                    # d=4.75 > 半宽：不计
    assert out.bonus_cars == 0 and out.bonus_value == 0.0


def test_unreachable_bonus_not_counted():
    """可达门：t_miss ≤ need+τ+margin 不计（来不及不追，不计入也不降权）。"""
    # x=1.25 处 need=0.325，门=0.605；t_miss=0.6 过近 → 不计
    rep = evaluate(_bands(), (), (), 0.0, _params(), (_bonus(1.0, t_miss=0.6),))
    by_x = _best_by_x(rep)
    assert by_x[1.25].bonus_cars == 0
    assert by_x[1.25].bonus_value == 0.0


def test_far_bonus_beyond_horizon_not_counted():
    """窗外：t_miss > 前瞻窗不计（远权折减是币的语义，奖励车 v1 不做）。"""
    rep = evaluate(_bands(), (), (), 0.0, _params(), (_bonus(1.0, t_miss=2.5),))
    by_x = _best_by_x(rep)
    assert by_x[1.25].bonus_cars == 0


def test_occluding_car_blocks_bonus():
    """遮挡：先到的车挡住（我们赶到前它已过不挡、pacing 车 t=None 挡）。"""
    # 车在奖励车横位、t_meet=0.9 ∈ [need, t_miss]：挡
    blocker = CarSource(x_m=1.0, t_meet_s=0.9, t_overtake_s=None)
    rep = evaluate(_bands(), (), (blocker,), 0.0, _params(), (_bonus(1.0),))
    by_x = _best_by_x(rep)
    assert by_x[1.25].bonus_cars == 0
    # 车先过（t_meet < need=0.325）：不挡
    passer = CarSource(x_m=1.0, t_meet_s=0.2, t_overtake_s=None)
    rep2 = evaluate(_bands(), (), (passer,), 0.0, _params(), (_bonus(1.0),))
    by_x2 = _best_by_x(rep2)
    assert by_x2[1.25].bonus_cars == 1
    assert by_x2[1.25].bonus_value == pytest.approx(BONUS_V)


# ---------- 优先级排序（层 1 > 层 3） ----------

def test_bonus_outranks_coin():
    """奖励车 120 > 金币组 90：胜出的是奖励车候选（排最前面的胜出；平手
    tie_margin 内偏中心——胜出位必是 bonus 候选，金币候选仍在不被删）。"""
    coin = CoinSource(x_m=-5.0, observed_count=3.0, conf=1.0, t_miss_s=1.0)
    bonus = _bonus(4.0)                 # 两源相隔远，无重叠候选
    rep = evaluate(_bands(), (coin,), (), 0.0, _params(), (bonus,))
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[4.25].bonus_value == pytest.approx(BONUS_V)
    best = by_x[round(rep.best_x_m, 6)]
    assert best.bonus_value == pytest.approx(BONUS_V)
    assert best.coin_value == 0.0       # 胜出位不是金币候选
    # 金币候选仍在（不是被删）：90 < 120
    coin_best = max(rep.candidates, key=lambda c: c.coin_value)
    assert coin_best.coin_value == pytest.approx(90.0)


def test_combo_bonus_plus_coin_sums():
    """顺手捎上：同候选分项求和（奖励车+金币组同候选 → 和）。"""
    coin = CoinSource(x_m=1.0, observed_count=3.0, conf=1.0, t_miss_s=1.0)
    rep = evaluate(_bands(), (coin,), (), 0.0, _params(), (_bonus(1.0),))
    by_x = _best_by_x(rep)
    c = by_x[1.25]
    assert c.bonus_value == pytest.approx(BONUS_V)
    assert c.coin_value == pytest.approx(90.0)
    assert c.score == pytest.approx(BONUS_V + 90.0)
