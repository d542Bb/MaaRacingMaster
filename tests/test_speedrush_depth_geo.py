# -*- coding: utf-8 -*-
"""深度几何观测层（depth_geo）的回归锁。

两层用例：
- **合成**（零数据依赖）：锁读法机制——全局 q20 把少数派路面钉成地面
  （中位数会被人行道捕获，518 帧实测三群体值）、直路双边界的读数与侧别、
  双守卫的逐个拒绝行为（收敛/侧别/远带）。合成深度图按透视口径构造：
  边界线过 (vpx, y_h)，路面占统计列 ≥20%（q20 适用条件，见 _ground_q20 注）。
- **数据锚点**（skipif：无离线缓存则跳）：锁定骑路缘 518（L 在场 + R 出租车
  伪块被拒）与贴护栏 437（R 在场 + L 翻面块被拒）的守卫裁决——标定锚点
  （conv≤350 / resid≤45 / 侧别 0.15 / 近带行≥2）不许漂。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from maaracing_master.plugins.speedrush.depth_geo import (
    NEAR_HI, NEAR_LO, DepthRoadObserver, _ground_q20, reading_from_map)
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()
ROAD0, ROAD_SLOPE = 4.0, 1.0 / 375.0     # 路面视差 c(y)：y340→4.0、y714→5.0
RAISE = 1.12                             # 边界外抬升面（+12% > 门 2%）

NPY = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
       / "speedrush" / "depth_review" / "npy")


def _road(y: int) -> float:
    return ROAD0 + (y - 340) * ROAD_SLOPE


def _base_map() -> np.ndarray:
    """全路面楔形（近带占统计列 ≥20%，q20 可钉住）。"""
    ys = np.arange(720)
    return (ROAD0 + (ys - 340).clip(0)[:, None] * ROAD_SLOPE).astype(np.float32) \
        * np.ones((1, 1280), np.float32)


def _draw_raised(m: np.ndarray, inner, side: str, y0: int, y1: int) -> None:
    """画抬升块：内沿 x=inner(y)，L 侧填 [0,x)、R 侧填 [x,1280)，值=c(y)·RAISE。"""
    for y in range(y0, y1):
        xi = int(round(inner(y)))
        if side == "L":
            m[y, :max(xi, 0)] = _road(y) * RAISE
        else:
            m[y, min(xi, 1280):] = _road(y) * RAISE


def _vp_line(k: float, side: str):
    """过 VP 的边界线：x = vpx ± k·(y−y_h)。"""
    sgn = -1.0 if side == "L" else 1.0
    return lambda y: CAL.vpx + sgn * k * (y - CAL.y_h)


# ── 读法机制 ────────────────────────────────────────────────────────────

def test_ground_q20_pins_minority_road_median_fails():
    """行内路面是少数派时：q20 钉住路面，中位数被人行道捕获（518 实测值）。"""
    m = np.full((720, 1280), 5.316, np.float32)
    m[:, :300] = 5.551                       # 人行道（统计列内 301）
    m[:, 301:540] = 5.316                    # 路面（统计列内 239，22%）
    m[:, 740:1000] = 5.551                   # 人行道（261）
    m[:, 1000:] = 6.148                      # 出租车（280）
    g = _ground_q20(m)
    assert abs(float(g[600 - 340]) - 5.316) < 1e-3      # q20 = 路面
    med = np.median(m[600, np.r_[0:540, 740:1280]])
    assert abs(float(med) - 5.551) < 1e-3               # 中位数 = 人行道（失效）


def test_straight_road_both_edges_detected():
    """直路双边界：双侧在场、内沿位置与车道单位落在真线附近。"""
    m = _base_map()
    _draw_raised(m, _vp_line(0.95, "L"), "L", 340, 715)
    _draw_raised(m, _vp_line(0.95, "R"), "R", 340, 715)
    rd = reading_from_map(m, CAL, None)
    assert rd.sides == 2
    # 近带中位行 y≈625 的真内沿
    x_l = CAL.vpx - 0.95 * (625 - CAL.y_h)
    x_r = CAL.vpx + 0.95 * (625 - CAL.y_h)
    assert abs(rd.left_x - x_l) <= 8
    assert abs(rd.right_x - x_r) <= 8
    assert rd.left_edge_lane <= -0.15
    assert rd.right_edge_lane >= 0.15


# ── 双守卫的拒绝行为 ────────────────────────────────────────────────────

def test_conv_guard_rejects_nonconverging_block():
    """竖直内沿不过 VP（外推偏移 >350px）→ R 弃权，原因含 conv。"""
    m = _base_map()
    _draw_raised(m, lambda y: 1000.0, "R", 550, 715)
    rd = reading_from_map(m, CAL, None)
    assert rd.right_edge_lane is None
    assert any("conv" in r for r in rd.rejects)


def test_side_guard_rejects_crossing_block():
    """横贯块的内沿落在画面中央（lane≈0）→ L 侧别拒；右缘不触边无 R 候选。"""
    m = _base_map()
    for y in range(550, 715):
        m[y, :1000] = _road(y) * RAISE       # 触左缘、内沿 x=999 ≈ 中央
    rd = reading_from_map(m, CAL, None)
    assert rd.left_edge_lane is None
    assert any("lane" in r for r in rd.rejects)
    assert rd.right_edge_lane is None


def test_far_band_block_rejected():
    """块主体在远带（近带无内沿行）→ 弃权，原因含 远带。"""
    m = _base_map()
    _draw_raised(m, lambda y: 1200.0, "R", 340, 478)
    rd = reading_from_map(m, CAL, None)
    assert rd.right_edge_lane is None
    assert any("远带" in r for r in rd.rejects)


def test_baseline_guard_rejects_soft_gate():
    """内沿内侧 40~120px 本底窗被垫到 1.5%（>门/2、<门）→ 门失效弃权。

    复刻第一关终选的跨档失效形态（块从路面里起跳、边界=松门交点）：
    守卫让失去物理语义的门主动交出决策权，而不是继续伪装成功。"""
    m = _base_map()
    inner = _vp_line(0.95, "R")
    _draw_raised(m, inner, "R", 340, 715)
    for y in range(NEAR_LO, NEAR_HI + 1):
        x0 = int(round(inner(y)))
        m[y, x0 - 120:x0 - 40] = _road(y) * 1.015     # 松门本底（<2% 不自成块）
    rd = reading_from_map(m, CAL, None)
    assert rd.right_edge_lane is None
    assert any("门本底" in r for r in rd.rejects)


# ── 资产与会话降级 ──────────────────────────────────────────────────────

def test_ego_mask_asset_loads():
    """挖除区 = 上半身矩形（y351~532）∪ 车带下延（同列带到底，皮肤无关段）。"""
    mask = DepthRoadObserver._load_ego_mask()
    assert mask is not None and mask.shape == (720, 1280)
    assert mask[351:532, 512:765].all()          # 上半身高区
    assert mask[532:715, 512:765].all()          # 车带下延（粘连带整体排除）
    assert not mask[:351, 512:766].any()         # 上缘之上不挖
    assert not mask[:, :512].any() and not mask[:, 766:].any()


def test_carband_breaks_tail_bridge():
    """车尾粘连（内沿在 y540~565 段冲进中央列带）被车带下延挖除救回。

    不挖除时粘连段污染内沿拟合（残差爆、整块被拒）；挖除后内沿回归
    真边界线——按"真边界不进中央带"的结构事实泛化，无需精细车形。"""
    inner = _vp_line(0.95, "L")
    m = _base_map()
    _draw_raised(m, inner, "L", 340, 715)
    for y in range(540, 566):                    # 车尾粘连：内沿冲到中央带
        m[y, :700] = _road(y) * RAISE
    m[533:715, 512:765] = _road(600) * RAISE * 2  # 车身高读数（挖除区形状）
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask())
    assert rd.left_edge_lane is not None and rd.left_edge_lane <= -0.15
    rd_noguard = reading_from_map(m, CAL, None)
    assert rd_noguard.left_edge_lane is None      # 不挖除：粘连桥污染被守卫拒


def test_observer_without_session_returns_none():
    """权重缺失（session=None）时 observe 恒 None——road_offset 退纯模型积分。"""
    obs = DepthRoadObserver(None, CAL)
    assert obs.observe(np.zeros((720, 1280, 3), np.uint8)) is None


# ── 数据锚点（守卫标定不许漂；无离线缓存则跳）──────────────────────────

def _cached_map(key: str) -> np.ndarray | None:
    p = NPY / f"{key}__da2s.npy"
    if not p.exists():
        return None
    return np.load(p).astype(np.float32)


@pytest.mark.skipif(_cached_map("frames__000518") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_518_straddle():
    """骑路缘 518：L=人行道右缘在场，R 出租车伪块被双守卫拒。"""
    m = _cached_map("frames__000518")
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask())
    assert rd.left_edge_lane is not None and rd.left_edge_lane < -0.15
    assert rd.right_edge_lane is None
    assert any(r.startswith("R:") for r in rd.rejects)


@pytest.mark.skipif(_cached_map("frames__000437") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_437_wallhug():
    """贴护栏 437：R=护栏基部在场，L 翻面块（conv 559）被拒——侧别由深度独立裁决。"""
    m = _cached_map("frames__000437")
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask())
    assert rd.right_edge_lane is not None and rd.right_edge_lane > 0.15
    assert rd.left_edge_lane is None
    assert any(r.startswith("L:") for r in rd.rejects)
