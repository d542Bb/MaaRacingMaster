# -*- coding: utf-8 -*-
"""分带走廊（corridor.py + _EgoRoadObserver）的回归锁。

合成用例锁语义（2026-10-21 走廊收敛定案）：近场带绿区为底、远场带找边读数
为底（带内实测/借界推断/空气墙兜底/unknown 诚实降级）、远箱不进近场带、
墙实体路径（单侧/遮挡从全盲变半盲供数）、区兜底只在新鲜拍、拒绝不兜底
（宁弃不喂）、plane_health 标注。数据面锁 _side_has_space 的带来源规则
（tests/test_speedrush_decision.py）。"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from maaracing_master.plugins.speedrush import module as smod
from maaracing_master.plugins.speedrush.corridor import (
    INFERRED, UNKNOWN, Corridor, build_corridor)
from maaracing_master.plugins.speedrush.depth_geo import (
    GRID_DRIVABLE, GRID_UNKNOWN, GRID_X_MAX, DepthRoadReading)
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


def _grid(rows, col_lo=50, col_hi=73, blk_lo=0, blk_hi=0):
    """绿区 DrivableGrid：行号直接给（zc=3+(iz+0.5)·0.25），列 50~73 =
    X∈[−1.5,+4.5]m。blk_lo/blk_hi>0 时左/右外侧邻格给 blocked（观测到的
    高结构=实测绿区边界的证据面）。"""
    from maaracing_master.plugins.speedrush import depth_geo as dg
    st = np.full((dg.GRID_NZ, dg.GRID_NX), dg.GRID_UNKNOWN, np.int8)
    for iz in rows:
        st[iz, col_lo:col_hi + 1] = dg.GRID_DRIVABLE
        if blk_lo:
            st[iz, col_lo - blk_lo:col_lo] = dg.GRID_BLOCKED
        if blk_hi:
            st[iz, col_hi + 1:col_hi + 1 + blk_hi] = dg.GRID_BLOCKED
    return dg.DrivableGrid(state=st, coef=(0.0, 0.0, 0.72),
                           counts=np.full(st.shape, 9, np.int32),
                           dig_cells=0, latency_ms=0.0)


def _off(c):
    return None if c is None else -c.center_lane


# ---------- 近场带：绿区为底、空气墙兜底 ----------

def test_near_band_green_base_air_when_no_green():
    """近场带：绿区在场的带用绿区实测边界（带 6-9m 左外邻 blocked）；
    绿区缺席的带落空气墙（inferred，center±W/2）——墙箱不再接棒带边界
    （2026-10-21 收敛定案：墙融合退役，墙实体仅存后备链单侧墙中心）。"""
    grid = _grid(range(12, 24), blk_lo=1)    # 绿区 z 6~9m（带 6-9），左外邻 blocked
    c = build_corridor(_bnd(), grid, fid=11,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    assert c.bands[1].left.src == "measured"          # 绿区接棒（带 6-9）
    assert c.bands[1].left.conf == 1.0
    assert c.bands[1].left.last_fid is None           # 绿区边界无墙箱年龄
    assert c.bands[0].left.src == INFERRED            # 带 0 无绿区→空气墙
    assert c.bands[0].left.x_m == pytest.approx(-2.0 * LW)
    assert c.bands[0].right.src == INFERRED
    assert c.bands[0].right.x_m == pytest.approx(+2.0 * LW)
    assert c.plane_health == "ok"


def test_green_boundary_data_edge_falls_to_air():
    """绿区到头是 unknown（数据到头：视锥截断/点饥饿）≠ 实测边界 → 不给
    measured 绿区边界，带落空气墙（宁缺勿假——d00036 近带绿左逐行贴视锥
    边界 0.1~0.3m，2026-10-10 定案：数据截断不冒充实测）。"""
    grid = _grid(range(12, 24))              # 外侧邻格全 unknown
    c = build_corridor(_bnd(), grid, fid=11,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    assert c.bands[1].left.src == INFERRED
    assert c.bands[1].left.x_m == pytest.approx(-2.0 * LW)
    assert c.bands[1].left.conf == 0.0


def test_green_boundary_window_edge_falls_to_air():
    """绿区顶到栅格窗缘（列 0，外邻格出窗）= 数据边界，同 unknown 判 →
    落空气墙（窗缘也是数据到头，不是墙）。"""
    grid = _grid(range(12, 24), col_lo=0)
    c = build_corridor(_bnd(), grid, fid=11,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    assert c.bands[1].left.src == INFERRED


def test_green_boundary_median_over_observed_rows_only():
    """带内部分行外邻格是 unknown（数据到头）：中位只在 observed 行上取
    （unknown 邻格行不进中位）。"""
    from maaracing_master.plugins.speedrush import corridor as corr
    grid = _grid(range(12, 24), blk_lo=1)
    # 前 4 行 observed（edge 列 36）；后 8 行绿区右移 4 列且外邻格 unknown
    # ——若 unknown 邻格行进中位，中位被拽到列 54（x=−0.375）
    grid.state[16:24, 49:55] = GRID_UNKNOWN
    grid.state[16:24, 54] = GRID_DRIVABLE
    b = corr._green_boundary(grid, -1, 6.0, 9.0)
    assert b is not None and b.src == "measured"
    assert b.x_m == pytest.approx(-GRID_X_MAX + 50.5 * 0.25)


def test_green_boundary_below_min_observed_falls_to_air():
    """observed 行不足下限（单行 blocked 邻格=单行噪声）→ 不冒充实测，
    落空气墙。"""
    grid = _grid(range(12, 24), blk_lo=1)
    grid.state[13:24, 49] = GRID_UNKNOWN          # 只剩 1 行 observed
    c = build_corridor(_bnd(), grid, fid=11,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    assert c.bands[1].left.src == INFERRED


def test_band_unknown_when_no_center():
    """无中心先验（W 未成形）且无找边无绿区 → unknown（不臆造边界）。"""
    c = build_corridor(_bnd(), None, fid=1,
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
    c = build_corridor(r_bad, None, fid=1, center_lane=None,
                       width_lane=None, src="none", lane_w_m=LW)
    assert c.plane_health == "degraded"
    c2 = build_corridor(None, None, fid=1, center_lane=None,
                        width_lane=None, src="none", lane_w_m=LW)
    assert c2.plane_health == "none"


# ---------- 远场带：找边读数为底（2026-10-21 收敛定案） ----------

def test_far_band_edge_pts_measured():
    """远场带：带内有找边检出 → measured（取最靠路心者），来源标测量帧。"""
    bnd = _bnd(edge_pts=[(1, 18.0, 4.5), (1, 18.0, 8.0)])
    c = build_corridor(bnd, None, fid=21,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    b5 = c.bands[5]                            # 相机系 18~21m（含 zc=18 箱）
    assert b5.z0 == pytest.approx(18.0 - 5.0)  # 车头系带标
    assert b5.right.src == "measured"
    assert b5.right.x_m == pytest.approx(4.5)  # 最靠路心者，外侧检出不当边界
    assert b5.right.conf == pytest.approx(0.5)
    assert b5.right.last_fid == 21


def test_far_band_carry_forward_inferred():
    """远场带带内无检出：向已测远箱借最近者 → inferred（方向级，FAR_CARRY_M
    内）；超窗落空气墙/unknown——借界不冒充实测（维护者口径「远模糊」）。"""
    bnd = _bnd(edge_pts=[(1, 18.0, 4.5)])
    c = build_corridor(bnd, None, fid=21,
                       center_lane=None, width_lane=None, src="pair",
                       lane_w_m=LW)
    assert c.bands[4].right.src == INFERRED    # 带 15~18m：借 zc=18（距离 1.5m）
    assert c.bands[4].right.x_m == pytest.approx(4.5)
    assert c.bands[4].right.conf == 0.0
    assert c.bands[6].right.src == INFERRED    # 带 21~24m：借 zc=18（距离 4.5m）
    assert c.bands[7].right.src == UNKNOWN     # 带 24~27m：距离 7.5m > 6m 超窗


def test_near_band_ignores_far_edge_pts():
    """找边远箱只进远场带供方向，不进近场带（金标契约：远箱不进车道约束）。
    近场带只有远箱读数时落空气墙，不拿远箱当近场边界。"""
    bnd = _bnd(edge_pts=[(1, 18.0, 4.5)])
    c = build_corridor(bnd, None, fid=21,
                       center_lane=0.0, width_lane=4.0, src="pair", lane_w_m=LW)
    assert c.bands[0].right.src == INFERRED    # 空气墙，不是远箱
    assert c.bands[0].right.x_m == pytest.approx(+2.0 * LW)
    assert c.bands[1].right.src == INFERRED


def test_far_band_without_evidence_no_center_unknown():
    """远场带无找边读数、无中心先验 → unknown（不臆造边界）。"""
    c = build_corridor(_bnd(), None, fid=1,
                       center_lane=None, width_lane=None, src="none",
                       lane_w_m=LW)
    assert c.bands[7].right.src == UNKNOWN


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
    # 走廊带已无墙融合（2026-10-21 收敛定案）：近场带无绿区落空气墙
    assert c.bands[0].left.src == INFERRED
    assert c.bands[0].left.last_fid is None


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


# ---------- 主候选供数门分账（2026-10-17 拒绝拍审计面） ----------

def test_rejected_pair_records_reject_and_pair_w():
    """垃圾对拒绝拍：reject=off_max、off_raw/pair_w 落快照——三道门分账
    审计面（trace ro_off_raw/ro_w/ro_pair_w/ro_reject 列数据源）。"""
    o = smod._EgoRoadObserver(lane_w_m=LW)
    o.update(_bnd(-6.0, -2.0), now=1000.0, grid=_grid(range(12, 24)), fid=1)
    assert o.last["reject"] == "off_max"
    assert o.last["off_raw"] == pytest.approx(4.0, abs=0.01)   # -((-6−2)/2)
    assert o.last["pair_w"] == pytest.approx(4.0, abs=0.01)    # -2−(−6)


def test_width_guard_reject_recorded():
    """对宽门拒绝拍：W 成形后对宽超出 ±20% 的对 reject=width_guard，
    pair_w=原始对宽（滤波前）。"""
    o = smod._EgoRoadObserver(lane_w_m=LW)
    for i in range(30):                       # W 成形所需最少对宽数
        assert o.update(_bnd(-2.0, 2.0), now=1000.0 + 0.05 * i,
                        grid=_grid(range(12, 24)), fid=i + 1) is not None
    assert o.width == pytest.approx(4.0)
    assert o.update(_bnd(-1.0, 0.2), now=1002.0,
                    grid=_grid(range(12, 24)), fid=31) is None
    assert o.last["reject"] == "width_guard"
    assert o.last["pair_w"] == pytest.approx(1.2, abs=0.01)


def test_accepted_pair_reject_none():
    """接受拍 reject=None（拒绝标记只在拒绝拍出场）。"""
    o = smod._EgoRoadObserver(lane_w_m=LW)
    assert o.update(_bnd(-2.0, 2.0), now=1000.0, grid=_grid(range(12, 24)),
                    fid=1) is not None
    assert o.last["reject"] is None


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


# ---------- 带标车头系（2026-10-10 挪正） ----------

def test_band_z_labels_head_frame():
    """带标挪正：CorridorBand.z0/z1 = 相机系减 CAM_Z_DATUM_M（车头前，负=
    车头后）；带内匹配仍走相机系——band 0 覆盖相机 [GRID_Z_LO, +3m)，带内
    找边箱/栅格行匹配与带标一致（同一条带的两侧标对了才叫挪正）。"""
    from maaracing_master.plugins.speedrush import depth_geo as dg
    c = build_corridor(_bnd(), _grid(range(12, 24)), fid=1,
                       center_lane=0.0, width_lane=None, src="pair",
                       lane_w_m=LW)
    assert len(c.bands) == 9
    b0 = c.bands[0]
    assert b0.z0 == pytest.approx(dg.GRID_Z_LO - dg.CAM_Z_DATUM_M)
    assert b0.z1 == pytest.approx(dg.GRID_Z_LO + 3.0 - dg.CAM_Z_DATUM_M)
    assert c.bands[-1].z1 == pytest.approx(dg.GRID_Z_HI - dg.CAM_Z_DATUM_M)
