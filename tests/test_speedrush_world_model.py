# -*- coding: utf-8 -*-
"""世界模型层（阶段 B）契约回归锁。

锁住四件事：
1. **归一公式**——x_lane 与手算值一致（公式 = trick ②c 预注册判据同族，不许漂）；
2. **本车道读零**——正前方目标（cx=EGO_CX）的 x_lane ≈ 0（自车锚正确）；
3. **地平线过滤**——cy ≤ y_h+margin 的框丢弃（消失点附近归一发散，带病值不下发）；
4. **排序契约**——输出按距离由近及远（cy 降序），跨类别统一排。
"""

from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.perception import Detection, PerceptionResult
from maaracing_master.plugins.speedrush.world_model import Calib, EGO_CX, build_world

CAL = Calib()


def _per(cars=(), coins=(), bonuses=()):
    return PerceptionResult(frame_id=1, ts_ns=1, cars=list(cars),
                            coins=list(coins), bonuses=list(bonuses), infer_ms=1.0)


def _hand_x(cx: int, cy: int, cal: Calib = CAL) -> float:
    return ((cx - cal.vpx) / (cy - cal.y_h)
            - (EGO_CX - cal.vpx) / (cal.v_ego - cal.y_h)) * cal.a_x


class TestNormalize:
    def test_matches_hand_formula(self):
        d = Detection(cx=700, cy=500, w=60, h=80, conf=0.9)
        t = build_world(_per(cars=[d]))[0]
        assert t.x_lane == pytest.approx(_hand_x(700, 500), abs=1e-9)

    def test_ego_lane_reads_zero(self):
        # 正前方（自车列）的目标横向读 ~0
        d = Detection(cx=int(EGO_CX), cy=520, w=50, h=60, conf=0.8)
        t = build_world(_per(cars=[d]))[0]
        assert abs(t.x_lane) < 0.01

    def test_right_positive_left_negative(self):
        right = build_world(_per(cars=[Detection(800, 500, 50, 60, 0.9)]))[0]
        left = build_world(_per(cars=[Detection(480, 500, 50, 60, 0.9)]))[0]
        assert right.x_lane > 0 > left.x_lane


class TestFilterAndOrder:
    def test_above_horizon_dropped(self):
        bad = Detection(cx=640, cy=int(CAL.y_h), w=10, h=10, conf=0.9)
        ok = Detection(cx=640, cy=400, w=10, h=10, conf=0.9)
        out = build_world(_per(cars=[bad, ok]))
        assert [t.cy for t in out] == [400]

    def test_far_target_kept_with_none_xlane(self):
        # 适用域外（分母 < MIN_DENOM）：目标保留（距离序有效），横向量=None
        from maaracing_master.plugins.speedrush.world_model import MIN_DENOM
        far = Detection(cx=700, cy=int(CAL.y_h + MIN_DENOM) - 1, w=10, h=10, conf=0.7)
        near = Detection(cx=700, cy=int(CAL.y_h + MIN_DENOM) + 1, w=10, h=10, conf=0.7)
        out = build_world(_per(cars=[far, near]))
        assert len(out) == 2
        assert out[0].cy == near.cy and out[0].x_lane is not None
        assert out[1].cy == far.cy and out[1].x_lane is None

    def test_sorted_near_to_far_across_kinds(self):
        per = _per(
            cars=[Detection(700, 450, 40, 50, 0.8)],
            coins=[Detection(640, 600, 20, 20, 0.6), Detection(640, 380, 20, 20, 0.6)],
            bonuses=[Detection(500, 520, 90, 90, 0.7)])
        out = build_world(per)
        assert [t.cy for t in out] == [600, 520, 450, 380]
        assert [t.kind for t in out] == ["coin", "bonus", "car", "coin"]

    def test_kind_labels(self):
        out = build_world(_per(cars=[Detection(700, 500, 40, 50, 0.8)],
                               coins=[Detection(640, 500, 20, 20, 0.6)],
                               bonuses=[Detection(560, 500, 90, 90, 0.7)]))
        assert {t.kind for t in out} == {"coin", "car", "bonus"}

    def test_empty_perception(self):
        assert build_world(_per()) == []
