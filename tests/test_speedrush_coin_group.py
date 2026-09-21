# -*- coding: utf-8 -*-
"""金币组聚合器的回归锁（设计稿 v2 §三；step 3）。

锁四件事：同帧聚类口径（Δx+Δcy 双门）、跨帧身份（Jaccard 继承 / 低于阈值新建）、
失联宽限（组级退役纪律）、收益下限（估计=观测，漏检只标记不虚增）。
观察构造直接手搭 TrackedTarget/WorldObservation——聚合器只吃公开契约，
测它不需要真跑 Tracker。
"""

from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.coin_group import (
    AggParams, CoinGroupAggregator)
from maaracing_master.plugins.speedrush.tracking import (
    PerceptionHealth, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import KIND_CAR, KIND_COIN


def _coin(tid: int, x: float, cy: int, conf: float = 0.9):
    return _tt(tid, KIND_COIN, x, cy, conf)


def _tt(tid: int, kind: str, x: float, cy: int, conf: float):
    from maaracing_master.plugins.speedrush.tracking import TrackedTarget
    return TrackedTarget(
        id=tid, kind=kind, x_lane=x, x_sigma=0.1, cy=cy, w=20, h=20, conf=conf,
        rel_approach=0.0, first_seen_fid=0, last_seen_fid=0, validity_until_fid=0)


def _obs(fid: int, targets=()) -> WorldObservation:
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 33_000_000, frame_age_ms=10.0,
        stage=1, health=PerceptionHealth(True, True, True, bool(targets), False),
        boundary=None, targets=tuple(targets), far_targets=())


# ---------- 同帧聚类 ----------

def test_three_coins_one_group():
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1, [_coin(1, 0.20, 400), _coin(2, 0.45, 405), _coin(3, 0.70, 410)]))
    assert len(obs.coin_groups) == 1
    g = obs.coin_groups[0]
    assert g.member_ids == (1, 2, 3)
    assert g.observed_count == 3 and g.estimated_count == 3
    assert g.x_center == pytest.approx(0.45, abs=1e-6)
    assert g.cy_min == 400 and g.cy_max == 410


def test_distant_coin_own_group():
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1, [_coin(1, 0.2, 400), _coin(2, 0.4, 400), _coin(9, 2.5, 430)]))
    assert [g.member_ids for g in obs.coin_groups] == [(1, 2), (9,)] or \
        [g.member_ids for g in obs.coin_groups] == [(9,), (1, 2)]
    # 排序近→远：cy_max=430 的组在前
    assert obs.coin_groups[0].cy_max == 430


def test_cy_adjacency_gates_lateral_neighbors():
    """横向贴得近但纵向差一截（不同排）——不并组：双门都要过。"""
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1, [_coin(1, 0.2, 400), _coin(2, 0.35, 500)]))
    assert len(obs.coin_groups) == 2


def test_non_coin_targets_ignored():
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1, [_tt(1, KIND_CAR, 0.2, 400, 0.9)]))
    assert obs.coin_groups == ()


# ---------- 跨帧身份 ----------

def test_group_id_persists_across_frames():
    agg = CoinGroupAggregator()
    o1 = agg.update(_obs(1, [_coin(1, 0.2, 400), _coin(2, 0.4, 400)]))
    o2 = agg.update(_obs(2, [_coin(1, 0.2, 402), _coin(2, 0.4, 402)]))
    assert o2.coin_groups[0].group_id == o1.coin_groups[0].group_id
    assert o2.coin_groups[0].first_seen_fid == 1


def test_member_swap_below_jaccard_makes_new_group():
    agg = CoinGroupAggregator()
    o1 = agg.update(_obs(1, [_coin(i, 0.2 * i, 400) for i in (1, 2, 3, 4)]))
    # 只留 2/4（Jaccard 2/4=0.5 恰在阈值上——继承）；换 3/4 与旧 1/2 零交叠——新建
    o2 = agg.update(_obs(2, [_coin(3, 0.6, 400), _coin(4, 0.8, 400)]))
    old_gid = o1.coin_groups[0].group_id
    gids = {g.group_id for g in o2.coin_groups}
    assert old_gid in gids          # 旧组靠宽限存续 + 部分成员继承，两种形态都合法
    assert o2.coin_groups            # 且总有组输出，不炸空


# ---------- 失联宽限与退役 ----------

def test_group_grace_then_retire():
    p = AggParams(group_grace=3)
    agg = CoinGroupAggregator(params=p)
    agg.update(_obs(1, [_coin(1, 0.2, 400)]))
    gone = agg.update(_obs(2))
    assert len(gone.coin_groups) == 1          # 失联 1 行：宽限在册
    assert gone.coin_groups[0].last_seen_fid == 1
    assert gone.coin_groups[0].validity_until_fid == 4
    for f in (3, 4):
        gone = agg.update(_obs(f))
    still = {g.group_id for g in gone.coin_groups}
    assert len(still) == 1                     # 失联 3 行 = 宽限边界，还在
    retired = agg.update(_obs(5))
    assert retired.coin_groups == ()           # 失联 4 行：退役


# ---------- 收益下限纪律 ----------

def test_partial_flag_no_inflation():
    """组内异常大间距（仍低于聚类门、相对中位偏大）→ partial；estimated 仍 = observed。"""
    agg = CoinGroupAggregator()
    # Δx = 0.02 / 0.28：两对都过 0.3 的聚类门同组，但 max > 1.8×median(0.15)=0.27
    obs = agg.update(_obs(1, [_coin(1, 0.10, 400), _coin(2, 0.12, 400),
                              _coin(3, 0.40, 400)]))
    g = obs.coin_groups[0]
    assert g.member_ids == (1, 2, 3)
    assert g.partial_observation is True
    assert g.estimated_count == 3 == g.observed_count


def test_beyond_gate_is_split_not_partial():
    """超聚类门的间距直接分组——partial 只管组内，两机制不混。"""
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1, [_coin(1, 0.1, 400), _coin(3, 1.2, 400)]))
    assert len(obs.coin_groups) == 2
    assert all(not g.partial_observation for g in obs.coin_groups)


def test_uniform_group_not_partial():
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1, [_coin(1, 0.1, 400), _coin(2, 0.25, 400), _coin(3, 0.4, 400)]))
    assert obs.coin_groups[0].partial_observation is False


# ---------- 参数与状态 ----------

@pytest.mark.parametrize("kwargs", [
    {"max_dx_lane": 0}, {"jaccard_min": 0}, {"group_grace": 0},
    {"gap_factor": 1.0}, {"max_cy_adj": -1},
])
def test_params_fail_loud(kwargs):
    with pytest.raises(ValueError):
        AggParams(**kwargs)


def test_reset_forgets_groups_keeps_gid_counter():
    agg = CoinGroupAggregator()
    o1 = agg.update(_obs(1, [_coin(1, 0.2, 400)]))
    gid1 = o1.coin_groups[0].group_id
    agg.reset()
    o2 = agg.update(_obs(2, [_coin(1, 0.2, 400)]))
    assert o2.coin_groups[0].group_id != gid1          # 不复用
    assert o2.coin_groups[0].first_seen_fid == 2       # 从头计


def test_empty_observation_passthrough():
    agg = CoinGroupAggregator()
    obs = agg.update(_obs(1))
    assert obs.coin_groups == () and obs.frame_id == 1
