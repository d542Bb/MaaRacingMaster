# -*- coding: utf-8 -*-
"""边界感知层 v1（HSV 黄线 → BoundarySummary）的回归锁。

核心口径是**免费的强判据**：透视正确时直道路缘归一后 x_lane 沿行恒定
（路缘是过消失点的直线）。合成帧按这个口径构造——直线全部过 (vpx, y_h)，
弯道用不过 VP 的线模拟。断言锁行为（validity/残差方向性），不锁绝对值。
"""

from __future__ import annotations

import numpy as np

from maaracing_master.plugins.speedrush.boundary import detect_boundary
from maaracing_master.plugins.speedrush.tracking import BoundarySummary
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()   # 几何真源 gate0.json
YELLOW = (255, 200, 40)  # RGB：H≈24 S 高 V 高，落在旧栈验证过的黄色范围内
BG = (40, 45, 50)      # 非黄底色（沥青灰蓝）


def _frame() -> np.ndarray:
    return np.full((720, 1280, 3), BG, dtype=np.uint8)


def _line(img: np.ndarray, x_of_y, width=8) -> None:
    for y in range(int(CAL.y_h) + 25, 720):
        xc = int(round(x_of_y(y)))
        if 0 <= xc < img.shape[1]:
            img[y, max(0, xc - width // 2):min(img.shape[1], xc + width // 2 + 1)] = YELLOW


def _straight(k: float):
    """过 VP、归一横向恒定的路缘：x = vpx ± k·(y−y_h)。"""
    return lambda y: CAL.vpx - k * (y - CAL.y_h)


def test_straight_road_valid_and_low_residual():
    img = _frame()
    _line(img, _straight(1.5))
    _line(img, lambda y: CAL.vpx + 1.5 * (y - CAL.y_h))
    s = detect_boundary(img, CAL)
    assert isinstance(s, BoundarySummary)
    assert s.schema_version == 1
    assert s.validity is True
    assert s.left_x < s.right_x
    assert s.road_width > 100
    assert s.straight_residual < 0.2        # 过 VP → 残差只剩 1px 量化噪声（≈0.02/px）
    assert s.uncertainty == s.uncertainty and s.uncertainty < 3.0      # NaN-safe 且稳
    if s.vp_row is not None:
        assert abs(s.vp_row) < 15          # 交点就在地平线附近，漂移≈0


def test_curve_raises_residual():
    """不过 VP 的"弯道"路缘：x_lane 沿行系统漂移——残差判**量级关系**不赌临界值。"""
    img = _frame()
    _line(img, lambda y: CAL.vpx - 1.5 * (y - CAL.y_h) + 0.35 * (y - 500))
    _line(img, lambda y: CAL.vpx + 1.5 * (y - CAL.y_h) - 0.35 * (y - 500))
    s = detect_boundary(img, CAL)
    assert s.validity is True                     # 检测本身有效……
    ref = detect_boundary(_straight_frame(), CAL)  # 同仪器直道对照
    assert s.straight_residual > 10 * ref.straight_residual  # 残差高一量级=判据在工作
    assert s.vp_row is not None and abs(s.vp_row) > 20       # 假交点漂移也如实偏离


def _straight_frame() -> np.ndarray:
    img = _frame()
    _line(img, _straight(1.5))
    _line(img, lambda y: CAL.vpx + 1.5 * (y - CAL.y_h))
    return img


def test_no_lines_invalid_not_crash():
    s = detect_boundary(_frame(), CAL)
    assert s.validity is False
    assert np.isnan(s.road_width)


def test_single_side_invalid():
    """只检到左缘：coverage（双侧行占比）不达标——单边摘要不足以喂校验层。"""
    img = _frame()
    _line(img, _straight(1.5))
    s = detect_boundary(img, CAL)
    assert s.validity is False


def test_noise_blobs_invalid():
    """零散小黄点（<MIN_RUN_PX 宽/高）不得当成路缘。"""
    img = _frame()
    rng = np.random.default_rng(7)
    for _ in range(200):
        y = int(rng.integers(360, 560))
        x = int(rng.integers(0, 1280))
        img[y:y + 2, x:x + 4] = YELLOW
    s = detect_boundary(img, CAL)
    assert s.validity is False


def test_summary_defaults_declared():
    """契约字段的口径注释（上沿取 x、下沿取宽）被合成帧锁定。"""
    img = _frame()
    _line(img, _straight(1.5))
    _line(img, lambda y: CAL.vpx + 1.5 * (y - CAL.y_h))
    s = detect_boundary(img, CAL)
    top_y = int(CAL.y_h) + 30
    assert abs(s.left_x - (CAL.vpx - 1.5 * (top_y - CAL.y_h))) < 6   # 中点对中点，容画线厚度
    # 上沿宽度 < 下沿宽度（透视收拢）：road_width 取下沿才是"最宽最稳"
    assert s.road_width > (s.right_x - s.left_x)
