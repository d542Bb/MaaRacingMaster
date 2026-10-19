# -*- coding: utf-8 -*-
"""墙线拟合回归锁（2026-10-18 走廊重建）。

锁定：逐行内缘 → 分段直线；直墙/斜墙（车旋转）的中心与斜率、单侧空气墙、
过宽裁剪、窄走廊不调整、绿区包围块（车）不参与拟合。
"""
from __future__ import annotations

import numpy as np
import pytest

from maaracing_master.plugins.speedrush.depth_geo import (
    GRID_BLOCKED, GRID_DRIVABLE, GRID_NX, GRID_NZ, GRID_UNKNOWN,
    DrivableGrid, wall_line_reading)

LANE_W = 3.38


def _grid(state: np.ndarray) -> DrivableGrid:
    return DrivableGrid(state=state, coef=(0.0, 0.0, 2.0),
                        counts=np.full(state.shape, 9, np.int32),
                        dig_cells=0, latency_ms=1.0)


def _iz(z_head: float) -> int:
    """车头系 z → 栅格行（相机 z = 车头 z + 5，栅格从 3 起）。"""
    return int((z_head + 5.0 - 3.0) / 0.25)


def _ix(x: float) -> int:
    return int((x + 14.0) / 0.25)


def _strip_road(st: np.ndarray, lx_of, rx_of, z0=0.0, z1=16.0) -> None:
    """在两侧墙之间铺绿区、留外侧未知（墙不会被四向绿区包围）。"""
    for z in np.arange(z0, z1 + 1e-9, 0.25):
        iz = _iz(float(z))
        if not (0 <= iz < GRID_NZ):
            continue
        lo, hi = _ix(lx_of(float(z))), _ix(rx_of(float(z)))
        st[iz, lo + 1:hi] = GRID_DRIVABLE


def _walls(st: np.ndarray, lx_of, rx_of, z0=0.0, z1=16.0) -> None:
    for z in np.arange(z0, z1 + 1e-9, 0.25):
        iz = _iz(float(z))
        if not (0 <= iz < GRID_NZ):
            continue
        st[iz, _ix(lx_of(float(z)))] = GRID_BLOCKED
        st[iz, _ix(rx_of(float(z)))] = GRID_BLOCKED


def _scene(lx_of, rx_of, *, left=True, right=True,
           z0=0.0, z1=16.0) -> np.ndarray:
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    _strip_road(st, lx_of, rx_of, z0, z1)
    _walls(st, lx_of, rx_of, z0, z1) if left and right else None
    if left and not right:
        for z in np.arange(z0, z1 + 1e-9, 0.25):
            st[_iz(float(z)), _ix(lx_of(float(z)))] = GRID_BLOCKED
    if right and not left:
        for z in np.arange(z0, z1 + 1e-9, 0.25):
            st[_iz(float(z)), _ix(rx_of(float(z)))] = GRID_BLOCKED
    return st


def test_straight_walls_center_and_slope():
    wlc = wall_line_reading(_grid(_scene(lambda z: -5.0, lambda z: 5.0)),
                            LANE_W)
    assert wlc is not None
    assert wlc.health == "ok"
    assert wlc.center_lane == pytest.approx(0.0, abs=0.05)
    assert wlc.width_lane == pytest.approx(10.0 / LANE_W, abs=0.05)
    assert wlc.slope == pytest.approx(0.0, abs=0.02)
    assert wlc.rows_l > 8 and wlc.rows_r > 8


def test_rotated_walls_recover_direction():
    """道路整体右偏 0.2（约 11°）：中心随 z 线性移动，斜率还原。"""
    wlc = wall_line_reading(
        _grid(_scene(lambda z: -5.0 + 0.2 * z, lambda z: 5.0 + 0.2 * z)),
        LANE_W)
    assert wlc is not None
    assert wlc.slope == pytest.approx(0.2, abs=0.02)
    c0, w0, _clip, h0 = wlc.frame_at(0.0, LANE_W,
                                     width_prior_lane=4.0)
    c10, w10, _clip10, h10 = wlc.frame_at(10.0, LANE_W,
                                          width_prior_lane=4.0)
    assert h0 == "ok" and h10 == "ok"
    assert c0 == pytest.approx(0.0, abs=0.15)
    assert c10 == pytest.approx(2.0, abs=0.25)
    assert w0 == pytest.approx(w10, abs=0.15)


def test_single_side_uses_air_wall():
    """只有左墙：右侧用 4 道先验补空气墙，中心=左墙+2 道。"""
    wlc = wall_line_reading(
        _grid(_scene(lambda z: -5.0, lambda z: 8.0,
                     left=True, right=False)), LANE_W)
    assert wlc is not None
    assert wlc.health == "single"
    assert wlc.width_lane == pytest.approx(4.0, abs=1e-6)
    assert wlc.center_lane == pytest.approx(
        (-5.0 + 2.0 * LANE_W) / LANE_W, abs=0.08)


def test_overwide_clipped_to_four_lanes():
    wlc = wall_line_reading(_grid(_scene(lambda z: -8.0, lambda z: 8.0)),
                            LANE_W)
    assert wlc is not None
    assert wlc.clipped is True
    assert wlc.width_lane == pytest.approx(4.0, abs=1e-6)
    assert wlc.center_lane == pytest.approx(0.0, abs=0.05)


def test_narrow_not_adjusted():
    wlc = wall_line_reading(_grid(_scene(lambda z: -2.0, lambda z: 2.0)),
                            LANE_W)
    assert wlc is not None
    assert wlc.clipped is False
    assert wlc.width_lane == pytest.approx(4.0 / LANE_W, abs=0.05)
    assert wlc.health == "ok"


def test_enclosed_car_block_not_a_wall():
    """路中间被绿区包围的红块（车）先剔除，不参与墙线拟合。"""
    st = _scene(lambda z: -5.0, lambda z: 5.0)
    st[_iz(6.0):_iz(9.0), _ix(-1.0):_ix(1.0)] = GRID_BLOCKED
    wlc = wall_line_reading(_grid(st), LANE_W)
    assert wlc is not None
    assert wlc.center_lane == pytest.approx(0.0, abs=0.05)
    assert wlc.width_lane == pytest.approx(10.0 / LANE_W, abs=0.05)


def test_no_walls_returns_none():
    st = np.full((GRID_NZ, GRID_NX), GRID_UNKNOWN, np.int8)
    assert wall_line_reading(_grid(st), LANE_W) is None


def test_gap_splits_segments_but_keeps_wall():
    """墙中间缺 2m：分段断开，两侧段仍可用于中心线。"""
    st = _scene(lambda z: -5.0, lambda z: 5.0)
    st[_iz(7.0):_iz(9.0), _ix(-5.0)] = GRID_UNKNOWN
    st[_iz(7.0):_iz(9.0), _ix(5.0)] = GRID_UNKNOWN
    wlc = wall_line_reading(_grid(st), LANE_W)
    assert wlc is not None
    assert wlc.center_lane == pytest.approx(0.0, abs=0.08)
    assert len(wlc.left_segs) >= 1


def test_wall_line_master_builds_corridor():
    """master 开：墙线读数直接变走廊（src=wall_line），绿区降为兜底。"""
    from maaracing_master.plugins.speedrush.config import GridXcheck
    from maaracing_master.plugins.speedrush.module import _EgoRoadObserver

    st = _scene(lambda z: -5.0, lambda z: 5.0)
    wlc = wall_line_reading(_grid(st), LANE_W)
    obs = _EgoRoadObserver(GridXcheck(mode="master"), LANE_W,
                           wall_line_master=True)
    cor = obs.update(None, grid=_grid(st), fid=1, wall_line=wlc)
    assert cor is not None
    assert cor.src == "wall_line"
    assert cor.center_lane == pytest.approx(0.0, abs=0.05)
    assert cor.width_lane == pytest.approx(10.0 / LANE_W, abs=0.05)
    assert len(cor.bands) == 9


def test_wall_line_master_single_side_air_wall():
    from maaracing_master.plugins.speedrush.config import GridXcheck
    from maaracing_master.plugins.speedrush.module import _EgoRoadObserver

    st = _scene(lambda z: -5.0, lambda z: 8.0, left=True, right=False)
    wlc = wall_line_reading(_grid(st), LANE_W)
    obs = _EgoRoadObserver(GridXcheck(mode="master"), LANE_W,
                           wall_line_master=True)
    cor = obs.update(None, grid=_grid(st), fid=1, wall_line=wlc)
    assert cor is not None
    assert cor.src == "wall_line"
    assert cor.width_lane == pytest.approx(4.0, abs=1e-6)
    assert cor.bands[0].left.src == "inferred"
