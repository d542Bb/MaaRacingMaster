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
    assert rep.target_x_m == pytest.approx(0.0)    # 币心在车身带内：不动线


def test_pass_picks_roomier_side():
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)


def test_both_sides_blocked_holds_center():
    cars = [Obj("car", 0.0, 2.0),
            Obj("car", 0.65 * LW, 2.0),
            Obj("car", -0.65 * LW, 2.0)]
    rep = select(_road(), [], cars, [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_layer_and_center_fallback():
    rep = select(_road(), [], [], [Obj("coin", 0.35, 2.5)], 0.0, _p())
    assert rep.layer == 3 and rep.target_x_m == pytest.approx(0.0)


def test_coin_not_centered_car_has_width():
    """币不必正中：车身带内的币保持原线，带外的只走到带边。"""
    p = _p()
    band = p.ego_half_lane * LW
    # 带内 0.2 道：目标 = 自车横位
    rep = select(_road(), [], [], [Obj("coin", 0.2 * LW, 2.0)], 0.0, p)
    assert rep.layer == 3 and rep.target_x_m == pytest.approx(0.0)
    # 带外 0.8 道：目标 = 币心 − 车身半宽（不是币心）
    rep = select(_road(), [], [], [Obj("coin", 0.8 * LW, 2.0)], 0.0, p)
    assert rep.layer == 3
    assert rep.target_x_m == pytest.approx(0.8 * LW - band)
    rep = select(_road(), [], [], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_off_line_not_selected():
    """无承诺道时只做本道/相邻一道：1.5 道外的散币不直接拽。"""
    rep = select(_road(), [], [], [Obj("coin", 1.5 * LW, 3.0)], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_adjacent_lane_selected():
    """相邻道（0.9 道）的币可以成为主目标；目标按车身带收，不对币心。"""
    p = _p()
    rep = select(_road(), [], [], [Obj("coin", 0.9 * LW, 2.5)], 0.0, p)
    assert rep.layer == 3 and rep.why == "coin"
    assert rep.target_x_m == pytest.approx(0.9 * LW - p.ego_half_lane * LW)


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


# ---------- 擦边预判式（2026-11-02，闸 pass_preposition_enabled） ----------

def _pp():
    return _p(pass_preposition_enabled=True)


def test_pass_preposition_biases_away_from_adjacent_car():
    """0141 复刻：右邻道带来车（1.0 道、1.5s 到站）→ 向远车侧（左）安全
    区间边界起手，why=center:pp。"""
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 1.5)], [], 0.0, _pp())
    assert rep.layer == 4 and rep.why == "center:pp"
    assert rep.target_x_m == pytest.approx(-0.17 * LW, abs=0.02)


def test_pass_preposition_gates_window_side_and_safe():
    """窗外车/t=None/unsafe 车/两侧都有车：不偏；同道贴身仍归 pass 层。"""
    far = select(_road(), [], [Obj("car", 1.0 * LW, 3.5)], [], 0.0, _pp())
    assert far.why == "center"                      # t > horizon
    no_t = select(_road(), [], [Obj("car", 1.0 * LW, None)], [], 0.0, _pp())
    assert no_t.why == "center"                     # 仪器缺位不触发
    unsafe = select(_road(), [],
                    [Obj("car", 1.0 * LW, 1.5, safe=False)], [], 0.0, _pp())
    assert unsafe.why == "center"                   # 不可安全不触发
    both = select(_road(), [], [Obj("car", 1.0 * LW, 1.5),
                                Obj("car", -1.0 * LW, 1.5)], [], 0.0, _pp())
    assert both.why == "center"                     # 无「更宽一侧」
    near = select(_road(), [], [Obj("car", 0.3 * LW, 1.5)], [], 0.0, _pp())
    assert near.layer == 2                          # 同道贴身仍归 pass 层


def test_pass_preposition_gate_off_and_committed():
    """闸关=逐位回今天；并线承诺期无偏置。"""
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 1.5)], [], 0.0, _p())
    assert rep.why == "center"                      # 闸关
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 1.5)], [], 0.0, _pp(),
                 committed=True)
    assert rep.why == "commit"                      # committed 关


# ---------- 预排视界延长（6→10s，2026-11-02） ----------

def test_preposition_extends_to_long_horizon_coin():
    """6~10s 带内的币也参与预置（视界延长后）：币组首检 ~8s 到站不再漏预排。"""
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 8.0)], 0.0,
                 _p(preposition_horizon_s=10.0))
    assert rep.layer == 4
    assert rep.target_x_m == pytest.approx(1.0 * LW, abs=0.05)


def test_preposition_beyond_extended_horizon_excluded():
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 10.5)], 0.0,
                 _p(preposition_horizon_s=10.0))
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_preposition_far_side_gate_holds_in_extended_band():
    """横距门在延长视界内照旧生效：2.5 道外的币不预置。"""
    rep = select(_road(), [], [], [Obj("coin", 3.0 * LW, 8.0)], 0.0,
                 _p(preposition_horizon_s=10.0))
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
    sel.update(_road(), [], [], [Obj("coin", -2.0, 2.0)], 0.0, 0.05)
    # 同层目标跳 >0.5 道：驻留未到，目标保持（币按车身带收，目标=币心∓带）
    rep = sel.update(_road(), [], [], [Obj("coin", 2.0, 2.0)], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(-(2.0 - _p().ego_half_lane * LW),
                                           abs=0.05)
    for _ in range(5):
        rep = sel.update(_road(), [], [], [Obj("coin", 2.0, 2.0)], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(2.0 - _p().ego_half_lane * LW,
                                           abs=0.05)
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
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)   # 币在车身带内

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


# ---------- 跨线唯一入口（2026-10-27 请求生成契约） ----------

HALF = (0.5 - 0.27 - 0.06) * LW    # 承诺道内可活动半径（车心安全区间）


def test_out_of_lane_coin_becomes_request_not_drag():
    """邻道币组：目标回夹本道区间边界，越线意图变请求——车不停在两条道中间。"""
    rep = select(_road(), [], [], [Obj("coin", 1.0 * LW, 2.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.why == "center"
    assert rep.target_x_m == pytest.approx(HALF, abs=0.02)
    assert rep.lane_request == 1.0


def test_in_lane_coin_keeps_line():
    """车身带内的币保持原线：不为一枚偏 0.1 道的币拽方向盘。"""
    rep = select(_road(), [], [], [Obj("coin", 0.1 * LW, 2.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 3 and rep.lane_request is None
    assert rep.target_x_m == pytest.approx(0.0)


def test_two_lane_coin_requests_when_reachable():
    """请求口径 ±2（2026-11-02）：隔一条道的币时间够（光锥可达）→ 发请求，
    判据是光锥不是道距；三道外仍不请求。"""
    rep = select(_road(), [], [], [Obj("coin", 2.0 * LW, 3.0)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request == 2.0
    rep = select(_road(), [], [], [Obj("coin", 3.0 * LW, 3.0)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request is None


def test_out_of_lane_reward_becomes_request():
    rep = select(_road(), [Obj("reward", 1.0 * LW, 2.0)], [], [], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.lane_request == 1.0
    assert rep.target_x_m == pytest.approx(0.0, abs=HALF + 0.02)


def test_same_lane_coin_aims_band_edge():
    """本道内偏一侧的币（0.4 道）：只走到车身带边，不对币心；
    收集判据（0.35 道）比车身带（0.27 道）宽，压在带边仍能吃到。"""
    rep = select(_road(), [], [], [Obj("coin", 0.4 * LW, 2.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 3 and rep.lane_request is None
    assert rep.target_x_m == pytest.approx((0.4 - _p().ego_half_lane) * LW)


def test_pass_keeps_full_graze_authority():
    """擦边/让位**不进门**：正前方 0 道上的车要让到 0.65 道才出得了车半宽
    （0.27）——夹进安全区间（0.17）等于迎面撞上去（用户红线：抵消微调＝撞车）。"""
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)
    assert rep.lane_request is None


def test_pending_request_does_not_preempt_yielding():
    """出跨度的机会**不占目标位**：邻道奖励车/邻道币组与一辆正压自车的车同拍
    出现时，本拍仍让位（±0.65 道），请求延后一拍再说。

    复核打出的真 bug 形态：字典序里让「本拍追不到的机会」占住目标位，等于
    用一条邻道奖励车取消掉让位动作——最长持续整个起意计分窗（6 拍 0.3 s）。"""
    rw = [Obj("reward", 1.0 * LW, 2.0)]
    coins = [Obj("coin", 1.0 * LW, 2.5)]
    cars = [Obj("car", 0.0, 2.0)]
    for opp_kw in ({"rewards": rw, "coins": []}, {"rewards": [], "coins": coins}):
        rep = select(_road(), opp_kw.get("rewards", []), cars,
                     opp_kw.get("coins", []), 0.0, _p(), commit_x_m=0.0)
        assert rep.layer == 2 and rep.why == "pass", opp_kw
        assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)
        assert rep.lane_request is None


def test_request_resumes_after_yielding():
    """让位拍过去后机会仍在 → 请求照常发出（计分只是漏一拍，不清零）。"""
    rw = [Obj("reward", 1.0 * LW, 2.0)]
    rep = select(_road(), rw, [], [], 0.0, _p(), commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request == 1.0


def test_preposition_bias_stays_in_interval():
    """预置偏移同样不越线：未来机会在邻道 → 请求 + 偏移回夹区间。"""
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 5.0)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.lane_request == 1.0
    assert rep.target_x_m == pytest.approx(HALF, abs=0.02)


def test_committed_emits_no_request():
    """并线期不发新请求：横向意图由承诺态持有。"""
    rep = select(_road(), [Obj("reward", 2.0 * LW, 2.0)], [], [], 0.0, _p(),
                 committed=True, commit_x_m=0.0)
    assert rep.why == "commit" and rep.lane_request is None


def test_reach_slack_is_the_same_gate_in_seconds():
    """观测面与判据同源：slack≥0 必须逐点等价于原可达门，负值=本拍来不及。"""
    from maaracing_master.plugins.speedrush.selection import (
        _reachable, reach_slack)
    p = _p()
    for dx_lane in (-2.0, -1.0, -0.4, -0.1, 0.0, 0.1, 0.4, 1.0, 2.0):
        for t in (0.0, 0.2, 0.5, 1.0, 1.5, 2.0, 3.0):
            x = dx_lane * LW
            s = reach_slack(x, 0.0, t, p)
            assert (s is not None and s >= 0.0) == _reachable(x, 0.0, t, p)
    assert reach_slack(0.0, 0.0, None, p) is None
    assert reach_slack(0.0, 0.0, 0.1, p) == pytest.approx(0.1 - p.reaction_s)


def test_unsafe_car_is_ignored_by_safety_consumers():
    """对象表判为不安全的车（超龄/退役/无横位）不参与擦边、让位、占用
    与路点插入；同一辆车新鲜时允许改变结果（1b 注入锁）。"""
    from maaracing_master.plugins.speedrush.selection import _blocked
    p = _p()
    assert _blocked([Obj("car", 0.0, 1.0)], 0.0, 1.0, p) is True
    assert _blocked([Obj("car", 0.0, 1.0, safe=False)], 0.0, 1.0, p) is False
    # 擦边层：不安全车不再触发让位
    rep = select(_road(), [], [Obj("car", 0.0, 2.0, safe=False)], [], 0.0, p)
    assert rep.layer == 4 and rep.why == "center"
    # 路点：顺路的擦边点只在车安全时插入
    from maaracing_master.plugins.speedrush.selection import _waypoints
    wp_safe = _waypoints(1, 1.5, 4.0, [Obj("car", 0.65, 2.0)], [],
                         _road(), 0.0, p)
    wp_unsafe = _waypoints(1, 1.5, 4.0,
                           [Obj("car", 0.65, 2.0, safe=False)], [],
                           _road(), 0.0, p)
    assert "pass" in [w.src for w in wp_safe]
    assert "pass" not in [w.src for w in wp_unsafe]


def test_old_fixed_gate_replaced_by_cone_and_adjacent_rule():
    """1.4 道外的币：旧 1.2 道固定门会丢；有承诺道时按锥+相邻请求裁决。"""
    coin = Obj("coin", 1.4 * LW, 2.5)
    rep = select(_road(), [], [], [coin], 0.0, _p(), commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request == 1.0
    # 无承诺道：只做本道/相邻一道，不直接拽 1.4 道
    rep2 = select(_road(), [], [], [coin], 0.0, _p())
    assert rep2.layer == 4 and rep2.target_x_m == pytest.approx(0.0, abs=0.05)


def test_multi_lane_coin_time_budget_gates_request():
    """±2 请求的资格是光锥：时间不够（到站 0.5s、横移 2 道来不及）不请求；
    时间够由 test_two_lane_coin_requests_when_reachable 覆盖。"""
    rep = select(_road(), [], [], [Obj("coin", 2.0 * LW, 0.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request is None


def test_scoring_hit_carries_pending_adjacent_request():
    """命中拍不再吞请求：另一辆邻道奖励车的挂起请求随报告带出。"""
    rewards = [Obj("reward", 1.0 * LW, 2.2), Obj("reward", 0.1 * LW, 2.5)]
    rep = select(_road(), rewards, [], [], 0.0, _p(), commit_x_m=0.0)
    assert rep.layer == 1 and rep.why == "reward"
    assert rep.lane_request == 1.0


def test_safety_hit_discards_pending_request():
    """擦边/让位命中时挂起请求本拍丢弃（安全动作优先，路线 pass_hold）。"""
    rep = select(_road(), [Obj("reward", 1.0 * LW, 2.2)],
                 [Obj("car", 0.0, 2.0)], [], 0.0, _p(), commit_x_m=0.0)
    assert rep.why == "pass" and rep.lane_request is None


def test_selector_horizon_override_changes_layer():
    """4b 接线：本拍视界覆盖只作用于选择/预置，不改驻留参数。"""
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    sel = LayeredSelector(_p())
    coin = [Obj("coin", 0.0, 2.5)]
    assert sel.update(_road(), [], [], coin, 0.0, 0.05).layer == 3
    sel.reset()
    rep = sel.update(_road(), [], [], coin, 0.0, 0.05, horizon_s=2.0)
    assert rep.layer == 4        # 2.5s 机会出 2s 视界，只剩预置（x=0）


def test_layer_dwell_delays_scoring_layer_but_not_pass():
    """4c：计分层切换需 0.2s；擦边/让位层永远立即。"""
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    sel = LayeredSelector(_p(layer_dwell=True))
    reward = [Obj("reward", 0.0, 2.0)]
    assert sel.update(_road(), reward, [], [], 0.0, 0.05).layer == 1
    coin = [Obj("coin", 0.0, 1.0)]
    for _ in range(4):                   # 0.15s（含首拍 0）：仍在旧层
        assert sel.update(_road(), [], [], coin, 0.0, 0.05).layer == 1
    assert sel.update(_road(), [], [], coin, 0.0, 0.05).layer == 3
    # 擦边层立即接管，不等驻留
    car = [Obj("car", 0.0, 2.0)]
    assert sel.update(_road(), [], car, [], 0.0, 0.05).layer == 2
    # 离开擦边层也立即
    assert sel.update(_road(), reward, [], [], 0.0, 0.05).layer == 1


def test_layer_dwell_default_off_switches_immediately():
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    sel = LayeredSelector(_p())
    sel.update(_road(), [Obj("reward", 0.0, 2.0)], [], [], 0.0, 0.05)
    assert sel.update(_road(), [], [], [Obj("coin", 0.0, 1.0)],
                      0.0, 0.05).layer == 3


def test_event_replan_small_step_and_safety_override():
    """4c：无输入事件时目标 0.1 道/拍小步修正；replan 与擦边层立即采纳。"""
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    p = _p(event_replan=True, switch_dwell_s=0.0)
    sel = LayeredSelector(p)
    r1 = sel.update(_road(), [Obj("reward", 0.0, 2.0)], [], [], 0.0, 0.05)
    assert r1.layer == 1 and r1.target_x_m == pytest.approx(0.0)
    r2 = sel.update(_road(), [Obj("reward", 0.8 * LW, 2.0)], [], [],
                    0.0, 0.05)
    assert r2.layer == 1
    assert r2.target_x_m == pytest.approx(0.1 * LW)
    r3 = sel.update(_road(), [Obj("reward", 0.8 * LW, 2.0)], [], [],
                    0.0, 0.05, replan=True)
    assert r3.target_x_m == pytest.approx(0.8 * LW)
    r4 = sel.update(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, 0.05)
    assert r4.layer == 2            # 擦边层不等事件


def test_pass_hold_gate_off_drops_past_car():
    """闸关：t<0 的车（已并排/略过）不进擦边候选——逐位回现行行为。"""
    rep = select(_road(), [], [Obj("car", 0.0, -0.3)], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_pass_hold_keeps_graze_until_clear():
    """闸开：并排/刚脱离（t∈[−hold,0]）保持擦边目标，免可达/阻挡检查。

    极限超车判定在"脱离并排那一帧"——层提前塌回中心撤销回避，交付
    横距散布 0.08~1.35 道（窗口 [0.54, 0.77]），就是交付失败的主体。"""
    rep = select(_road(), [], [Obj("car", 0.0, -0.3)], [], 0.0,
                 _p(pass_hold_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)


def test_pass_hold_pending_car_still_beats_past_car():
    """闸开排序：来得及的未到车（t≥0）优先于已并排的（t<0），不倒挂。"""
    cars = [Obj("car", 0.0, -0.2), Obj("car", 0.0, 2.5)]
    rep = select(_road(), [], cars, [], 0.0, _p(pass_hold_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"
    wp_t = [w.t for w in rep.waypoints if w.src == "pass"]
    assert wp_t and wp_t[0] == pytest.approx(2.5)


def test_pass_min_gap_rejects_squeeze_band():
    """闸开：走廊夹取把目标压进 [半宽和, min_gap) 危险带即弃车。

    窄路（1 道）里车偏一侧，另一侧被夹回只剩 0.57 道——闸关时接受
    （>半宽和合法但离碰撞线只剩 0.03 道，横移噪声 ±0.15 道半数撞上）；
    闸开时两侧都给不出 0.6 道 → 不擦，回中心。"""
    car_x = 0.42 * LW
    rep = select(_road(width_lanes=1.0), [], [Obj("car", car_x, 2.0)],
                 [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m - car_x) == pytest.approx(0.57 * LW, abs=0.02)
    rep2 = select(_road(width_lanes=1.0), [], [Obj("car", car_x, 2.0)],
                  [], 0.0, _p(pass_hold_enabled=True))
    assert rep2.layer == 4 and rep2.why == "center"


def test_pass_in_commit_gate_off_stays_commit():
    """闸关：承诺期擦边不运行（现行行为——承诺期 1~3 层关闭）。"""
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p(),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 4 and rep.why == "commit"


def test_pass_in_commit_dodges_during_merge():
    """闸开：承诺道内的擦边候选（帧 #1530 实景：出租车在承诺道车表）
    擦边层放行——安全动作不被并线锁死（2.0s=可达窗口内）。"""
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0,
                 _p(pass_in_commit_enabled=True),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 2 and rep.why == "pass"


def test_pass_horizon_extends_candidates():
    """视界放宽：3.15s 出头的刹车主车（帧 #2017 实景）闸关被 3.0s 排除、
    闸开进入候选——躲不躲仍由可达性判据把关。"""
    far = Obj("car", 0.0, 3.15)
    rep = select(_road(), [], [far], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"
    rep2 = select(_road(), [], [Obj("car", 0.0, 3.15)], [], 0.0,
                  _p(pass_horizon_s=4.5))
    assert rep2.layer == 2 and rep2.why == "pass"


def test_graze_adjacent_car_leans_toward_it():
    """主动贴边（2026-11-03）：邻道 0.9 道的车、3s 到站——不再只回中吃 30
    分，目标贴到车心近侧 graze_gap（极限超车 150 分的入口）。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 2 and rep.why == "graze"
    assert rep.target_x_m == pytest.approx(0.9 * LW - 0.68 * LW, abs=0.06)


def test_graze_same_lane_emergency_wins_over_adjacent():
    """同道贴身车按到站排序先于邻道贴边：躲车（pass）优先于擦车（graze）。
    邻道车摆在 1.3 道（贴边带外缘）——0.9 道会占住同道车的闪避位（占用
    否决正确，不是本用例要验的东西）；同道车 2.0s=可达窗口内。"""
    cars = [Obj("car", 1.3 * LW, 3.0), Obj("car", 0.0, 2.0)]
    rep = select(_road(), [], cars, [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"


def test_graze_adjacent_needs_ample_time():
    """时门：邻道车 1s 到站太急（lean_min=2s）不起意贴边——临时起意=撞车，
    落回中心带（紧急躲避仍由同道带与车进半宽后接管）。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 1.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 4 and rep.why == "center"


def test_graze_target_blocked_by_other_car():
    """贴边位被别车占住（到站相近）时不贴：graze 候选让位，不挤进车缝。"""
    cars = [Obj("car", 0.9 * LW, 3.0), Obj("car", 0.25 * LW, 3.1)]
    rep = select(_road(), [], cars, [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.why != "graze"


def test_graze_gate_off_keeps_reactive_only():
    """闸关=逐位回今天：邻道车不进擦边候选，落回中心带。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_graze_committed_stays_reactive_only():
    """承诺期不开贴边（改道契约不动）：邻道车在车表也不得改道。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True,
                    pass_in_commit_enabled=True),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 4 and rep.why == "commit"


def test_graze_hit_drops_pending_lane_request():
    """贴边命中拍丢弃挂起请求：贴着一辆车的同时不再起并线（意图打架源）。"""
    rep = select(_road(), [Obj("reward", 1.5 * LW, 2.5)],
                 [Obj("car", 0.9 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True), commit_x_m=0.0)
    assert rep.why == "graze" and rep.lane_request is None
