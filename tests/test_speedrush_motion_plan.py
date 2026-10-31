# -*- coding: utf-8 -*-
"""A1 场景快照契约：帧/符号/单位、年龄与 σ、走廊陈旧、空场景。"""
from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.corridor import (
    BandBoundary, Corridor, CorridorBand)
from maaracing_master.plugins.speedrush.decision import (
    DecisionEngine, _monotonic_coin_t)
from maaracing_master.plugins.speedrush.motion_plan import (
    EgoState, PlanGate, Scene, _Cand, build_scene, plan_motion)
from maaracing_master.plugins.speedrush.selection import (
    BonusSource, CarSource, CoinSource, RoadBand, RoadFrame)
from maaracing_master.plugins.speedrush.traffic import CarView
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DistanceSource, MetricCarState, PerceptionHealth,
    TrackedTarget, WorldObservation)

LW = 3.38
CFG = _read_decision(DECISION_FILE)


def _road(*, src="measured", age_ms=None, conf=1.0):
    return RoadFrame(
        (RoadBand(0.0, 6.0, -4.0, 4.0, src=src, age_ms=age_ms, conf=conf),),
        8.0, -4.0, 4.0, True)


def _scene(**kw):
    args = dict(
        fid=10, ego_m=1.0, road=_road(), rewards=[], cars=[], coins=[],
        executed_lane=0.0, origin=0.0, lane_w_m=LW, frame_age_ms=20.0,
        corridor_age_ms=50.0, max_obj_age_s=0.25, corridor_age_max_s=0.25,
        sigma_obj0_m=0.15, sigma_obj_rate_mps=0.6, cam_z_datum_m=1.79)
    args.update(kw)
    return build_scene(**args)


def test_frame_symbols_and_units():
    s = _scene(origin=5.0, rewards=[
        BonusSource(10.0, 1.0, 0.9, age_ms=100.0, src="pixel")],
        coins=[CoinSource(8.0, 3, 0.8, 1.5, age_ms=200.0)],
        cars=[CarSource(12.0, 0.5, z_m=5.0, age_ms=50.0, src="metric")])
    r, car, c = s.objs
    assert (r.kind, c.kind, car.kind) == ("reward", "coin", "car")
    assert r.x_m == pytest.approx(5.0)          # 源 - origin
    assert c.x_m == pytest.approx(3.0)
    # 车按 z 处中心线回正（本测试路心恒 0）
    assert car.x_m == pytest.approx(7.0)
    assert r.sigma_m == pytest.approx(0.15 + 0.6 * 0.1)
    assert c.sigma_m == pytest.approx(0.15 + 0.6 * 0.2)
    assert s.ego_lane == pytest.approx(1.0 / LW)


def test_frame_shift_moves_scene_to_planner_center():
    s = _scene(origin=5.0, ego_m=1.0, frame_shift_m=2.0,
               rewards=[BonusSource(10.0, 1.0, 0.9, age_ms=0.0)])
    assert s.ego_x_m == pytest.approx(3.0)
    assert s.objs[0].x_m == pytest.approx(7.0)
    assert s.road.left_m == pytest.approx(-2.0)
    assert s.road.right_m == pytest.approx(6.0)
    assert s.road.bands[0].left == pytest.approx(-2.0)
    assert _scene().ego_x_m == pytest.approx(1.0)   # 默认不平移


def test_large_frame_shift_holds_plan_for_pd():
    """场景/自车参考系差超过硬门：计划不得按错帧的走廊接管。"""
    s = _scene(road=_wide_road(), ego_m=3.0)
    p = _plan(s, ego=EgoState(0.0, 0.0, 45.0, 0.0))
    assert not p.valid and p.reason == "plan:frame_shift"
    # 亚米级分歧仍按不确定处理，不退回
    s2 = _scene(road=_wide_road(), ego_m=0.5)
    p2 = _plan(s2, ego=EgoState(0.0, 0.0, 45.0, 0.0))
    assert p2.valid


def test_old_objects_dropped_and_corridor_stale():
    s = _scene(rewards=[BonusSource(0.0, 1.0, 1.0, age_ms=300.0)],
               corridor_age_ms=300.0)
    assert s.objs == ()                          # 超龄丢弃
    assert s.stale is True
    assert _scene(corridor_age_ms=None).stale is False


def test_empty_scene():
    s = _scene()
    assert s.objs == () and s.stale is False
    assert s.as_trace()["n"] == 0


def test_stale_targets_survive_with_decayed_arrival():
    """保鲜窗内：目标保留、到站时间按龄前移；已到站的不再当未来目标。"""
    s = _scene(coins=[CoinSource(5.0, 3, 0.9, 1.5, age_ms=300.0)],
               rewards=[BonusSource(2.0, 1.0, 0.9, age_ms=300.0)],
               max_obj_age_s=0.6)
    coin = next(o for o in s.objs if o.kind == "coin")
    reward = next(o for o in s.objs if o.kind == "reward")
    assert coin.t_arrive_s == pytest.approx(1.2)
    assert reward.t_arrive_s == pytest.approx(0.7)
    assert coin.sigma_m == pytest.approx(0.15 + 0.6 * 0.3)
    s2 = _scene(coins=[CoinSource(5.0, 3, 0.9, 0.2, age_ms=300.0)],
                max_obj_age_s=0.6)
    assert s2.objs == ()


def test_pixel_car_age_is_freshness_not_track_lifetime():
    """持续被检出的老轨：场景年龄按最后真实读数算，不按轨道生命期蒸发。"""
    eng = DecisionEngine(CFG)
    v = CarView(id=9, x_lane=1.0, cy=500, rel_approach=5.0, d_min=1.0,
                age_ticks=200, last_seen_fid=10)
    eng.update(_obs(10), 0.05, executed_lane=0.0,
               corridor=_corridor(fid=10, last_fid=10),
               traffic=((v,), ()))
    car = next(o for o in eng.scene.objs if o.kind == "car")
    assert car.age_ms == pytest.approx(0.0)
    # 10 帧没再看到（20Hz → 500ms）：仍在 0.6s 保鲜窗内，带龄保留
    eng.update(_obs(20), 0.05, executed_lane=0.0,
               corridor=_corridor(fid=20, last_fid=20),
               traffic=((v,), ()))
    car = next(o for o in eng.scene.objs if o.kind == "car")
    assert car.age_ms == pytest.approx(500.0)
    # 20 帧没再看到（1000ms）：超保鲜窗
    eng.update(_obs(30), 0.05, executed_lane=0.0,
               corridor=_corridor(fid=30, last_fid=30),
               traffic=((v,), ()))
    assert not any(o.kind == "car" for o in eng.scene.objs)


def test_metric_car_x_is_time_smoothed_across_ticks():
    """同一跟踪 id 的米制横位跳 4m：场景里按 α 收敛，不直接喂给规划器。"""
    eng = DecisionEngine(CFG)
    a = CFG.traffic.metric_x_ema_alpha
    xs = []

    def mc(x):
        return MetricCarState(
            target_id=7, source=DistanceSource.ANCHOR, z_m=20.0, x_m=x,
            v_close_mps=10.0, t_meet_s=2.0, anchor_age_ms=10.0)

    for fid, x in ((1, 5.0), (2, 1.0), (3, 1.0)):
        eng.update(_obs(fid, metric_cars=(mc(x),)), 0.05,
                   executed_lane=0.0,
                   corridor=_corridor(fid=fid, last_fid=fid))
        car = next(o for o in eng.scene.objs if o.kind == "car")
        xs.append(car.x_m)
    assert xs[0] == pytest.approx(5.0)
    assert xs[1] == pytest.approx(5.0 + a * (1.0 - 5.0))
    assert xs[2] == pytest.approx(xs[1] + a * (1.0 - xs[1]))


def test_coin_time_only_moves_closer():
    assert _monotonic_coin_t(None, 3.0, 0.0, 0.25) == pytest.approx(3.0)
    # 同组从 1.5s 跳到 3.0s：判为组里更远的一枚，沿用旧时间按经过时间前移
    assert _monotonic_coin_t((1.5, 10), 3.0, 0.1, 0.25) \
        == pytest.approx(1.4)
    # 正常接近：接受新值
    assert _monotonic_coin_t((1.5, 10), 1.3, 0.1, 0.25) \
        == pytest.approx(1.3)


def test_untimed_far_car_is_not_a_static_wall():
    """t=None 且纵向不近：不再当静态障碍（否则远处 60m 的车会挡住整条车道）。"""
    car = dict(t_meet_s=None, z_m=None, age_ms=0.0, src="pixel")
    far = _plan(_scene(cars=[CarSource(1.0, near=False, **car)]))
    near = _plan(_scene(cars=[CarSource(1.0, near=True, **car)]))
    assert far.collision_cost == 0.0
    assert near.collision_cost > 0.0
def _corridor(*, fid=1, last_fid=1):
    bands = tuple(CorridorBand(
        -2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
        BandBoundary(-4.0, "measured", 1.0, last_fid),
        BandBoundary(4.0, "measured", 1.0, last_fid)) for i in range(3))
    return Corridor(bands, 0.0, 8.0 / LW, "pair", "ok")


def _obs(fid, groups=(), targets=(), metric_cars=()):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, bool(groups or targets), False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups,
        metric_cars=tuple(metric_cars), fid_rate_hz=20.0)


def _group(fid, n=3, x=0.4):
    members = tuple(TrackedTarget(
        id=100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=470 + i * 3, w=20, h=20, conf=0.85, rel_approach=2.5,
        first_seen_fid=1, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n))
    g = CoinGroup(group_id=1, member_ids=tuple(m.id for m in members),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=470,
                  cy_max=470 + 3 * (n - 1), conf_min=0.85,
                  partial_observation=False, first_seen_fid=1,
                  last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, members


def test_engine_exposes_scene_and_clears_without_road():
    eng = DecisionEngine(CFG)
    g, ms = _group(3)
    out = None
    for fid in (1, 2, 3):
        out = eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                         executed_lane=0.0, corridor=_corridor(fid=fid,
                                                               last_fid=fid))
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
    assert out.reason == "sel:coin"
    scene = eng.scene
    assert isinstance(scene, Scene)
    assert scene.fid == 3 and len(scene.objs) == 1
    assert scene.objs[0].kind == "coin"
    assert scene.objs[0].count == 3
    # 组上次观测在上一拍：20Hz 下年龄=50ms
    assert scene.objs[0].age_ms == pytest.approx(50.0)

    eng.update(_obs(3, groups=(g,), targets=ms), 0.05,
               executed_lane=0.0, corridor=None)
    assert eng.scene is None


def test_corridor_stale_uses_band_age():
    eng = DecisionEngine(CFG)
    # last_fid 远落后于当前 fid：20Hz 下 10 拍=500ms > 250ms
    out = eng.update(_obs(11), 0.05, executed_lane=0.0,
                     corridor=_corridor(fid=11, last_fid=1))
    assert out.reason == "sel:center"
    assert eng.scene is not None and eng.scene.stale is True


# ---------- A2 评估器 ----------

def _plan(scene, *, ego=None, motion=None, gate=None, intent_lane=0.0):
    return plan_motion(scene, ego or EgoState(0.0, 0.0, 45.0, 0.0),
                       motion or CFG.motion, CFG.selection, CFG.planner, gate,
                       intent_lane=intent_lane)


def _wide_road():
    return RoadFrame((RoadBand(0.0, 6.0, -5.0, 5.0, src="measured",
                               age_ms=0.0, conf=1.0),), 10.0, -5.0, 5.0, True)


def test_candidates_and_determinism():
    s = _scene(road=_wide_road())
    a, b = _plan(s), _plan(s)
    n_base = len(CFG.motion.heading_levels) + 1          # 绝对档位 + 当前朝向
    n_rev = sum(1 for u in CFG.motion.heading_levels
                if abs(u) >= CFG.motion.reverse_min_rad) + 1  # 半程回正（含当前）
    assert len(a.cand_x) == n_base + n_rev + 1   # + 意图跟踪候选
    assert (a.chosen, a.score_key, a.x_preview_m) == \
        (b.chosen, b.score_key, b.x_preview_m)


def test_reverse_candidate_brakes_toward_center():
    """半程回正候选存在时，带横向速度冲过路心的车不再只按恒朝向候选选：
    计划必须给出朝路心（负）的车头角，且预测路径在 2s 内回到路心附近。"""
    s = _scene(road=_wide_road(), ego_m=1.0)
    p = _plan(s, ego=EgoState(1.0 / LW, 0.047, 45.0, 0.4), intent_lane=0.0)
    assert p.valid and p.layer == 2                     # 意图在带外 = 动作
    assert p.psi_feedforward < 0.4                      # 不是继续右打
    end = p.cand_x[p.chosen][-1]
    assert abs(end) < 1.0                             # 末端回到路心 1m 内


def test_scene_opportunities_do_not_steer_the_plan():
    """单一意图：场景里的金币/奖励车不再改变计划落点。得分目标只能由决策的
    intent 给出——两套价值会各自算出一个目标、按谁本拍有效轮流打方向。"""
    base = _plan(_scene(road=_wide_road(), ego_m=0.0))
    with_coin = _plan(_scene(
        road=_wide_road(), ego_m=0.0,
        coins=[CoinSource(1.5, 5, 0.9, 1.0, age_ms=0.0)]))
    with_reward = _plan(_scene(
        road=_wide_road(), ego_m=0.0,
        rewards=[BonusSource(1.5, 1.0, 0.9, age_ms=0.0)]))
    for p in (with_coin, with_reward):
        assert p.chosen == base.chosen
        assert p.x_preview_m == pytest.approx(base.x_preview_m)
    # 同样的机会只经由 intent 起作用
    aimed = _plan(_scene(road=_wide_road(), ego_m=0.0), intent_lane=0.9)
    assert aimed.cand_x[aimed.chosen][-1] > base.cand_x[base.chosen][-1] + 0.5 * LW


def test_intent_shifts_the_tracking_candidate():
    """唯一意图：决策给的道就是计划要去的道（-1 道 → 末端明显偏左）。"""
    s = _scene(road=_wide_road(), ego_m=0.0)
    base = _plan(s)
    run = _plan(s, intent_lane=-1.0)
    assert run.layer == 2                               # 跨道 = 动作
    assert run.psi_feedforward < base.psi_feedforward
    assert run.cand_x[run.chosen][-1] < base.cand_x[base.chosen][-1] - 0.5 * LW


def test_center_deadband_holds_inside_band():
    """带内不修正：小偏移时选直行，不追路心。"""
    p = _plan(_scene(road=_wide_road(), ego_m=0.5),
              ego=EgoState(0.5 / LW, 0.0, 45.0, 0.0))
    assert p.valid and p.layer == 4
    assert p.psi_feedforward == pytest.approx(0.0)


def test_center_holds_current_heading_inside_band():
    """带内无机会时保持正在进行的朝向，不无故回正。"""
    p = _plan(_scene(road=_wide_road(), ego_m=0.0),
              ego=EgoState(0.0, 0.005, 45.0, 0.0))
    assert p.valid and p.layer == 4
    assert p.psi_feedforward == pytest.approx(0.005)


def test_maintenance_caps_stick_action_does_not():
    """意图就在中心带内=维护：杆幅受限，不得用车头角去追厘米级偏差；
    意图要跨道=动作：放开全权（否则并线永远打不出去）。"""
    ego = EgoState(3.0 / LW, 0.0, 45.0, 0.0)
    hold = _plan(_scene(road=_wide_road(), ego_m=3.0), ego=ego,
                 intent_lane=3.0 / LW)
    assert hold.valid and hold.layer == 4
    assert abs(hold.psi_feedforward) <= CFG.motion.center_heading_rad
    run = _plan(_scene(road=_wide_road(), ego_m=3.0), ego=ego,
                intent_lane=0.0)
    assert run.valid and run.layer == 2
    assert abs(run.psi_feedforward) > CFG.motion.center_heading_rad
def test_untimed_depth_car_ahead_biases_sideways():
    """有距无速：本车道前方有纵深、没到站时间的车也要提前往空侧并，
    不能等到它进入 2s 视界（2026-10-19 局 204308_p1 的直撞根因）。"""
    car = CarSource(0.0, None, z_m=35.0, age_ms=0.0, src="metric")
    p = _plan(_scene(road=_wide_road(), ego_m=0.0, cars=[car]),
              ego=EgoState(0.0, 0.0, 45.0, 0.0))
    assert p.valid and p.layer == 4
    assert abs(p.psi_feedforward) >= 0.05       # 明确改朝向（≥2.9°）
    base = _plan(_scene(road=_wide_road(), ego_m=0.0),
                 ego=EgoState(0.0, 0.0, 45.0, 0.0))
    assert abs(base.psi_feedforward) < 0.05


def test_untimed_depth_car_out_of_lane_does_not_bias():
    car = CarSource(3.5, None, z_m=35.0, age_ms=0.0, src="metric")
    p = _plan(_scene(road=_wide_road(), ego_m=0.0, cars=[car]),
              ego=EgoState(0.0, 0.0, 45.0, 0.0))
    assert p.valid and abs(p.psi_feedforward) < 0.05


def test_late_timed_car_blocks_lane_change_toward_it():
    """到站时间短于碰撞提前量的车，横移躲不开，但不得主动往它那侧并。"""
    base = _plan(_scene(road=_wide_road(), ego_m=0.0),
                 ego=EgoState(0.0, 0.0, 45.0, 0.0), intent_lane=0.85)
    assert base.psi_feedforward >= 0.05
    late = CarSource(3.0, 0.2, z_m=20.0, age_ms=0.0, src="metric")
    p = _plan(_scene(road=_wide_road(), ego_m=0.0, cars=[late]),
              ego=EgoState(0.0, 0.0, 45.0, 0.0), intent_lane=0.85)
    assert p.psi_feedforward < base.psi_feedforward
    assert p.psi_feedforward <= 0.05


def test_metric_x_hard_jump_is_held_until_confirmed():
    """单拍米制横位跳 5m 先拒（真实横移不可能这么快），连续 3 拍才接受。"""
    eng = DecisionEngine(CFG)
    view = CarView(id=7, x_lane=0.3, cy=500, rel_approach=5.0, d_min=0.3,
                   age_ticks=1, last_seen_fid=1)
    m = MetricCarState(target_id=7, source=DistanceSource.ANCHOR,
                       z_m=4.0, x_m=1.0, v_close_mps=None, t_meet_s=None,
                       anchor_age_ms=0.0)
    eng.update(_obs(1, metric_cars=(m,)), 0.05,
               executed_lane=0.0, corridor=_corridor(fid=1, last_fid=1),
               traffic=((view,), ()))
    car = next(o for o in eng.scene.objs if o.kind == "car")
    assert car.x_m == pytest.approx(1.0, abs=0.05)
    for fid in (2, 3):
        v = replace(view, last_seen_fid=fid)
        eng.update(_obs(fid, metric_cars=(replace(m, x_m=6.0),)), 0.05,
                   executed_lane=0.0,
                   corridor=_corridor(fid=fid, last_fid=fid),
                   traffic=((v,), ()))
        car = next(o for o in eng.scene.objs if o.kind == "car")
        assert car.x_m == pytest.approx(1.0, abs=0.05)   # 跳变被拒
    v = replace(view, last_seen_fid=4)
    eng.update(_obs(4, metric_cars=(replace(m, x_m=6.0),)), 0.05,
               executed_lane=0.0, corridor=_corridor(fid=4, last_fid=4),
               traffic=((v,), ()))
    car = next(o for o in eng.scene.objs if o.kind == "car")
    assert car.x_m > 2.0                                # 连续 3 拍：接受


def test_collision_is_soft_cost_not_infeasible():
    """普通车碰撞只扣价值，不再把全部候选判成不可行。"""
    cars = [CarSource(-2.0, 0.5, age_ms=0.0),
            CarSource(0.0, 0.5, age_ms=0.0),
            CarSource(2.0, 0.5, age_ms=0.0)]
    p = _plan(_scene(road=_wide_road(), cars=cars))
    assert p.valid
    assert p.reason in ("plan:ok", "plan:ok_uncertain", "plan:ok_shortened")
    assert p.n_feasible > 0


def test_soft_margin_is_cost_not_infeasible():
    """软余量侵入 3cm、物理边界仍安全：候选必须留在可行集里付代价，
    不能整层判死再去选满杆「最快逃离」。"""
    road = RoadFrame((RoadBand(0.0, 6.0, -5.0, 5.0, src="measured",
                               age_ms=0.0, conf=1.0),), 10.0, -5.0, 5.0, True)
    ego_m = 3.85
    s = _scene(road=road, ego_m=ego_m)
    p = _plan(s, ego=EgoState(ego_m / LW, 0.0, 45.0, 0.0),
              intent_lane=ego_m / LW)
    assert p.valid and p.layer == 4
    assert p.corridor_viol_m == pytest.approx(0.0, abs=1e-9)
    assert p.corridor_soft_viol_m > 0.0
    assert abs(p.psi_feedforward) <= CFG.motion.center_heading_rad


def test_corridor_all_infeasible_holds_position():
    road = RoadFrame((RoadBand(0.0, 6.0, 6.0, 9.0, src="measured",
                               age_ms=0.0, conf=1.0),), 3.0, 6.0, 9.0, True)
    p = _plan(_scene(road=road, ego_m=0.0))
    assert not p.valid and p.reason == "plan:infeasible_corridor"


def test_infeasible_corridor_escapes_toward_open_side():
    """自车压在左包络内（贴墙行驶的日常态）且正向右逃离：可行集为空时
    必须选「最快脱离侵入」。旧兜底选「位移最小」，等于满舵左抵消逃离速度，
    把车钉在护栏上（192643_p2 fid 3653~3670 连续 0.85s 左满舵撞栏）。"""
    road = RoadFrame((RoadBand(0.0, 6.0, -6.9, 8.0, src="measured",
                               age_ms=0.0, conf=1.0),), 14.9, -6.9, 8.0, True)
    s = _scene(road=road, ego_m=-6.54)
    p = _plan(s, ego=EgoState(s.ego_lane, 0.028, 45.0, 0.28))    # 正向右逃离
    assert not p.valid and p.reason == "plan:infeasible_corridor"
    assert p.corridor_viol_m > 0.0
    assert p.psi_feedforward > 0.0                          # 继续向右脱离


def test_empty_corridor_intersection_not_unconstrained():
    """交集为空时不得当成“无走廊约束”放行。"""
    road = RoadFrame((RoadBand(0.0, 6.0, 4.0, 5.0, src="measured",
                               age_ms=0.0, conf=1.0),), 1.0, 4.0, 5.0, True)
    p = _plan(_scene(road=road))
    assert not p.valid and p.reason == "plan:infeasible_corridor"


def test_offcenter_band_still_constrains():
    """整条近带都在路心左侧时也要参与交集，不能按“右缘≤0”跳过。"""
    road = RoadFrame((RoadBand(0.0, 6.0, -6.0, -2.0, src="measured",
                               age_ms=0.0, conf=1.0),), 4.0, -6.0, -2.0, True)
    p = _plan(_scene(road=road, ego_m=0.0))
    assert not p.valid and p.reason == "plan:infeasible_corridor"


def test_corridor_checked_only_near_term():
    """恒定杆 2s 必然驶出包络；走廊只验前 1s，不能把必要避让判成不可行。"""
    road = RoadFrame((RoadBand(0.0, 6.0, -6.0, 6.0, src="measured",
                               age_ms=0.0, conf=1.0),), 12.0, -6.0, 6.0, True)
    p = _plan(_scene(road=road, ego_m=0.0))
    assert p.valid
    assert max(max(c) for c in p.cand_x) > 6.0


def test_preview_velocity_is_rollout_velocity():
    from maaracing_master.plugins.speedrush.planner import step
    s = _scene(road=_wide_road(),
               coins=[CoinSource(1.5, 5, 0.9, 1.0, age_ms=0.0)])
    p = _plan(s)
    x, psi = s.ego_lane, 0.0
    for _ in range(int(round(CFG.motion.preview_s / CFG.motion.dt_s))):
        x, psi = step(x, psi, p.psi_feedforward, 45.0,
                      CFG.motion.dt_s, CFG.planner)
    assert p.v_preview_lane_s == pytest.approx(45.0 * math.tan(psi) / LW)
    assert p.x_preview_lane == pytest.approx(
        (x * LW - p.frame_delta_m) / LW)
def test_imminent_car_skipped_by_reaction_time():
    """反应时内到站的车横向已来不及，不得把全部候选判成碰撞不可行。"""
    p = _plan(_scene(road=_wide_road(),
                     cars=[CarSource(0.0, 0.1, age_ms=0.0)]))
    assert p.valid


def test_stale_scene_invalid():
    p = _plan(_scene(road=_wide_road(), corridor_age_ms=999.0))
    assert not p.valid and p.reason == "plan:stale"


def test_sigma_ego_shortens_horizon():
    m = replace(CFG.motion, sigma_ego0_m=0.1,
                sigma_ego_rate_mps=2.0, sigma_ego_max_m=0.5)
    p = _plan(_scene(road=_wide_road()), motion=m)
    assert p.reason == "plan:ok_shortened"
    assert len(p.cand_x[0]) < int(round(CFG.motion.horizon_s / CFG.motion.dt_s)) + 1


def _c(idx, value, feasible=True, center=0.0, layer=4, collision=0.0):
    key = (not feasible, -value, 0.0, center, 0.0, idx)
    return _Cand(idx, 0.0, (0.0,), 0.0, 0.0, feasible, 9.0,
                 center, 0.0, layer, key, value,
                 collision_cost=collision)


def test_gate_requires_value_margin_and_dwell():
    g = PlanGate(switch_margin=30.0, dwell_s=0.1, epsilon=1e-3,
                 geom_margin=0.03)
    inc, better = _c(3, 100.0), _c(4, 200.0)
    assert g.choose([inc], 0.05).idx == 3
    assert g.choose([better, inc], 0.05).idx == 3      # 第一次只计 streak
    assert g.choose([better, inc], 0.05).idx == 4      # 0.1s 后接管
    # 价值差小于 margin：维持现任
    tiny = _c(5, 120.0)
    assert g.choose([tiny, better], 0.05).idx == 4
    # 同价值时，中心改善超过几何门槛也应换（否则卡死在直行候选）
    cur = _c(6, 0.0, center=1.0)
    near = _c(7, 0.0, center=0.5)
    g2 = PlanGate(switch_margin=30.0, dwell_s=0.1, epsilon=1e-3,
                  geom_margin=0.03)
    assert g2.choose([cur], 0.05).idx == 6
    assert g2.choose([near, cur], 0.05).idx == 6
    assert g2.choose([near, cur], 0.05).idx == 7
    # 避撞型大价值差（碰撞代价更低）首拍立即切
    g3 = PlanGate(switch_margin=15.0, dwell_s=0.1, epsilon=1e-3,
                  geom_margin=0.03, immediate_margin=100.0)
    bad, good = _c(8, -200.0, collision=180.0), _c(9, 0.0)
    assert g3.choose([bad], 0.05).idx == 8
    assert g3.choose([good, bad], 0.05).idx == 9


def test_gate_score_jump_needs_dwell():
    """得分机会新出现不得单帧拽杆：擦边/金币/奖励的价值（90~240）都高于
    immediate=100，旧逻辑首拍就切；2026-10-30 人工局里计划层擦边段 36 段
    有 25 段只活 1 拍，杆值跟着一拍一换。得分类升级必须走 dwell。"""
    g = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                 geom_margin=0.03, immediate_margin=100.0)
    base, coin = _c(0, 0.0), _c(1, 120.0, layer=3)
    assert g.choose([base], 0.05).idx == 0
    for _ in range(3):                                  # 单帧闪断三拍不动
        assert g.choose([coin, base], 0.05).idx == 0
    assert g.choose([coin, base], 0.05).idx == 1        # 持续 0.2s 后接管
    # 奖励车（240）同理，不因价值大跌就跳过确认
    g2 = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                  geom_margin=0.03, immediate_margin=100.0)
    base2, reward = _c(0, 0.0), _c(1, 240.0, layer=1)
    assert g2.choose([base2], 0.05).idx == 0
    assert g2.choose([reward, base2], 0.05).idx == 0


def test_gate_demotes_stale_full_stick_without_dwell():
    """现任自己失效时不等 dwell：擦边出窗后满杆候选掉到 0 价值且带超限罚，
    必须本拍让位；否则空路会继续按上一拍的满杆 0.2~0.3s。"""
    def _capped(idx, value, overcap, center=0.0, collision=0.0):
        key = (False, -value, overcap, center, 0.0, idx)
        return _Cand(idx, 0.0, (0.0,), 0.0, 0.0, True, 9.0,
                     center, 0.0, 4, key, value,
                     collision_cost=collision)

    g = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                 geom_margin=0.03, immediate_margin=100.0)
    stale = _capped(0, 0.0, 1e6, center=5.0)
    capped = _capped(5, 0.0, 0.0, center=0.5)
    assert g.choose([stale], 0.05).idx == 0
    # 首拍就切：现任从限幅外变成超限候选，驻留不适用
    assert g.choose([capped, stale], 0.05).idx == 5
    # 反向：现任碰撞风险更高同样立即让位（不是只有「最佳变好」才切）
    g2 = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                  geom_margin=0.03, immediate_margin=100.0)
    assert g2.choose([_capped(1, -50.0, 0.0, collision=100.0)], 0.05).idx == 1
    assert g2.choose([_capped(2, 100.0, 0.0, collision=0.0)], 0.05).idx == 2


def test_gate_holds_incumbent_through_brief_infeasible_flicker():
    """现任短暂不可行不立刻让位；连续超过宽限才换。"""
    g = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                 geom_margin=0.03, infeasible_grace_s=0.1)
    inc, other = _c(3, 100.0), _c(4, 110.0)
    assert g.choose([inc], 0.05).idx == 3
    assert g.choose([other], 0.05).idx == 3      # 第一拍闪断：保持
    assert g.choose([other], 0.05).idx == 4      # 0.1s 宽限到期：让位


def test_gate_reset_drops_precommit_hold():
    """相位切换清门：并线第一拍不得把上一拍的机会层候选带进承诺期。"""
    g = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                 geom_margin=0.03, infeasible_grace_s=0.1)
    inc, other = _c(3, 100.0), _c(4, 110.0)
    assert g.choose([inc], 0.05).idx == 3
    g.reset()
    assert g.choose([other], 0.05).idx == 4


def test_gate_immediate_margin_overrides_infeasible_grace():
    """避撞级价值差不等宽限。"""
    g = PlanGate(switch_margin=15.0, dwell_s=0.2, epsilon=1e-3,
                 geom_margin=0.03, immediate_margin=10.0,
                 infeasible_grace_s=0.2)
    assert g.choose([_c(3, 100.0)], 0.05).idx == 3
    assert g.choose([_c(4, 200.0)], 0.05).idx == 4


def test_planner_tracks_valid_plan_only():
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    from maaracing_master.plugins.speedrush.tracking import (
        DecisionOutput, DecisionState)
    s = _scene(road=_wide_road(), ego_m=0.0)
    p = _plan(s, intent_lane=0.5)
    out = DecisionOutput(schema_version=5, state=DecisionState.CRUISE,
                         x_target=0.0, reason="x", emitted_fid=1,
                         valid_until_fid=10)
    pl = LateralPlanner(CFG.planner)
    # 计划给的是车头角指令，执行层用同一条朝向律换成杆值（无朝向证据→ψ̂=0）
    exp = max(-1.0, min(1.0, CFG.planner.k_psi * p.psi_feedforward))
    pl.update(out, 0.05, 1, plan=p)
    assert pl.last_steer_raw == pytest.approx(exp)

    pl2 = LateralPlanner(CFG.planner)
    pl2.update(out, 0.05, 1, plan=replace(p, valid=False))
    assert pl2.last_steer_raw != pytest.approx(exp)

    # 无效计划必须逐位退回旧 x_target 路径
    out2 = replace(out, x_target=0.5)
    pl3 = LateralPlanner(CFG.planner)
    pl4 = LateralPlanner(CFG.planner)
    pl3.update(out2, 0.05, 1)
    pl4.update(out2, 0.05, 1, plan=replace(p, valid=False))
    assert pl4.last_steer_raw == pytest.approx(pl3.last_steer_raw)


def test_plan_tracking_caps_maintenance_not_collision():
    """layer4 无碰撞代价：维护性计划不得放大成满杆；
    value<0（碰撞/距离代价）保留全权避让。"""
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    from maaracing_master.plugins.speedrush.tracking import (
        DecisionOutput, DecisionState)
    out = DecisionOutput(schema_version=5, state=DecisionState.CRUISE,
                         x_target=0.0, reason="x", emitted_fid=1,
                         valid_until_fid=10)
    plan = SimpleNamespace(valid=True, psi_feedforward=0.14, x_preview_lane=2.0,
                           v_preview_lane_s=0.0, layer=4, value_total=0.0,
                           collision_cost=0.0)
    pl = LateralPlanner(CFG.planner)
    pl.update(out, 0.05, 1, plan=plan)
    assert pl.last_steer_raw == pytest.approx(CFG.planner.hold_stick_max)
    pl2 = LateralPlanner(CFG.planner)
    # 只有真的预测到碰撞才解除维护杆幅；"有距无速"的小代价不算
    pl2.update(out, 0.05, 1, plan=SimpleNamespace(
        valid=True, psi_feedforward=0.14, x_preview_lane=2.0,
        v_preview_lane_s=0.0, layer=4, value_total=-60.0,
        collision_cost=60.0))
    assert pl2.last_steer_raw > CFG.planner.hold_stick_max


def test_plan_feedforward_slew_limits_candidate_step():
    """计划换候选前馈阶跃：单拍杆值变化不许超过配置上限。"""
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    from maaracing_master.plugins.speedrush.tracking import (
        DecisionOutput, DecisionState)
    s = _scene(road=_wide_road(), ego_m=0.0)
    p = _plan(s)
    out = DecisionOutput(schema_version=5, state=DecisionState.CRUISE,
                         x_target=0.0, reason="x", emitted_fid=1,
                         valid_until_fid=10)
    pl = LateralPlanner(CFG.planner)
    pl.update(out, 0.05, 1, plan=p)
    u1 = pl.last_steer_raw
    jumped = replace(p, psi_feedforward=p.psi_feedforward + 0.8)
    pl.update(out, 0.05, 2, plan=jumped)
    cap = CFG.planner.plan_u_slew_s * 0.05
    assert abs(pl.last_steer_raw - u1) <= cap + 1e-6


def test_plan_never_overrides_fsm_states():
    """CONSERVE/FAULT/ABORT 的旧安全分支优先于计划。"""
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    from maaracing_master.plugins.speedrush.tracking import (
        DecisionOutput, DecisionState)
    p = _plan(_scene(road=_wide_road(),
                     coins=[CoinSource(1.5, 5, 0.9, 1.0, age_ms=0.0)]))
    huge = replace(p, psi_feedforward=1.0, x_preview_lane=5.0,
                   v_preview_lane_s=5.0)
    for state, xt in ((DecisionState.CONSERVE, None),
                      (DecisionState.FAULT, 1.0),
                      (DecisionState.ABORT, 0.0)):
        out = DecisionOutput(schema_version=5, state=state, x_target=xt,
                             reason="x", emitted_fid=1, valid_until_fid=10)
        a = LateralPlanner(CFG.planner)
        b = LateralPlanner(CFG.planner)
        a.update(out, 0.05, 1, plan=huge)
        b.update(out, 0.05, 1)
        assert a.last_steer_raw == pytest.approx(b.last_steer_raw)


def test_metric_z_large_jump_resets_instead_of_lagging():
    """并列车真实纵深 21m→5m 是快速通过，不是抖动；固定 EMA 会拖成 13m。"""
    eng = DecisionEngine(CFG)
    view = CarView(id=8, x_lane=0.3, cy=500, rel_approach=5.0, d_min=0.3,
                   age_ticks=1, last_seen_fid=1)
    m = MetricCarState(target_id=8, source=DistanceSource.ANCHOR,
                       z_m=21.3, x_m=2.0, v_close_mps=None, t_meet_s=None,
                       anchor_age_ms=0.0)
    eng.update(_obs(1, metric_cars=(m,)), 0.05, executed_lane=0.0,
               corridor=_corridor(fid=1, last_fid=1),
               traffic=((view,), ()))
    for fid in (2, 3):
        v = replace(view, last_seen_fid=fid)
        eng.update(_obs(fid, metric_cars=(replace(m, z_m=5.4),)), 0.05,
                   executed_lane=0.0,
                   corridor=_corridor(fid=fid, last_fid=fid),
                   traffic=((v,), ()))
    car = next(o for o in eng.scene.objs if o.kind == "car")
    assert car.z_m == pytest.approx(5.4, abs=0.2)


def test_metric_z_reset_also_unlocks_bad_x_hold():
    """新近锚点到达时 x 必须从新基线重来，不能被旧远锚的 x 硬跳变门锁死。"""
    eng = DecisionEngine(CFG)
    view = CarView(id=9, x_lane=0.3, cy=500, rel_approach=5.0, d_min=0.3,
                   age_ticks=1, last_seen_fid=1)
    far = MetricCarState(target_id=9, source=DistanceSource.ANCHOR,
                         z_m=20.0, x_m=-18.0, v_close_mps=None,
                         t_meet_s=None, anchor_age_ms=0.0)
    eng.update(_obs(1, metric_cars=(far,)), 0.05, executed_lane=0.0,
               corridor=_corridor(fid=1, last_fid=1),
               traffic=((view,), ()))
    near = replace(far, z_m=3.3, x_m=-2.6)
    for fid in (2, 3):
        v = replace(view, last_seen_fid=fid)
        eng.update(_obs(fid, metric_cars=(near,)), 0.05, executed_lane=0.0,
                   corridor=_corridor(fid=fid, last_fid=fid),
                   traffic=((v,), ()))
    car = next(o for o in eng.scene.objs if o.kind == "car")
    assert -5.0 < car.x_m < 0.0
def test_heading_levels_resolve_the_pass_band():
    """档位粒度要能落进计分带：1.3~2s 到站时 0.1 档约 0.8 道、0 档原地，
    中间必须有 0.05 级落点，否则候选总在「贴包络」和「过远」之间跳。"""
    levels = CFG.motion.heading_levels
    assert 0.05 in levels and -0.05 in levels