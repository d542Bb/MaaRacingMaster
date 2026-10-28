# -*- coding: utf-8 -*-
"""四层选择层回归锁（2026-10-18 走廊重建）。

锁定：奖励车>擦边>金币>中心带的严格字典序、中心带不追正中、路点顺路
插入与可行性、层切换即时/同层驻留。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.selection import (
    LayeredSelector, Obj, RoadBand, RoadFrame, SelectionParams, select)

LW = 3.38


def _road(center=0.0, width_lanes=4.0, *, curve=0.0):
    half = width_lanes * LW / 2.0
    bands = tuple(
        RoadBand(z0, z0 + 3.0, center + curve * z0 - half,
                 center + curve * z0 + half)
        for z0 in range(-2, 25, 3))
    return RoadFrame(bands, width_lanes * LW, center - half,
                     center + half, True)


def _p(**kw):
    return SelectionParams(lane_w_m=LW, **kw)


def test_reward_is_layer_one():
    rep = select(_road(), [Obj("reward", 2.0, 2.0)],
                 [], [Obj("coin", 0.0, 1.0)], 0.0, _p())
    assert rep.layer == 1 and rep.why == "reward"
    assert rep.target_x_m == pytest.approx(2.0)
    assert rep.maneuver is True


def test_reward_infeasible_falls_to_coin():
    """2 道外、0.1s 到站：来不及，降到金币层（不是硬追奖励车）。"""
    rep = select(_road(), [Obj("reward", 6.0, 0.1)],
                 [], [Obj("coin", 0.3, 2.0)], 0.0, _p())
    assert rep.layer == 3 and rep.why == "coin"
    assert rep.target_x_m == pytest.approx(0.3)


def test_pass_picks_roomier_side():
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.65 * LW, abs=0.05)


def test_both_sides_blocked_holds_center():
    cars = [Obj("car", 0.0, 2.0),
            Obj("car", 0.65 * LW, 2.0),
            Obj("car", -0.65 * LW, 2.0)]
    rep = select(_road(), [], cars, [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_layer_and_center_fallback():
    rep = select(_road(), [], [], [Obj("coin", 0.35, 2.5)], 0.0, _p())
    assert rep.layer == 3 and rep.target_x_m == pytest.approx(0.35)
    rep = select(_road(), [], [], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_off_line_not_selected():
    """超过 1.2 道的散币不追（追了会把车拉离币线，3 秒内也横移不过去）。"""
    rep = select(_road(), [], [], [Obj("coin", 1.5 * LW, 3.0)], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_adjacent_lane_selected():
    """相邻道（0.9 道）的币可以成为主目标。"""
    rep = select(_road(), [], [], [Obj("coin", 0.9 * LW, 2.5)], 0.0, _p())
    assert rep.layer == 3 and rep.why == "coin"
    assert rep.target_x_m == pytest.approx(0.9 * LW)


def test_aligned_coin_needs_no_move_time():
    """已经对齐的币只留反应时间：0.2s 到站也接（旧规则要 0.58s）。"""
    rep = select(_road(), [], [], [Obj("coin", 0.05 * LW, 0.2)], 0.0, _p())
    assert rep.layer == 3 and rep.why == "coin"


def test_aligned_reward_needs_no_move_time():
    """正前方已对齐的奖励车：到站 0.2s 仍是层 1（旧规则被可达门丢掉）。"""
    rep = select(_road(), [Obj("reward", 0.1 * LW, 0.2)], [], [], 0.0, _p())
    assert rep.layer == 1 and rep.why == "reward"


def test_center_band_no_correction_inside():
    p = _p()
    half = (0.5 - p.ego_half_lane - p.lane_clearance_lane) * LW
    inside = 0.8 * half
    rep = select(_road(), [], [], [], inside, p)
    assert rep.target_x_m == pytest.approx(inside)   # 区间内不修正
    assert rep.maneuver is False
    outside = 1.2 * LW
    rep = select(_road(), [], [], [], outside, p)
    assert rep.target_x_m == pytest.approx(half, abs=0.05)  # 只去最近边界
    assert rep.target_x_m != pytest.approx(0.0, abs=0.05)
    assert rep.maneuver is True


def test_center_anchor_uses_current_station():
    """中心锚点=当前站中心：弯道 8m 外的中心不参与（避免带边左偏）。"""
    rep = select(_road(curve=0.1), [], [], [], 0.0, _p())
    assert rep.layer == 4
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_coarse_lane_takes_center_layer_only():
    """粗层方向只接管 L4：无机会时目标=选定车道的近侧带边（道号相对
    当前中心线），不再原地贴回中心线。机会层不受影响。"""
    half = (0.5 - _p().ego_half_lane - _p().lane_clearance_lane) * LW
    rep = select(_road(), [], [], [], 0.0, _p(), coarse_lane=-1.0)
    assert rep.layer == 4 and rep.why == "center:coarse"
    assert rep.target_x_m == pytest.approx(-LW + half, abs=0.05)
    assert rep.maneuver is True
    rep = select(_road(), [Obj("reward", 2.0, 2.0)], [], [], 0.0, _p(),
                 coarse_lane=-1.0)
    assert rep.layer == 1 and rep.why == "reward"


def test_coarse_lane_zero_keeps_center_band():
    """粗层选中心道（lane=0）不改回中语义：区间内不修正。"""
    x = 0.15 * LW
    rep = select(_road(), [], [], [], x, _p(), coarse_lane=0.0)
    assert rep.why == "center"
    assert rep.target_x_m == pytest.approx(x)
    assert rep.maneuver is False


def test_preposition_biases_toward_future_coin():
    """视界外 1.2 道的币：无即时目标时锚点朝币侧预置，最多 1 道。"""
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 5.0)], 0.0, _p())
    assert rep.layer == 4
    assert rep.target_x_m == pytest.approx(1.0 * LW, abs=0.05)


def test_preposition_ignores_near_and_far_sides():
    # 视界内但来不及的币不参与预置（仍守中心）
    rep = select(_road(), [], [], [Obj("coin", 2.0 * LW, 2.5)], 0.0, _p())
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)
    # 2.5 道外的币不参与预置
    rep = select(_road(), [], [], [Obj("coin", 3.0 * LW, 5.0)], 0.0, _p())
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_preposition_prefers_reward_over_coin():
    rep = select(_road(), [Obj("reward", 0.5 * LW, 5.0)],
                 [], [Obj("coin", -1.0 * LW, 4.0)], 0.0, _p())
    assert rep.target_x_m == pytest.approx(0.5 * LW, abs=0.05)


def test_preposition_disabled_holds_center():
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 5.0)], 0.0,
                 _p(preposition_enabled=False))
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_waypoints_include_pass_and_coin():
    """奖励车在 5s 外，途中有同车道车和顺路币：先擦边、再收币、最后奖励车。"""
    rewards = [Obj("reward", 2.0, 5.0)]
    cars = [Obj("car", 0.0, 2.0)]
    coins = [Obj("coin", 1.8, 3.5)]
    rep = select(_road(), rewards, cars, coins, 0.0, _p(horizon_s=5.0))
    assert rep.layer == 1
    assert len(rep.waypoints) <= 4
    srcs = [w.src for w in rep.waypoints]
    assert srcs[-1] == "reward"
    assert "pass" in srcs
    assert "coin" in srcs
    ts = [w.t for w in rep.waypoints]
    assert ts == sorted(ts)


def test_waypoint_cap_and_monotone_coins():
    """反向金币不绕路；路点最多 4 个。"""
    rewards = [Obj("reward", 2.0, 3.0)]
    coins = [Obj("coin", -2.0, 1.0), Obj("coin", 1.0, 1.5),
             Obj("coin", 1.5, 2.0), Obj("coin", 1.8, 2.5)]
    rep = select(_road(), rewards, [], coins, 0.0, _p())
    assert len(rep.waypoints) <= 4
    assert all(w.x >= -0.2 for w in rep.waypoints)   # 反向币被丢
    assert rep.waypoints[-1].src == "reward"


def test_layer_change_immediate_same_layer_dwell():
    sel = LayeredSelector(_p())
    sel.update(_road(), [], [], [Obj("coin", -1.0, 2.0)], 0.0, 0.05)
    # 同层目标跳 0.6 道（两侧都在 0.5 线内）：驻留未到，目标保持
    rep = sel.update(_road(), [], [], [Obj("coin", 1.0, 2.0)], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(-1.0, abs=0.05)
    for _ in range(5):
        rep = sel.update(_road(), [], [], [Obj("coin", 1.0, 2.0)], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(1.0, abs=0.05)
    # 层变化：奖励车出现立即切
    rep = sel.update(_road(), [Obj("reward", 1.0, 2.0)], [],
                     [Obj("coin", 1.0, 2.0)], 0.0, 0.05)
    assert rep.layer == 1 and rep.target_x_m == pytest.approx(1.0)


def test_coin_hold_then_release_and_override():
    """币组消失后沿最后币线再走 0.6s；奖励车出现立即覆盖。"""
    sel = LayeredSelector(_p())
    sel.update(_road(), [], [], [Obj("coin", 0.3, 2.0)], 0.0, 0.05)
    rep = sel.update(_road(), [], [], [], 0.0, 0.1)
    assert rep.layer == 3 and rep.why == "coin_hold"
    assert rep.target_x_m == pytest.approx(0.3, abs=0.05)
    for _ in range(7):
        rep = sel.update(_road(), [], [], [], 0.0, 0.1)
    assert rep.layer == 4 and rep.why == "center"
    # 再选一次币，然后奖励车覆盖
    sel.update(_road(), [], [], [Obj("coin", 0.3, 2.0)], 0.0, 0.05)
    sel.update(_road(), [], [], [], 0.0, 0.1)
    rep = sel.update(_road(), [Obj("reward", 0.8, 2.0)], [], [], 0.0, 0.05)
    assert rep.layer == 1 and rep.target_x_m == pytest.approx(0.8)


def test_committed_ignores_opportunities_and_preposition():
    """并线承诺期：奖励车/擦边/金币/预置都不许改道，只按承诺道回中。"""
    rep = select(_road(), [Obj("reward", 2.0, 2.0)],
                 [Obj("car", 0.0, 2.0)],
                 [Obj("coin", 1.2 * LW, 2.0)], 0.0, _p(),
                 coarse_lane=1.0, committed=True)
    half = (0.5 - _p().ego_half_lane - _p().lane_clearance_lane) * LW
    assert rep.layer == 4 and rep.why == "commit"
    assert rep.target_x_m == pytest.approx(LW - half, abs=0.05)
    assert [w.src for w in rep.waypoints] == ["commit"]


def test_committed_selector_bypasses_dwell():
    """承诺期换目标立即生效：同层驻留不能拖住并线目标。"""
    sel = LayeredSelector(_p())
    sel.update(_road(), [], [], [], 0.0, 0.05,
               coarse_lane=-1.0, committed=True)
    rep = sel.update(_road(), [], [], [], 0.0, 0.05,
                     coarse_lane=1.0, committed=True)
    half = (0.5 - _p().ego_half_lane - _p().lane_clearance_lane) * LW
    assert rep.why == "commit"
    assert rep.target_x_m == pytest.approx(LW - half, abs=0.05)


def test_no_road_holds():
    road = RoadFrame((), None, None, None, False)
    rep = select(road, [], [], [], 0.5, _p())
    assert rep.layer == 0 and rep.why == "hold:no_road"
    assert rep.target_x_m == pytest.approx(0.5)
