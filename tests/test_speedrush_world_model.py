# -*- coding: utf-8 -*-
"""A1 归一尺子回归锁（经 Tracker 生产口径，原 build_world 用例迁移）。

锁住四件事：
1. **归一公式**——x_lane 与手算值一致（公式 = trick ②c 预注册判据同族，不许漂）；
2. **本车道读零**——正前方目标（cx=EGO_CX）的 x_lane ≈ 0（自车锚正确）；
3. **地平线过滤**——cy ≤ y_h 的框丢弃（消失点附近归一发散，带病值不下发）；
4. **排序契约**——targets/far_targets 各按距离由近及远（cy 降序），跨类别统一排。
"""

from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.perception import Detection, PerceptionResult
from maaracing_master.plugins.speedrush.tracking import Tracker
from maaracing_master.plugins.speedrush.world_model import (
    KIND_BONUS, KIND_CAR, KIND_COIN, Calib, load_calib)

CAL = load_calib()


def _per(cars=(), coins=(), bonuses=()):
    return PerceptionResult(frame_id=1, ts_ns=1, cars=list(cars),
                            coins=list(coins), bonuses=list(bonuses), infer_ms=1.0)


def _observe(cars=(), coins=(), bonuses=()):
    """喂一帧感知 → WorldObservation（生产唯一口径：Tracker.update）。"""
    return Tracker().update(_per(cars=cars, coins=coins, bonuses=bonuses),
                            frame_age_ms=1.0, stage=0)


def _hand_x(cx: int, cy: int, cal: Calib = CAL) -> float:
    return ((cx - cal.vpx) / (cy - cal.y_h)
            - (cal.ego_cx - cal.vpx) / (cal.v_ego - cal.y_h)) * cal.a_x


class TestNormalize:
    def test_matches_hand_formula(self):
        d = Detection(cx=700, cy=500, w=60, h=80, conf=0.9)
        obs = _observe(cars=[d])
        assert obs.targets[0].x_lane == pytest.approx(_hand_x(700, 500), abs=1e-9)

    def test_ego_lane_reads_zero(self):
        # 正前方（自车列）的目标横向读 ~0
        d = Detection(cx=int(CAL.ego_cx), cy=520, w=50, h=60, conf=0.8)
        obs = _observe(cars=[d])
        assert abs(obs.targets[0].x_lane) < 0.01

    def test_right_positive_left_negative(self):
        right = _observe(cars=[Detection(800, 500, 50, 60, 0.9)])
        left = _observe(cars=[Detection(480, 500, 50, 60, 0.9)])
        assert right.targets[0].x_lane > 0 > left.targets[0].x_lane


class TestFilterAndOrder:
    def test_above_horizon_dropped(self):
        bad = Detection(cx=640, cy=int(CAL.y_h), w=10, h=10, conf=0.9)
        ok = Detection(cx=640, cy=400, w=10, h=10, conf=0.9)
        obs = _observe(cars=[bad, ok])
        assert [t.cy for t in obs.targets] == [400]

    def test_far_target_kept_in_far_tuple(self):
        # 适用域外（分母 < MIN_DENOM）：目标保留（距离序有效），进 far_targets
        # 只给粗方向不给横向数值；域内的进 targets 带 x_lane
        MIN_DENOM = CAL.min_denom
        far = Detection(cx=700, cy=int(CAL.y_h + MIN_DENOM) - 1, w=10, h=10, conf=0.7)
        near = Detection(cx=700, cy=int(CAL.y_h + MIN_DENOM) + 1, w=10, h=10, conf=0.7)
        obs = _observe(cars=[far, near])
        assert [t.cy for t in obs.targets] == [near.cy]
        assert obs.targets[0].x_lane is not None
        assert [g.cy for g in obs.far_targets] == [far.cy]

    def test_sorted_near_to_far_across_kinds(self):
        obs = _observe(
            cars=[Detection(700, 450, 40, 50, 0.8)],
            coins=[Detection(640, 600, 20, 20, 0.6), Detection(640, 380, 20, 20, 0.6)],
            bonuses=[Detection(500, 520, 90, 90, 0.7)])
        merged = sorted(obs.targets + obs.far_targets,
                        key=lambda t: -t.cy)
        assert [t.cy for t in merged] == [600, 520, 450, 380]
        assert [t.kind for t in merged] == [KIND_COIN, KIND_BONUS, KIND_CAR, KIND_COIN]

    def test_kind_labels(self):
        obs = _observe(cars=[Detection(700, 500, 40, 50, 0.8)],
                       coins=[Detection(640, 500, 20, 20, 0.6)],
                       bonuses=[Detection(560, 500, 90, 90, 0.7)])
        assert {t.kind for t in obs.targets} == {KIND_COIN, KIND_CAR, KIND_BONUS}

    def test_empty_perception(self):
        obs = _observe()
        assert obs.targets == () and obs.far_targets == ()
