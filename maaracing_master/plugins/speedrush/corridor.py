# -*- coding: utf-8 -*-
"""分带走廊（2026-10-21 走廊收敛定案：绿区底 + 找边远箱，墙融合退役）——
感知层→控制层的统一供数表示。

**它是什么**：把绿区范围（可行驶栅格的实测路面）与找边远箱读数（逐箱找边
扫描的 16~30m 检出）与空气墙先验（中线±W/2）组织成 3m 一带的走廊。每一带、
每一侧都能回答评审验收面的三个问题：**边界来自哪里**（measured/inferred/
unknown 三态）、**证据有多新**（last_fid + 置信）、**当前允许做什么**（由
消费端按来源规则判；2026-10-18 起选择层直接使用带边界做目标夹取）。

**它不是什么**：不做分类决策。来源只是可信度的一部分，不是最终安全结论
（measured ≠ 激进许可——实测边界也可能因拟合误差抖动不适合立即作为激进
控制依据）；走廊也不替代栅格与找边的内部逻辑，只组织它们的产出。空气墙
永远 inferred（宪法：路面边界=中线±路宽/2，来自先验不来自检测，不许冒充
实测）。

来源规则（带内逐侧，2026-10-21 影子对比定案，证据指针见当次提交）：
- **近场带**（相机系 z < FAR_Z_CAM）：绿区范围（栅格实测路面）为底；绿区
  缺席落空气墙（先验，inferred），再无证据才 unknown。
- **远场带**（相机系 z ≥ FAR_Z_CAM）：找边远箱读数为底（带内检出=measured、
  取最靠路心者；带内无检出向已测远箱借最近者=inferred、FAR_CARRY_M 内）；
  无借界落空气墙。远箱与车道读数分离的金标契约不变（找边读数只进远场带
  供方向，不进近场车道约束）。
- **墙融合退役（带内）**：影子对比（143 帧）实证「绿区底 + 找边远箱」在翻面率、
  抖动、远区覆盖上同时不劣于墙融合——WallTrack 从**带内**供数面退役；它仍
  是走廊路心绝对链的物理锚点（双侧同箱中点 / 单侧墙±W/2，见
  module._wall_center）。

中远场粗带（coarse_bands，2026-10-21 方案定案）：相机系 16~80m、15m/带，由
地面密度读法供「地面可见范围」（depth_geo.ground_far_extents，推断级）——
**只进 coarse_bands 不进 bands**：车道级消费面（粗层净空/选择夹取）只读
bands，远带是方向级约束（哪边更开、范围在哪收窄），不自动灌进车道约束，
消费端分层接入（见 module._EgoRoadObserver 与方案文档）。"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass

from maaracing_master.plugins.speedrush.depth_geo import (
    CAM_Z_DATUM_M, GRID_BLOCKED, GRID_CELL, GRID_DRIVABLE, GRID_UNKNOWN,
    GRID_X_MAX, GRID_Z_HI, GRID_Z_LO)
from maaracing_master.plugins.speedrush.wall_track import MEASURED

# 边界来源（MEASURED 与后备链墙实体同名复用，不建第二真源；墙态 occluded
# 随墙融合退出走廊供数面）
INFERRED, UNKNOWN = "inferred", "unknown"

BAND_LEN_M = 3.0
# 末带不足 3m 照收（诚实标注 z1）
N_BANDS = int(-(-(GRID_Z_HI - GRID_Z_LO) // BAND_LEN_M))    # ceil → 9
# 绿区边界的 observed 行下限（3/12 行=0.75m）：单行噪声邻格不冒充实测边界
# （d00036 逐行实测：真边界带外邻 blocked 10~12 行、假墙带（视锥截断）0 行，
# 2026-10-10 定案）。
GREEN_OBS_ROWS_MIN = 3
# 远场带下界（相机系）：逐箱找边的 16~30m 远箱从这里起进走廊供方向（金标
# 契约：远箱不进近场车道约束）。NEAR_Z=16 的带 15-18 从此界起算。
FAR_Z_CAM = 15.0
# 远场带内无检出时，向已测远箱借界的最大纵深（米；借界=inferred 方向级，
# 影子对比定案的覆盖来源）
FAR_CARRY_M = 6.0
# 交接一致性门容差（道）：远粗带与近带的路心跳变超此比例 → 延续近带边界
# （路不在 16m 处瞬移；门拦「远区看到别处」的污染带，不拦正常的宽水管——
# 地面可见范围比车道边界宽是设计内，2026-10-21 方案定案）
FAR_HANDOVER_TOL_LANE = 1.5
# 近带中心守卫（2026-10-23）：保险杠近场视锥/挖洞会把绿区裁成偏心小块
# （实拍 185126_p1：带中心偏离绝对路心 4.9m、左缘跑到路心右侧 2.7m），
# 而选择帧中心与包络都吃分带边界——偏心带=规划器朝墙打舵的根因。
# 近带（z<9m）双侧中点偏离绝对路心超此值 → 该带退回空气墙（绝对路心±W/2）。
NEAR_BAND_CENTER_TOL_M = 1.0
NEAR_BAND_GUARD_Z = 9.0


@dataclass(frozen=True)
class BandBoundary:
    """带内一侧的边界假设。x_m 为 None 且 src=unknown 表示无证据——消费端
    对 unknown 保持对称默认（不否决也不许走），不臆造边界。"""

    x_m: float | None        # 边界位置（米，自车系，左负右正）
    src: str                 # measured / inferred / unknown
    conf: float = 0.0        # 证据置信（绿区=1.0；找边远箱=0.5；inferred=0）
    last_fid: int | None = None   # 证据多新（测量帧的 fid；先验空气墙为 None）


@dataclass(frozen=True)
class CorridorBand:
    """一个 3m 带：左右边界 + 带内有效点数（unknown 的判据面——点数不足
    的带如实标注，不静默补成安全道路）。"""

    z0: float                # 带起点（米，车头前，负=车头后；相机系 z 减
                             # CAM_Z_DATUM_M——带内匹配仍走相机系，见 build_corridor）
    z1: float
    left: BandBoundary
    right: BandBoundary
    n_pts: int = 0


@dataclass(frozen=True)
class Corridor:
    """分带走廊：感知层→控制层的统一供数。

    center_lane（道，右正，原点=车）由供数门的优先级链算好传入，极性契约
    与旧 off 同式：off = −center_lane（planner 消费 −值，右正）。"""
    bands: tuple[CorridorBand, ...]   # 近→远（3m 带，车道级消费面）
    center_lane: float | None
    width_lane: float | None   # 走廊宽（道，边界到边界）
    src: str                   # pair / wall / wall_occl / pair_slot / region / none
    plane_health: str          # ok / degraded / none
    coarse_bands: tuple[CorridorBand, ...] = ()   # 中远场粗带 15m/带（16~80m，
    # 地面密度水管，src=inferred；**不进 bands**——车道级消费面（粗层净空/
    # 选择夹取）只读 bands，远带不自动灌进车道约束，方向级消费显式接入）
    far_hint_lane: float | None = None   # 中远场方向（滤波后，道，右正）：
    # 最近一条两侧有证据的粗带中点相对该纵深中心线；只在**新远带证据**拍
    # 更新、其余拍共享同一结果（方向级消费的唯一真源，消费端不再各自滤波）
    far_hint_age_ms: float | None = None  # 共享方向结果的龄（ms）


def _band_bounds(i: int) -> tuple[float, float]:
    """带 i 的相机系 z 界（带内匹配用；对外带标由 build_corridor 挪车头系）。"""
    z0 = GRID_Z_LO + i * BAND_LEN_M
    return z0, min(GRID_Z_HI, z0 + BAND_LEN_M)


def _band_rows_mask(grid, z0: float, z1: float) -> np.ndarray:
    """带内栅格行（zc∈[z0,z1)）。"""
    iz = np.arange(grid.state.shape[0])
    zc = GRID_Z_LO + (iz + 0.5) * GRID_CELL
    return (zc >= z0) & (zc < z1)


def _edge_far_boundary(edge_pts, side: int, z0: float, z1: float,
                       fid: int | None) -> BandBoundary | None:
    """远场带该侧的找边读数边界（2026-10-21 收敛定案：墙融合的替代供数）。

    找边扫描（depth_geo._scan_side）按 ZBIN 逐箱独立读数、已扫到 30m，且过
    尾部持续 + 缘体证据门——产线每帧都在算，近场车道读数只吃 ≤NEAR_Z 的箱
    （金标契约：远箱不进车道约束），本函数消费它的远箱产出。
    带内有检出取最靠路心者（同墙语义：内侧更窄的可用面；外侧检出会把带撑
    宽，2026-10-20 实机审计同判）；带内无检出向已测远箱借最近者（推断级
    FAR_CARRY_M 内——方向级供数不冒充实测，维护者口径「远模糊」）；再无
    弃权。``edge_pts``：reading 的各箱原始检出 ((side, zc_m, x_m), ...)，
    zc 相机系、x 自车系横向。"""
    inb = [x for (s, zc, x) in (edge_pts or ()) if s == side and z0 <= zc < z1]
    if inb:
        return BandBoundary(min(inb, key=abs), MEASURED, 0.5,
                            None if fid is None else int(fid))
    zc_band = 0.5 * (z0 + z1)
    cand = [(abs(zc - zc_band), x) for (s, zc, x) in (edge_pts or ())
            if s == side and zc >= FAR_Z_CAM - BAND_LEN_M
            and abs(zc - zc_band) <= FAR_CARRY_M]
    if cand:
        return BandBoundary(min(cand)[1], INFERRED, 0.0,
                            None if fid is None else int(fid))
    return None


def _ground_far_boundary(ext, side: int, fid: int | None) -> BandBoundary | None:
    """远粗带该侧的地面横向范围边界（字面密度读法，推断级）。

    ``ext``=(z0, z1, x_lo, x_hi, n_ground)（depth_geo.ground_far_extents 产出，
    每带地面包络参照）：该侧边界=地面可见范围端点。「地面可见范围」含路肩/
    栏外缓坡，比车道边界宽、常贴窗缘——推断级方向供数（维护者口径「水管」），
    不冒充实测；60-80m 结构环读法欠采样失败（2026-10-21 四连测），地面横向
    范围是该距离唯一成立的信号。"""
    x = ext[2] if side < 0 else ext[3]
    if x is None:
        return None
    return BandBoundary(x, INFERRED, 0.0, None if fid is None else int(fid))


def _coarse_bands(far_extents, fid: int | None, *, center_lane, width_lane,
                  lane_w_m: float | None, bands: tuple) -> tuple:
    """中远场粗带拼装（地面密度水管，src=inferred）+ 交接一致性门。

    门：首个粗带（16~30m）与覆盖其起点的 3m 带比**路心**（范围中点 vs 近带
    边界中点），跳变超 FAR_HANDOVER_TOL_LANE 道 → 整带延续近带边界（标
    inferred）——路不在 16m 处瞬移，远区看到别处（混前车/建筑）的污染带
    诚实降级。门只挂在有 3m 参照的首带；更深粗带跳变是方向级供数的真实
    信息（跨带一致率基线记录在案），不拦。"""
    out = []
    ref = None
    if far_extents:
        ref_z = far_extents[0][0]
        ref = next((bd for bd in bands
                    if bd.z0 + CAM_Z_DATUM_M <= ref_z < bd.z1 + CAM_Z_DATUM_M),
                   None)
    for z0, z1, x_lo, x_hi, n in far_extents:
        gate = False
        if ref is not None and x_lo is not None \
                and ref.left is not None and ref.right is not None \
                and ref.left.x_m is not None and ref.right.x_m is not None \
                and lane_w_m:
            cen = 0.5 * (x_lo + x_hi)
            rcen = 0.5 * (ref.left.x_m + ref.right.x_m)
            gate = abs(cen - rcen) > FAR_HANDOVER_TOL_LANE * lane_w_m
        sides = {}
        for side in (-1, 1):
            if gate:
                rb = ref.left if side < 0 else ref.right
                b = (BandBoundary(rb.x_m, INFERRED, 0.0, rb.last_fid)
                     if rb.x_m is not None else BandBoundary(None, UNKNOWN))
            else:
                b = (_ground_far_boundary((z0, z1, x_lo, x_hi, n), side, fid)
                     or _air_boundary(side, center_lane, width_lane, lane_w_m)
                     or BandBoundary(None, UNKNOWN))
            sides[side] = b
        # 带标挪正同 3m 带：对外车头系（z 减 CAM_Z_DATUM_M）
        out.append(CorridorBand(z0 - CAM_Z_DATUM_M, z1 - CAM_Z_DATUM_M,
                                sides[-1], sides[1], n))
    return tuple(out)


def _band_center_at(bands, z: float) -> float | None:
    """分带走廊在 z 处的中心线（带中点线性插值，原始车头系）。"""
    pts = [(0.5 * (b.z0 + b.z1), 0.5 * (b.left.x_m + b.right.x_m))
           for b in bands
           if b.left.x_m is not None and b.right.x_m is not None]
    if not pts:
        return None
    if z <= pts[0][0]:
        return pts[0][1]
    for (z0, c0), (z1, c1) in zip(pts, pts[1:]):
        if z <= z1:
            w = 0.0 if z1 <= z0 else (z - z0) / (z1 - z0)
            return c0 + (c1 - c0) * w
    return pts[-1][1]


def far_hint_lane(bands, coarse_bands, lane_w_m: float | None) -> float | None:
    """中远场地面可见范围的横向不对称（道，右正，原始车头系）。

    最近一条两侧有证据的粗带中点相对该纵深中心线的偏移。**原始坐标直接
    相减**，不经过选择帧平移——executed/走廊原点不得漏进方向信号。"""
    if not coarse_bands or not lane_w_m or lane_w_m <= 0:
        return None
    for b in coarse_bands:
        if b.left.x_m is None or b.right.x_m is None:
            continue
        cen = _band_center_at(bands, 0.5 * (b.z0 + b.z1))
        if cen is None:
            return None
        return (0.5 * (b.left.x_m + b.right.x_m) - cen) / lane_w_m
    return None


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


def _plane_health(reading) -> str:
    """平面健康面：reading 缺席=none；拟合失败或质量门拒绝=degraded。"""
    if reading is None:
        return "none"
    if getattr(reading, "coef", None) is None:
        return "degraded"
    if any("质量门" in r for r in (reading.rejects or ())):
        return "degraded"
    return "ok"


def build_corridor(reading, grid, fid: int | None, *,
                   center_lane: float | None, width_lane: float | None,
                   src: str, lane_w_m: float | None,
                   far_extents: tuple = (),
                   far_ground_supply: bool = True) -> Corridor:
    """分带走廊拼装：center/width/src 由供数门（_EgoRoadObserver 优先级链）
    算好传入，本函数只管分带表示、来源标注与 plane_health。

    带内来源（2026-10-21 收敛定案）：近场带绿区为底，远场带找边读数为底；
    墙融合退役（影子对比实证，见模块头）。``far_extents``：同帧中远场粗带
    地面横向范围（depth_geo.ground_far_extents 产出）——开关 far_ground_supply
    开时拼成 coarse_bands（地面密度水管，16~80m，src=inferred，**不进
    bands**：车道级消费面只读 bands，远带方向级接入是显式消费）；
    关（或无同帧范围）时 coarse_bands 为空。

    近带中心守卫：近带（z<NEAR_BAND_GUARD_Z）双侧中点偏离绝对路心超
    NEAR_BAND_CENTER_TOL_M → 该带退回空气墙。保险杠近场视锥/挖洞会把绿区
    裁成偏心小块，分带中心与包络（选择帧中心、规划器 center0）不得脱离
    墙/空气墙绝对链。"""
    bands = []
    edge_pts = (getattr(reading, "edge_pts", ()) or ()) if reading is not None else ()
    for i in range(N_BANDS):
        z0, z1 = _band_bounds(i)
        far = z0 >= FAR_Z_CAM
        sides = {}
        for side in (-1, 1):
            if far:
                b = (_edge_far_boundary(edge_pts, side, z0, z1, fid)
                     or _air_boundary(side, center_lane, width_lane, lane_w_m)
                     or BandBoundary(None, UNKNOWN))
            else:
                b = (_green_boundary(grid, side, z0, z1)
                     or _air_boundary(side, center_lane, width_lane, lane_w_m)
                     or BandBoundary(None, UNKNOWN))
            sides[side] = b
        if (z0 < NEAR_BAND_GUARD_Z and center_lane is not None
                and width_lane is not None and lane_w_m):
            lx, rx = sides[-1].x_m, sides[1].x_m
            if lx is not None and rx is not None and \
                    abs(0.5 * (lx + rx)
                        - center_lane * lane_w_m) > NEAR_BAND_CENTER_TOL_M:
                # 近带偏心（近场视锥/挖洞裁出的破碎绿区）→ 退回绝对路心
                # 的空气墙：分带中心与包络不得脱离墙/空气墙绝对链。
                for side in (-1, 1):
                    sides[side] = (
                        _air_boundary(side, center_lane, width_lane, lane_w_m)
                        or BandBoundary(None, UNKNOWN))
        n_pts = 0
        if grid is not None:
            rows = _band_rows_mask(grid, z0, z1)
            n_pts = int(grid.counts[rows].sum())
        # 带标挪正（2026-10-10）：对外字段车头系（z 减 CAM_Z_DATUM_M），带内
        # 匹配（找边箱/栅格行）仍走相机系——测量帧不变，表示面诚实
        bands.append(CorridorBand(z0 - CAM_Z_DATUM_M, z1 - CAM_Z_DATUM_M,
                                  sides[-1], sides[1], n_pts))
    coarse = (_coarse_bands(far_extents, fid, center_lane=center_lane,
                            width_lane=width_lane, lane_w_m=lane_w_m,
                            bands=tuple(bands))
              if far_ground_supply and far_extents else ())
    return Corridor(tuple(bands), center_lane, width_lane, src,
                    _plane_health(reading), coarse)
