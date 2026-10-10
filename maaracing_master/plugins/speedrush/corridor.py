# -*- coding: utf-8 -*-
"""分带走廊（walls-only 第 6 步，2026-10-09）——感知层→控制层的统一供数表示。

**它是什么**：把墙实体（WallTrack 逐箱假设）、绿区范围（可行驶栅格的实测
路面）与空气墙先验（中线±W/2）组织成 3m 一带的走廊。每一带、每一侧都能
回答评审验收面的三个问题：**边界来自哪里**（measured/occluded/inferred/
unknown 四态）、**证据有多新**（last_fid + 置信）、**当前允许做什么**（由
消费端按来源规则判，见 decision._side_has_space）。

**它不是什么**：不做分类决策。来源只是可信度的一部分，不是最终安全结论
（评审追加：measured ≠ 激进许可——实测墙也可能因拟合误差/浅结构抖动不适
合立即作为激进控制依据）；走廊也不替代栅格与找边的内部逻辑，只组织它们
的产出。空气墙永远 inferred（宪法：路面边界=中线±路宽/2，来自先验不来自
检测，不许冒充实测）；墙永远赢（撞界≤规划域，冲突时墙收窄走廊——带内
wall bins 优先于绿区范围，消费端同一规则）。

来源优先级（带内逐侧）：墙箱（WallTrack 攒的实体，measured/occluded）>
绿区范围（栅格实测路面，measured 低置信）> 空气墙（先验，inferred）>
unknown（无证据，诚实降级）。"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass

from maaracing_master.plugins.speedrush.depth_geo import (
    GRID_BLOCKED, GRID_CELL, GRID_DRIVABLE, GRID_UNKNOWN, GRID_X_MAX,
    GRID_Z_HI, GRID_Z_LO)
from maaracing_master.plugins.speedrush.wall_track import MEASURED, OCCLUDED

# 边界来源四态（wall_track 的 measured/occluded 同名复用，不建第二真源）
INFERRED, UNKNOWN = "inferred", "unknown"

BAND_LEN_M = 3.0
# 末带 15~16m 不足 3m 照收（1m 带，诚实标注 z1）
N_BANDS = int(-(-(GRID_Z_HI - GRID_Z_LO) // BAND_LEN_M))    # ceil → 5
# 绿区边界的 observed 行下限（3/12 行=0.75m）：单行噪声邻格不冒充实测边界
# （d00036 逐行实测：真边界带外邻 blocked 10~12 行、假墙带（视锥截断）0 行，
# 2026-10-10 定案）。
GREEN_OBS_ROWS_MIN = 3


@dataclass(frozen=True)
class BandBoundary:
    """带内一侧的边界假设。x_m 为 None 且 src=unknown 表示无证据——消费端
    对 unknown 保持对称默认（不否决也不许走），不臆造边界。"""

    x_m: float | None        # 边界位置（米，自车系，左负右正）
    src: str                 # measured / occluded / inferred / unknown
    conf: float = 0.0        # 证据置信（wall_track 攒的；绿区=1.0；inferred=0）
    last_fid: int | None = None   # 证据多新（wall_track 槽位最近在场 fid）


@dataclass(frozen=True)
class CorridorBand:
    """一个 3m 带：左右边界 + 带内有效点数（unknown 的判据面——点数不足
    的带如实标注，不静默补成安全道路）。"""

    z0: float                # 带起点（米，车前方）
    z1: float
    left: BandBoundary
    right: BandBoundary
    n_pts: int = 0


@dataclass(frozen=True)
class Corridor:
    """分带走廊：感知层→控制层的统一供数。

    center_lane（道，右正，原点=车）由供数门的优先级链算好传入，极性契约
    与旧 off 同式：off = −center_lane（planner 消费 −值，右正）。"""
    bands: tuple[CorridorBand, ...]   # 近→远
    center_lane: float | None
    width_lane: float | None   # 走廊宽（道，边界到边界）
    src: str                   # pair / wall / wall_occl / pair_slot / region / none
    plane_health: str          # ok / degraded / none
    far_trend: float | None    # 远场墙走向（米/米，只预判不硬否决，trace 消费）


def _band_bounds(i: int) -> tuple[float, float]:
    z0 = GRID_Z_LO + i * BAND_LEN_M
    return z0, min(GRID_Z_HI, z0 + BAND_LEN_M)


def _band_rows_mask(grid, z0: float, z1: float) -> np.ndarray:
    """带内栅格行（zc∈[z0,z1)）。"""
    iz = np.arange(grid.state.shape[0])
    zc = GRID_Z_LO + (iz + 0.5) * GRID_CELL
    return (zc >= z0) & (zc < z1)


def _wall_boundary(wall_track, side: int, z0: float, z1: float) -> BandBoundary | None:
    """带内该侧的墙箱边界（墙实体赢：有攒出置信的墙箱就用它）。同带多箱取
    置信最高者；measured 与 occluded 同优先（状态就是来源，消费端区分）。"""
    if wall_track is None:
        return None
    best = None
    for zc, pos, conf, state, _last_fid in wall_track.bins(side):
        if not state or not (z0 <= zc < z1):
            continue
        if best is None or conf > best[1]:
            best = (pos, conf, state, _last_fid)
    if best is None:
        return None
    pos, conf, state, last_fid = best
    return BandBoundary(float(pos), state, float(conf), int(last_fid))


def _green_boundary(grid, side: int, z0: float, z1: float) -> BandBoundary | None:
    """带内该侧的绿区范围边界（栅格实测路面）。

    边界语义二分（2026-10-10 视锥截断定案）：绿区到头有两种——外邻格
    **blocked**（观测到的高结构挡住=实测边界）与外邻格 **unknown/窗缘**
    （数据到头：视锥截断/点饥饿，不是墙）。只有前者算 measured，带内
    observed 行 < GREEN_OBS_ROWS_MIN 回退空气墙（返回 None，宁缺勿假——
    近带绿左逐行贴视锥边界 0.1~0.3m，d00036 实测）。中位只在 observed
    行上取；无绿行的行不进中位（旧码把 n_ix/−1 混进中位）。"""
    if grid is None:
        return None
    rows = _band_rows_mask(grid, z0, z1)
    st = grid.state[rows]
    drivable = st == GRID_DRIVABLE
    if not drivable.any():
        return None
    n_ix = st.shape[1]
    ix = np.arange(n_ix)
    if side < 0:
        edge = np.where(drivable, ix, n_ix).min(axis=1)    # 逐行绿区左边界
        nbr = edge - 1                                     # 外侧邻格
    else:
        edge = np.where(drivable, ix, -1).max(axis=1)      # 逐行绿区右边界
        nbr = edge + 1
    has = (edge >= 0) & (edge < n_ix)                      # 有绿行
    inb = (nbr >= 0) & (nbr < n_ix)                        # 邻格在窗内（出窗=数据到头）
    nb = np.full(edge.shape, GRID_UNKNOWN, np.int8)        # 默认值不出 observed
    ri = np.arange(edge.shape[0])
    nb[inb] = st[ri[inb], nbr[inb]]
    observed = has & inb & (nb == GRID_BLOCKED)
    if int(observed.sum()) < GREEN_OBS_ROWS_MIN:
        return None
    x_m = -GRID_X_MAX + (float(np.median(edge[observed])) + 0.5) * GRID_CELL
    return BandBoundary(x_m, MEASURED, 1.0, None)


def _air_boundary(side: int, center_lane: float | None,
                  width_lane: float | None, lane_w_m: float | None) -> BandBoundary | None:
    """空气墙（先验，永远 inferred）：center±W/2。无中心/无宽=None。"""
    if center_lane is None or width_lane is None or not (lane_w_m and lane_w_m > 0):
        return None
    edge = center_lane - width_lane / 2.0 if side < 0 else center_lane + width_lane / 2.0
    return BandBoundary(edge * lane_w_m, INFERRED, 0.0, None)


def _far_trend(wall_track) -> float | None:
    """远场墙走向：同侧 ≥2 个不同 z 的 measured 墙箱的线性斜率（米/米）。
    只预判不硬否决（trace 消费）；occluded 箱不进拟合（历史位置不参与走向）。"""
    if wall_track is None:
        return None
    best = None
    for side in (-1, 1):
        pts = [(zc, pos) for zc, pos, _c, s, _f in wall_track.bins(side)
               if s == MEASURED]
        if len(pts) < 2 or len({z for z, _ in pts}) < 2:
            continue
        zs = np.array([z for z, _ in pts])
        xs = np.array([x for _, x in pts])
        slope = float(np.polyfit(zs, xs, 1)[0])
        if best is None or len(pts) > best[1]:
            best = (slope, len(pts))
    return None if best is None else best[0]


def _plane_health(reading) -> str:
    """平面健康面：reading 缺席=none；拟合失败或质量门拒绝=degraded。"""
    if reading is None:
        return "none"
    if getattr(reading, "coef", None) is None:
        return "degraded"
    if any("质量门" in r for r in (reading.rejects or ())):
        return "degraded"
    return "ok"


def build_corridor(reading, grid, wall_track, fid: int | None, *,
                   center_lane: float | None, width_lane: float | None,
                   src: str, lane_w_m: float | None) -> Corridor:
    """分带走廊拼装：center/width/src 由供数门（_EgoRoadObserver 优先级链）
    算好传入，本函数只管分带表示、来源标注与 far_trend/plane_health。"""
    bands = []
    for i in range(N_BANDS):
        z0, z1 = _band_bounds(i)
        sides = {}
        for side in (-1, 1):
            b = (_wall_boundary(wall_track, side, z0, z1)
                 or _green_boundary(grid, side, z0, z1)
                 or _air_boundary(side, center_lane, width_lane, lane_w_m)
                 or BandBoundary(None, UNKNOWN))
            sides[side] = b
        n_pts = 0
        if grid is not None:
            rows = _band_rows_mask(grid, z0, z1)
            n_pts = int(grid.counts[rows].sum())
        bands.append(CorridorBand(z0, z1, sides[-1], sides[1], n_pts))
    return Corridor(tuple(bands), center_lane, width_lane, src,
                    _plane_health(reading), _far_trend(wall_track))
