# -*- coding: utf-8 -*-
"""分带走廊（corridor.py + _EgoRoadObserver 第 6 步）的回归锁。

合成用例锁语义：带内来源优先级（墙箱>绿区>空气墙>unknown）、墙实体路径
（单侧/遮挡从全盲变半盲供数）、区兜底只在新鲜拍、拒绝不兜底（宁弃不喂）、
plane_health/far_trend 标注。数据面锁 _side_has_space 的带来源规则
（tests/test_speedrush_decision.py）。"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from maaracing_master.plugins.speedrush import module as smod
from maaracing_master.plugins.speedrush.corridor import (
    INFERRED, UNKNOWN, Corridor, build_corridor)
from maaracing_master.plugins.speedrush.depth_geo import DepthRoadReading
from maaracing_master.plugins.speedrush.wall_track import OCCLUDED, WallTrack
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()
LW = CAL.lane_w_m


def _bnd(el=None, er=None, edge_pts=()):
    return DepthRoadReading(left_edge_lane=el, right_edge_lane=er,
                            left_x=None, right_x=None,
                            sides=int(el is not None) + int(er is not None),
                            latency_ms=1.0, rejects=(), edge_pts=tuple(edge_pts),
                            coef=(0.0, 0.0, 0.5))


def _grid(rows, col_lo=36, col_hi=59):
    """绿区 DrivableGrid：行号直接给（zc=3+(iz+0.5)·0.25），列 36~60 =
    X∈[−1.5,+4.5]m。"""
    from maaracing_master.plugins.speedrush import depth_geo as dg
    st = np.full((dg.GRID_NZ, dg.GRID_NX), dg.GRID_UNKNOWN, np.int8)
    for iz in rows:
        st[iz, col_lo:col_hi + 1] = dg.GRID_DRIVABLE
    return dg.DrivableGrid(state=st, coef=(0.0, 0.0, 0.72),
                           counts=np.full(st.shape, 9, np.int32),
                           dig_cells=0, latency_ms=0.0)


def _off(c):
    return None if c is None else -c.center_lane


# ---------- 带内来源优先级：墙箱 > 绿区 > 空气墙 > unknown ----------

def test_band_priority_wall_beats_green_beats_air():
    """同一带墙箱/绿区同场：墙赢（物理边界）；无墙带绿区接棒（measured 低
    置信）；两路都缺席带空气墙（inferred，center±W/2）。"""
    wt = WallTrack(CAL)
    wt.update(SimpleNamespace(edge_pts=[(-1, 4.0, -5.5)]), fid=10)
    grid = _grid(range(12, 24))              # 绿区 z 6~9m（带 6-9）
    c = build_corridor(_bnd(), grid, wt, fid=11,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    assert c.bands[0].left.src == "measured"          # 墙箱赢
    assert c.bands[0].left.x_m == pytest.approx(-5.5)
    assert c.bands[0].left.conf == 1.0
    assert c.bands[0].left.last_fid == 10
    assert c.bands[0].right.src == INFERRED           # 无右墙无绿区→空气墙
    assert c.bands[0].right.x_m == pytest.approx(+2.0 * LW)
    assert c.bands[1].left.src == "measured"          # 绿区接棒（带 6-9）
    assert c.bands[1].left.conf == 1.0
    assert c.plane_health == "ok"


def test_band_unknown_when_no_center():
    """无中心先验（W 未成形）且无墙无绿区 → unknown（不臆造边界）。"""
    c = build_corridor(_bnd(), None, None, fid=1,
                       center_lane=None, width_lane=None, src="none",
                       lane_w_m=LW)
    for b in c.bands:
        assert b.left.src == UNKNOWN and b.right.src == UNKNOWN
        assert b.left.x_m is None


def test_plane_health_degraded():
    r_bad = DepthRoadReading(left_edge_lane=None, right_edge_lane=None,
                             left_x=None, right_x=None, sides=0,
                             latency_ms=1.0, rejects=("平面质量门拒绝",),
                             coef=None)
    c = build_corridor(r_bad, None, None, fid=1, center_lane=None,
                       width_lane=None, src="none", lane_w_m=LW)
    assert c.plane_health == "degraded"
    c2 = build_corridor(None, None, None, fid=1, center_lane=None,
                        width_lane=None, src="none", lane_w_m=LW)
    assert c2.plane_health == "none"


# ---------- 墙实体路径：单侧/遮挡从全盲变半盲供数 ----------

def _fed_wall_observer(n_pairs=32):
    """W 成形 + 双侧墙箱攒足置信的观察器（pair: ±2.0 道，墙箱 ±5.5m）。"""
    o = smod._EgoRoadObserver(lane_w_m=LW, wall_track=WallTrack(CAL))
    for fid in range(1, n_pairs + 1):
        o.update(_bnd(-2.0, 2.0, edge_pts=[(-1, 4.0, -5.5), (1, 4.0, 5.5)]),
                 now=1000.0 + 0.05 * fid, fid=fid)
    return o


def test_wall_path_single_side_supplies():
    """单侧拍：右墙箱在场（conf 满级）→ 墙实体路径 src=wall，中心=墙∓W/2
    （469 拍 sides=1 型从全盲变半盲供数）。"""
    o = _fed_wall_observer()
    c = o.update(_bnd(None, 2.0, edge_pts=[(1, 4.0, 5.5)]),
                 now=1100.0, fid=33)
    assert c is not None and c.src == "wall"
    # 单侧拍只喂右缘：右墙箱是 measured（左箱转 occluded），MEASURED 优先
    # 选右墙 → 中心 = 墙(+5.5/LW 道) − W/2（W=4.0 道）
    assert _off(c) == pytest.approx(2.0 - 5.5 / LW)
    assert c.width_lane == pytest.approx(4.0)


def test_wall_occluded_path_supplies_with_honest_source():
    """遮挡拍（无任何边观测）：攒足置信的墙箱标遮挡 → 墙实体路径续上，
    src=wall_occl（单帧遮挡杀不死墙，但来源诚实）。"""
    o = _fed_wall_observer()
    c = o.update(_bnd(), now=1100.05, fid=34)   # 双墙都被车挡
    assert c is not None and c.src == "wall_occl"
    # 双墙同 z 箱：取更小 (zc, side) → 左墙 → 中心 = 墙(−5.5/LW) + W/2
    assert _off(c) == pytest.approx(5.5 / LW - 2.0)
    assert c.bands[0].left.src == OCCLUDED
    assert c.bands[0].left.last_fid == 32       # 证据年龄可读


def test_wall_path_dead_after_decay():
    """墙箱置信衰减到底（~2s 无观测）→ 墙路径失效，弃权不臆造。"""
    o = _fed_wall_observer()
    c = None
    for fid in range(35, 200):
        c = o.update(_bnd(), now=1100.1 + 0.05 * (fid - 35), fid=fid)
    assert c is None                            # 衰减到底，诚实黑视


# ---------- 拒绝不兜底 / 区兜底只在新鲜拍 ----------

def test_rejected_pair_stays_none_even_with_region():
    """垃圾对被拒（|off|>OFF_MAX）→ None，即使区读数在场也不兜底
    （宁弃不喂假路心；拒绝与缺席分两条路）。"""
    o = smod._EgoRoadObserver(lane_w_m=LW)
    assert o.update(_bnd(-6.0, -2.0), now=1000.0, grid=_grid(range(12, 24)),
                    fid=1) is None


def test_region_fallback_on_fresh_tick_only():
    """候选缺席的新鲜拍：墙/槽全空 → 区读数接棒（src=region）；复用拍
    （new=False）区不接棒（旧栅格不冒充新鲜）。"""
    o = smod._EgoRoadObserver(lane_w_m=LW)
    g = _grid(range(12, 24))                    # 绿区路心 +1.5m
    c = o.update(None, now=1000.0, grid=g, fid=1)
    assert c is not None and c.src == "region"
    assert _off(c) == pytest.approx(-1.5 / LW, abs=0.01)
    c2 = o.update(None, now=1000.05, grid=g, fid=2, new=False)
    assert c2 is None                           # 复用拍黑视（旧栅格不冒充）


def test_master_mode_region_first():
    """master 模式：区读数当主人（区在场优先），pair 降为审计候选。"""
    xc = SimpleNamespace(mode="master", tol_lane=0.45, bypass_streak=8)
    o = smod._EgoRoadObserver(xc, LW)
    g = _grid(range(12, 24))                    # 区路心 +1.5m → off≈−0.545
    c = o.update(_bnd(-2.0, 2.0), now=1000.0, grid=g, fid=1)
    assert c is not None and c.src == "region"
    assert _off(c) == pytest.approx(-1.5 / LW, abs=0.01)
