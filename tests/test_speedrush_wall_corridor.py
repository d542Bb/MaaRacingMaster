# -*- coding: utf-8 -*-
"""墙走廊读数回归锁（2026-10-18 用户定案：墙锚定走廊，影子期）。

锁定：绿包红剔除（路上的车/杂块不当墙，护栏保留）、墙走廊提取（两侧墙
内缘中位、单侧覆盖不足弃权）、过宽裁剪（墙间中点±2 道，窄了不处理）。
"""
from __future__ import annotations

import numpy as np
import pytest

from maaracing_master.plugins.speedrush.depth_geo import (
    GRID_BLOCKED, GRID_DRIVABLE, GRID_NX, GRID_NZ, GRID_UNKNOWN,
    DrivableGrid, _strip_enclosed_red, wall_corridor_reading)

LANE_W = 3.38


def _grid(state: np.ndarray) -> DrivableGrid:
    return DrivableGrid(state=state, coef=(0.0, 0.0, 2.0),
                        counts=np.full(state.shape, 9, np.int32),
                        dig_cells=0, latency_ms=1.0)


def _iz(z: float) -> int:
    return int((z - 3.0) / 0.25)


def _ix(x: float) -> int:
    return int((x + 14.0) / 0.25)


def _road(state: np.ndarray) -> None:
    state[:, _ix(-4):_ix(4)] = GRID_DRIVABLE


def test_enclosed_red_removed_wall_kept():
    """绿区包围的红块（路上的车）剔除，护栏（外侧无绿区）保留。"""
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    _road(st)
    st[_iz(8):_iz(9.5), _ix(-2):_ix(-0.5)] = GRID_BLOCKED      # 路上的车
    st[_iz(6):_iz(16), _ix(-6.25):_ix(-5.5)] = GRID_BLOCKED    # 左护栏+外侧路肩
    cleaned = _strip_enclosed_red(st)
    assert (cleaned[_iz(8):_iz(9.5), _ix(-2):_ix(-0.5)]
            == GRID_UNKNOWN).all()                              # 车被剔除
    assert (cleaned[_iz(6):_iz(16), _ix(-6.25):_ix(-5.5)]
            == GRID_BLOCKED).all()                              # 护栏保留


def test_enclosed_red_needs_green_all_sides():
    """护栏 z 端邻格无绿区（近端出视锥无点）→ 不算被包围，保留。"""
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    _road(st)
    st[_iz(6):_iz(16), _ix(-5.75):_ix(-5.5)] = GRID_BLOCKED
    cleaned = _strip_enclosed_red(st)
    assert (cleaned[_iz(6):_iz(16), _ix(-5.75):_ix(-5.5)]
            == GRID_BLOCKED).all()


def test_wall_corridor_overwide_clipped():
    """两侧墙 @±8m：宽 ≈4.8 道 > 4.5 → 裁到 4 道，中心=墙间中点。"""
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    _road(st)
    st[_iz(6):_iz(16), _ix(-8.25):_ix(-8.0)] = GRID_BLOCKED
    st[_iz(6):_iz(16), _ix(8.0):_ix(8.25)] = GRID_BLOCKED
    wc = wall_corridor_reading(_grid(st), LANE_W)
    assert wc.rows_l >= 8 and wc.rows_r >= 8
    assert wc.left_lane == pytest.approx(-8.125 / LANE_W, abs=0.05)
    assert wc.right_lane == pytest.approx(8.125 / LANE_W, abs=0.05)
    assert wc.clipped is True
    assert wc.width_lane == pytest.approx(4.0, abs=1e-6)
    assert wc.center_lane == pytest.approx(0.0, abs=0.02)


def test_wall_corridor_narrow_unclipped():
    """两侧墙 @±5.6m：宽 ≈3.3 道 ≤ 4.5 → 不裁，原样。"""
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    _road(st)
    st[_iz(6):_iz(16), _ix(-5.75):_ix(-5.5)] = GRID_BLOCKED
    st[_iz(6):_iz(16), _ix(5.5):_ix(5.75)] = GRID_BLOCKED
    wc = wall_corridor_reading(_grid(st), LANE_W)
    assert wc.clipped is False
    assert wc.width_lane == pytest.approx(11.25 / LANE_W, abs=0.05)
    assert wc.center_lane == pytest.approx(0.0, abs=0.02)


def test_wall_corridor_needs_both_sides_far_enough():
    """单侧墙覆盖 6 行 < WALL_MIN_ROWS → 该侧弃权，走廊 None（不臆造单侧）。"""
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    _road(st)
    st[_iz(6):_iz(7.5), _ix(-8.25):_ix(-8.0)] = GRID_BLOCKED
    wc = wall_corridor_reading(_grid(st), LANE_W)
    assert wc.rows_l == 6
    assert wc.left_lane is None and wc.right_lane is None
    assert wc.center_lane is None and wc.width_lane is None
