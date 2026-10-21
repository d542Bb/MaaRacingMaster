# -*- coding: utf-8 -*-
"""粗层选道：绿区净空 + 2D 占用、中心线相对、换道迟滞。"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from maaracing_master.plugins.speedrush.coarse import (
    CoarseParams, choose_lane)
from maaracing_master.plugins.speedrush.selection import RoadBand, RoadFrame

LW = 3.38
DATUM = 1.79
P = CoarseParams(wall_margin_lane=0.35, ego_half_lane=0.27,
                 car_half_lane=0.27, switch_margin_m=4.0,
                 lookahead_m=24.0, cam_z_datum_m=DATUM)


@dataclass(frozen=True)
class Car:
    x_m: float
    z_m: float | None = None
    t_arrive_s: float | None = None
    near: bool = False
    kind: str = "car"


def _road(rows, *, l=-6.0, r=6.0):
    """rows: [(z0, z1, left, right)]；缺省两侧 ±6m。"""
    bands = tuple(RoadBand(z0, z1, left, right, src="measured",
                           age_ms=0.0, conf=1.0)
                  for z0, z1, left, right in rows)
    left0 = rows[0][2]
    right0 = rows[0][3]
    return RoadFrame(bands, right0 - left0, left0, right0, True)


def _straight(*, l=-6.0, r=6.0, n=8, step=3.0):
    return _road([(i * step, (i + 1) * step, l, r) for i in range(n)])


def test_empty_road_returns_to_center_from_right_edge():
    rep = choose_lane(_straight(), [], ego_lane=3.0 / LW, lane_w_m=LW, p=P)
    assert rep.lane == 0.0
    assert rep.direction == -1
    assert rep.reason == "space"


def test_green_narrowing_pushes_away_from_wall():
    road = _road([(0, 3, -6, 6), (3, 6, -6, 2.0),
                  (6, 9, -6, 2.0), (9, 12, -6, 2.0)])
    rep = choose_lane(road, [], ego_lane=1.0, lane_w_m=LW, p=P)
    assert rep.lane == 0.0
    assert rep.direction == -1
    # 右道在 3m 处被走廊收窄截断
    right = next(c for c in rep.lanes if c.lane == 1.0)
    assert right.free_m == 3.0 and right.blocker == "space"


def test_car_in_lane_shifts_to_empty_side():
    road = _straight()
    car = Car(x_m=0.0, z_m=5.0 + DATUM)
    rep = choose_lane(road, [car], ego_lane=0.0, lane_w_m=LW, p=P)
    assert rep.lane == -1.0
    assert rep.direction == -1
    lanes = {c.lane: c for c in rep.lanes}
    assert lanes[0.0].free_m == pytest.approx(5.0) and lanes[0.0].blocker == "car"
    assert lanes[-1.0].free_m == 24.0


def test_car_on_other_lane_does_not_block():
    road = _straight()
    car = Car(x_m=3.38, z_m=8.0 + DATUM)
    rep = choose_lane(road, [car], ego_lane=1.0, lane_w_m=LW, p=P)
    lanes = {c.lane: c for c in rep.lanes}
    assert lanes[1.0].free_m == pytest.approx(8.0)
    assert lanes[0.0].free_m == 24.0
    assert rep.lane == 0.0


def test_car_beyond_lookahead_ignored():
    road = _straight()
    car = Car(x_m=0.0, z_m=P.lookahead_m + DATUM + 5.0)
    rep = choose_lane(road, [car], ego_lane=0.0, lane_w_m=LW, p=P)
    lanes = {c.lane: c for c in rep.lanes}
    assert lanes[0.0].free_m == pytest.approx(P.lookahead_m)
    assert lanes[0.0].blocker is None


def test_no_depth_car_uses_time_as_coarse_depth():
    road = _straight()
    car = Car(x_m=0.0, z_m=None, t_arrive_s=0.2)
    rep = choose_lane(road, [car], ego_lane=0.0, lane_w_m=LW, p=P)
    lanes = {c.lane: c for c in rep.lanes}
    assert lanes[0.0].free_m == pytest.approx(0.2 * P.range_speed_mps)
    assert lanes[0.0].blocker == "car"


def test_no_depth_near_car_blocks_current_lane():
    road = _straight()
    car = Car(x_m=0.0, z_m=None, near=True)
    rep = choose_lane(road, [car], ego_lane=0.0, lane_w_m=LW, p=P)
    assert rep.lane == -1.0
    assert next(c for c in rep.lanes if c.lane == 0.0).free_m == 0.5


def test_unknown_far_car_breaks_tie_against_center_hold():
    road = _straight()
    car = Car(x_m=0.0, z_m=None, near=False, t_arrive_s=None)
    # 现任在中心：未知远车不把整道判空，最优仍是空侧
    rep = choose_lane(road, [car], ego_lane=0.0, lane_w_m=LW, p=P,
                      current_lane=0.0)
    assert rep.lane == -1.0
    unknown = next(c for c in rep.lanes if c.lane == 0.0)
    assert unknown.unknown_car is True
    assert unknown.free_m == pytest.approx(P.lookahead_m)


def test_hysteresis_keeps_side_lane_when_center_is_blocked():
    road = _straight()
    car = Car(x_m=0.0, z_m=10.0 + DATUM)
    rep = choose_lane(road, [car], ego_lane=-1.0, lane_w_m=LW, p=P,
                      current_lane=-1.0)
    assert rep.lane == -1.0
    assert rep.direction == 0


def test_center_preference_unlocks_side_lane_when_center_clears():
    road = _straight()
    rep = choose_lane(road, [], ego_lane=-1.0, lane_w_m=LW, p=P,
                      current_lane=-1.0)
    assert rep.lane == 0.0
    assert rep.reason == "space"


def test_no_road_is_honest():
    road = RoadFrame((), None, None, None, False)
    rep = choose_lane(road, [], ego_lane=0.0, lane_w_m=LW, p=P)
    assert rep.reason == "no_road"
    assert rep.direction == 0


def test_occupied_incumbent_switches_even_inside_margin():
    road = _straight()
    # 现任中心道在 21m 处有车，空侧只多 3m（在 4m 迟滞内）也必须并出去
    car = Car(x_m=0.0, z_m=21.0 + DATUM)
    rep = choose_lane(road, [car], ego_lane=0.0, lane_w_m=LW, p=P,
                      current_lane=0.0)
    assert rep.lane == -1.0
    assert rep.reason == "space"


# ---------- 机会侧预置（方向级消费，2026-10-21 方案步骤 4） ----------

def _far_road(cleft, cright):
    """直道 + 末尾一条远粗带（两侧给 cleft/cright，选择坐标）。"""
    bands = tuple(RoadBand(i * 3.0, (i + 1) * 3.0, -6.0, 6.0,
                           src="measured", age_ms=0.0, conf=1.0)
                  for i in range(8))
    coarse = (RoadBand(30.0, 45.0, cleft, cright, src="inferred"),)
    return RoadFrame(bands, 12.0, -6.0, 6.0, True, coarse)


def test_far_direction_prepositions_toward_open_side():
    """机会侧预置：净空与占用全打平时，字典序回中项换成「离远带更开的一侧
    最近」——远带范围中点偏右（右侧更开）时选右道（开关开）。"""
    # 远带 [-6, +10]：中点 +2m ≈ +0.6 道，右侧更开
    road = _far_road(-6.0, 10.0)
    p_on = CoarseParams(wall_margin_lane=0.35, ego_half_lane=0.27,
                        car_half_lane=0.27, switch_margin_m=4.0,
                        lookahead_m=24.0, cam_z_datum_m=DATUM,
                        far_direction=True)
    rep = choose_lane(road, [], ego_lane=0.0, lane_w_m=LW, p=p_on)
    assert rep.lane == 1.0, f"应预置右侧，得 {rep.lane}"
    # 开关关：同一走廊逐位回回中
    rep_off = choose_lane(road, [], ego_lane=0.0, lane_w_m=LW, p=P)
    assert rep_off.lane == 0.0


def test_far_direction_off_is_bit_identical():
    """开关关（或粗带缺侧/无带）→ 无预置：选道与无粗带走廊逐位一致。"""
    plain = _straight()
    with_coarse = RoadFrame(plain.bands, plain.width_m, plain.left_m,
                            plain.right_m, True,
                            (RoadBand(30.0, 45.0, None, None,
                                      src="inferred"),))
    for p in (P, CoarseParams(wall_margin_lane=0.35, ego_half_lane=0.27,
                              car_half_lane=0.27, switch_margin_m=4.0,
                              lookahead_m=24.0, cam_z_datum_m=DATUM,
                              far_direction=True)):
        a = choose_lane(plain, [], ego_lane=0.0, lane_w_m=LW, p=p)
        b = choose_lane(with_coarse, [], ego_lane=0.0, lane_w_m=LW, p=p)
        assert (a.lane, a.direction, a.reason) == (b.lane, b.direction, b.reason)
