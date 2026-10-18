# -*- coding: utf-8 -*-
"""净空项（2026-10-18 用户定案：场即空间优先级）的回归锁。

锁面：软净空罚（近车衰减、context steering max 规则、时间门——前瞻窗外
远车不产生净空）、窄缝硬约束（车-车缝/车-缘缝判 blocked、币值不得压过、
纯走廊缘对不判）、闸关逐位回今天、阈值阶梯载入序校验。
数值场景以参数默认值反推（clearance_ref 2.199m=0.65 道、squeeze 2.535m=
0.75 道、horizon 2.0、crash_penalty 100），改参数会红——参数与场景是契约面。
"""
from __future__ import annotations

import math

import pytest

from maaracing_master.plugins.speedrush.corridor_reach import (
    CarSource, CoinSource, CorridorBandSpan, ReachParams, evaluate)

LANE_W = 3.38
REF_M = 0.65 * LANE_W            # 软净空作用半径（迁移自实机包络阈）
SQUEEZE_M = 0.75 * LANE_W        # 窄缝硬约束下限


def _params(**kw) -> ReachParams:
    base = dict(step_m=0.5, horizon_s=2.0, crash_t_s=0.8, crash_penalty=100.0,
                pass_value=30.0, far_coin_weight=0.3, tie_margin=20.0,
                shift_cost_per_m=0.0, coin_value_per_unit=30.0,
                coin_group_min_count=3.0, attribution_half_m=1.7,
                pass_band_m=LANE_W, tau_s=0.13, margin_s=0.15,
                need_of_dx_m=lambda dx: 0.3 + 0.1 * dx,
                clearance_ref_m=REF_M, clearance_penalty=60.0,
                squeeze_min_gap_m=SQUEEZE_M)
    return ReachParams(**{**base, **kw})


def _bands(n_bands=3, left=-4.0, right=4.0, z0=0.0, band_len=3.0):
    return tuple(CorridorBandSpan(z0 + i * band_len, z0 + (i + 1) * band_len,
                                  left, right) for i in range(n_bands))


def _car(x, t_meet=0.6):
    return CarSource(x_m=x, t_meet_s=t_meet)


def _coin(x, t_miss=1.5, n=3, conf=0.85):
    return CoinSource(x_m=x, observed_count=n, conf=conf, t_miss_s=t_miss)


def _best_by_x(rep):
    return {c.x_m: c for c in rep.candidates}


# ---------- 软净空罚 ----------

def test_clearance_penalty_grows_near_car():
    """近车候选分低于远车候选：净空不对称=优先级比较的来源。"""
    rep = evaluate(_bands(), (), (_car(1.0, t_meet=None),), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    near, far = by_x[1.25], by_x[-1.75]
    assert near.clearance_cost > 0.0
    assert far.clearance_cost == pytest.approx(0.0)   # 超出作用半径：0
    assert far.score > near.score


def test_clearance_far_car_no_cost():
    """时间门：前瞻窗外远车（t_meet>horizon）不产生净空——不与远车方向权
    双重惩罚。"""
    rep = evaluate(_bands(), (), (_car(0.0, t_meet=8.0),), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert all(c.clearance_cost == 0.0 for c in rep.candidates)


def test_clearance_max_not_sum():
    """两辆同侧近车：danger 取 max 不取 sum——与一辆同距车等价。"""
    one = evaluate(_bands(), (), (_car(-0.25, t_meet=None),), 0.0, _params())
    two = evaluate(_bands(), (), (_car(-0.25, t_meet=None),
                                  _car(2.75, t_meet=None)), 0.0, _params())
    assert one is not None and two is not None
    a = _best_by_x(one)[1.25]
    b = _best_by_x(two)[1.25]
    assert b.clearance_cost == pytest.approx(a.clearance_cost)


def test_clearance_gate_off_identical():
    """闸关（全零净空参数）：得分=旧构成（币值全额，无净空扣减）。"""
    rep = evaluate(_bands(), (_coin(0.0),), (), 0.0,
                   _params(clearance_penalty=0.0, squeeze_min_gap_m=0.0,
                           clearance_ref_m=0.0))
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[0.25].score == pytest.approx(30.0 * 3 * 0.85)  # 整组币值全额
    assert all(c.clearance_cost == 0.0 and not c.crash
               for c in rep.candidates)


def test_clearance_subtractive_only():
    """软净空罚是纯扣项：同参数下 penalty=60 的候选分 = penalty=0 分 − 罚。"""
    cars = (_car(1.0, t_meet=None), _car(-0.25, t_meet=None))
    off = evaluate(_bands(), (), cars, 0.0, _params(clearance_penalty=0.0))
    on = evaluate(_bands(), (), cars, 0.0, _params())
    assert off is not None and on is not None
    for c_off, c_on in zip(off.candidates, on.candidates):
        assert c_off.x_m == c_on.x_m and c_off.crash == c_on.crash
        assert c_on.score == pytest.approx(c_off.score - c_on.clearance_cost)


# ---------- 窄缝硬约束 ----------

def test_squeeze_blocks_narrow_car_car_gap():
    """两辆并排车缝隙 < squeeze_min：缝内候选全部 blocked（与碰撞同级）。"""
    rep = evaluate(_bands(), (), (_car(-0.5, t_meet=None),
                                  _car(0.5, t_meet=None)), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[0.25].crash                # 缝 1.0m < 2.535m
    assert by_x[0.25].score == pytest.approx(-100.0)
    # 缝外候选不受罚
    assert not by_x[-1.75].crash


def test_squeeze_car_edge_gap_blocked():
    """车贴走廊缘：车与缘的缝 < squeeze_min 也判 blocked（至少一侧是车）。"""
    # 车在 +3.0：右缘 +4.0 的缝 1.0m；候选 +3.5 卡在车与缘之间
    rep = evaluate(_bands(), (), (_car(3.0, t_meet=None),), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[3.25].crash


def test_squeeze_not_applied_to_pure_edge_pair():
    """无车：纯走廊缘对不判窄缝（路面窄于车体=读数损坏不是物理）。"""
    rep = evaluate(_bands(left=-1.0, right=1.0), (), (), 0.0, _params())
    assert rep is not None
    assert all(not c.crash for c in rep.candidates)


def test_squeeze_beats_coin_value():
    """币值 90（3 枚满组）不得压过窄缝硬约束：缝内币候选仍 blocked。"""
    rep = evaluate(_bands(), (_coin(0.0),), (_car(-0.5, t_meet=None),
                                             _car(0.5, t_meet=None)), 0.0,
                   _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[0.25].crash
    assert by_x[0.25].score == pytest.approx(-100.0)
    assert rep.best_x_m != by_x[0.25].x_m


def test_squeeze_wide_gap_passes():
    """缝宽 ≥ squeeze_min：候选不 blocked（硬约束只拦物理容不下的缝）。"""
    rep = evaluate(_bands(), (), (_car(-2.0, t_meet=None),
                                  _car(2.0, t_meet=None)), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert not by_x[0.25].crash            # 缝 4.0m > 2.535m


# ---------- 阈值阶梯载入序校验 ----------

def _cfg_with(cr_extra, tmp_path):
    """以仓库真源 decision.json 为基，覆写 corridor_reach 节写临时文件，
    走 _read_decision 同一载入路径（校验在载入段拦）。"""
    import json
    from maaracing_master.plugins.speedrush import DECISION_FILE
    from maaracing_master.plugins.speedrush.config import _read_decision
    raw = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    raw["corridor_reach"].update(cr_extra)
    p = tmp_path / DECISION_FILE.name
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return _read_decision(p)


def test_config_clearance_penalty_without_ref_rejected(tmp_path):
    with pytest.raises(ValueError, match="clearance_penalty"):
        _cfg_with({"clearance_penalty": 60.0, "clearance_ref_lane": 0.0},
                  tmp_path)


def test_config_squeeze_below_sampler_veto_rejected(tmp_path):
    with pytest.raises(ValueError, match="squeeze_min_gap_lane"):
        _cfg_with({"squeeze_min_gap_lane": 0.3}, tmp_path)


def test_config_ref_above_squeeze_rejected(tmp_path):
    with pytest.raises(ValueError, match="clearance_ref_lane"):
        _cfg_with({"clearance_ref_lane": 1.0, "squeeze_min_gap_lane": 0.75},
                  tmp_path)


def test_config_threshold_ladder_valid(tmp_path):
    """生产默认（0.45/0.65/0.75 阶梯）通过载入校验。"""
    cfg = _cfg_with({}, tmp_path)
    assert cfg.corridor_reach.clearance_ref_lane == 0.65
    assert cfg.corridor_reach.squeeze_min_gap_lane == 0.75
