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
                            coef=(0.0, 0.0, 1.45))   # bumper 机位 cam_h 物理值


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
    """近场带：绿区在场的带用绿区实测边界（整幅路面，带 6-9m 左外邻 blocked）；
    绿区缺席的带落空气墙（inferred，center±W/2）。近带中心必须锚在绝对
    路心——偏心绿区（保险杠近场视锥裁出的窄条）会被守卫打回空气墙。"""
    grid = _grid(range(12, 24), col_lo=29, col_hi=82,
                 blk_lo=1)     # 整幅路面 X≈±6.75m；左外邻 blocked
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


def test_near_band_offcenter_green_falls_back_to_air_wall():
    """近带绿区被视锥/挖洞裁成偏心小块（双侧中点偏离绝对路心 >1m）→
    整带退回绝对路心±W/2。否则选择帧中心与包络会跟着偏（实拍
    185126_p1：带中心偏离 4.9m、左缘跑到路心右侧，规划器朝墙打舵）。"""
    grid = _grid(range(12, 24), blk_lo=1)    # 偏心窄条 X∈[−1.5,+4.5]
    c = build_corridor(_bnd(), grid, fid=11,
                       center_lane=0.0, width_lane=4.0, src="wall",
                       lane_w_m=LW)
    assert c.bands[1].left.src == INFERRED
    assert c.bands[1].right.src == INFERRED
    assert c.bands[1].left.x_m == pytest.approx(-2.0 * LW)
    assert c.bands[1].right.x_m == pytest.approx(+2.0 * LW)


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
    assert b5.z0 == pytest.approx(18.0 - smod.CAM_Z_DATUM_M)  # 保险杠：相机系=车头系
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
    g = _grid(range(2, 10))                     # 绿区路心 +1.5m（读数行域 3~6m）
    c = o.update(None, now=1000.0, grid=g, fid=1)
    assert c is not None and c.src == "region"
    assert _off(c) == pytest.approx(-1.5 / LW, abs=0.01)
    c2 = o.update(None, now=1000.05, grid=g, fid=2, new=False)
    assert c2 is None                           # 复用拍黑视（旧栅格不冒充）


def test_master_mode_region_first():
    """master 模式：无墙证据时绿区物理边界接棒（双侧宏观读数），
    pair 降为审计候选。"""
    xc = SimpleNamespace(mode="master", tol_lane=0.45, bypass_streak=8)
    o = smod._EgoRoadObserver(xc, LW)
    g = _grid(range(2, 10))                     # 区路心 +1.5m → off≈−0.545（读数行域 3~6m）
    c = o.update(_bnd(-2.0, 2.0), now=1000.0, grid=g, fid=1)
    assert c is not None and c.src == "region"
    assert _off(c) == pytest.approx(-1.5 / LW, abs=0.01)


# ---------- master 绝对路心链（2026-10-23：墙/空气墙，pair/slot 不接棒） ----------

_MASTER = SimpleNamespace(mode="master", tol_lane=0.45, bypass_streak=8)


def _walls(left_lane, right_lane, zc=4.0):
    return [(-1, zc, left_lane * LW), (1, zc, right_lane * LW)]


def _master_wall_observer(n=32, left_lane=-2.0, right_lane=2.0):
    """master 观察器：双侧墙箱攒满置信 + W 成形（墙到墙宽=右−左）。"""
    o = smod._EgoRoadObserver(_MASTER, LW, wall_track=WallTrack(CAL),
                              mem_fallback=True)
    for fid in range(1, n + 1):
        o.update(_bnd(left_lane, right_lane,
                      edge_pts=_walls(left_lane, right_lane)),
                 now=1000.0 + 0.05 * fid, fid=fid)
    return o


def test_master_wall_both_sides_midpoint_and_width():
    """双侧同箱新鲜墙 → 路心中点 + 墙到墙宽（宏观物理边界，车辆已被挖除）。
    非对称对：左 −2.5 道 / 右 +1.5 道 → 中点 −0.5 道、宽 4.0 道。"""
    o = smod._EgoRoadObserver(_MASTER, LW, wall_track=WallTrack(CAL),
                              mem_fallback=True)
    c = o.update(_bnd(-2.5, 1.5, edge_pts=_walls(-2.5, 1.5)),
                 now=1000.0, fid=1)
    assert c is not None and c.src == "wall"
    assert _off(c) == pytest.approx(+0.5, abs=0.01)      # −center
    assert c.width_lane == pytest.approx(4.0, abs=0.01)


def test_master_wall_one_side_air_wall():
    """单侧新鲜墙 + W 先验 → 墙 ∓ W/2（另一侧是空气墙）。左墙转遮挡、
    右墙移到 +2.3 道：中心=右墙−W/2=+0.3 道，不是双侧中点 +0.15。"""
    o = _master_wall_observer()
    c = o.update(_bnd(None, 2.3, edge_pts=_walls(2.3, 2.3)[1:]),
                 now=1001.8, fid=33)
    assert c is not None and c.src == "wall"
    assert _off(c) == pytest.approx(-0.3, abs=0.01)      # center=+0.3
    assert c.width_lane == pytest.approx(4.0, abs=0.01)  # 单侧不更新宽度


def test_master_never_uses_pair_or_slot_as_center():
    """master 下找边对/保鲜槽不得当路心：无墙、无绿区 → 诚实 None
    （旧链在槽 TTL 内会拿上一拍的车位当路心——实拍 ±6m 横摆的根因）。"""
    o = smod._EgoRoadObserver(_MASTER, LW, mem_fallback=True)
    assert o.update(_bnd(-2.9, -0.9), now=1000.0, fid=1) is None
    assert o.update(None, now=1000.05, fid=2, new=False) is None


def test_master_absolute_memory_short_ttl():
    """记忆只桥接短暂遮挡：0.6s 内沿用绝对来源路心，超 0.8s 诚实 None
    （旧 TTL=2s 会把自车横移前的车位当路心）。"""
    o = smod._EgoRoadObserver(_MASTER, LW, mem_fallback=True)
    c = o.update(_bnd(-2.0, 2.0), now=1000.0, grid=_grid(range(2, 10)), fid=1)
    assert c is not None and c.src == "region"
    c2 = o.update(None, now=1000.6, fid=2)
    assert c2 is not None and c2.src == "memory"
    assert _off(c2) == pytest.approx(-1.5 / LW, abs=0.01)
    assert o.update(None, now=1001.0, fid=3) is None


def test_master_ego_at_left_edge_uses_wall_not_pair():
    """实拍 trace 365~376 回归：车贴左墙、单帧找边对报"正中"（0）、绿区弃权
    ——master 只认左墙 + W/2，路心必须在车右侧约 1.4 道；找边对不得接棒。"""
    o = _master_wall_observer()                 # W=4.0 道
    at_edge = _bnd(-1.3, 1.3, edge_pts=[(-1, 8.0, -0.6 * LW)])
    c1 = o.update(at_edge, now=1001.80, fid=33)
    assert c1 is not None and c1.src == "memory"   # 跳变门：先沿用记忆
    assert _off(c1) == pytest.approx(0.0, abs=0.15)  # 不是找边对的 0 道
    c2 = o.update(at_edge, now=1001.85, fid=34)
    assert c2 is not None and c2.src == "wall"
    assert _off(c2) == pytest.approx(-1.4, abs=0.05)  # −(左墙 −0.6 + W/2)


def test_master_wall_contradictory_pair_abstains_to_memory():
    """真左墙 + 假右缘同时在场（宽 2.1 道）→ 双侧宽门拒、两侧单侧反推也
    矛盾 → 墙诚实弃权，记忆保持最后绝对路心；不猜哪一侧（实拍 173018_p2
    的 ±1 道来回摆根因）。"""
    o = _master_wall_observer()                  # 路心 0、W=4.0
    bad = _bnd(-0.6, 1.5, edge_pts=_walls(-0.6, 1.5))
    c = o.update(bad, now=1001.80, fid=33)
    assert c is not None and c.src == "memory"
    assert _off(c) == pytest.approx(0.0, abs=0.01)
    c2 = o.update(bad, now=1001.85, fid=34)
    assert c2 is not None and c2.src == "memory"
    # 矛盾持续超记忆 TTL → 诚实 None（不用窄对/假侧硬撑）
    assert o.update(bad, now=1002.80, fid=53) is None


def test_master_memory_holds_last_accepted_value():
    """记忆=最后采信的绝对路心（不做 EMA）：小步采信 −0.3 道后断供，记忆
    必须还是 −0.3，不是滤波滞后的 −0.06（后者会与墙值交替，形成来回摆）。"""
    o = _master_wall_observer()                  # 路心 0
    c = o.update(_bnd(-1.7, 2.3, edge_pts=_walls(-1.7, 2.3)),
                 now=1001.80, fid=33)
    assert c is not None and c.src == "wall"
    assert _off(c) == pytest.approx(-0.3, abs=0.01)
    o._wt = None                                 # 墙证据消失
    c2 = o.update(None, now=1001.85, fid=34)
    assert c2 is not None and c2.src == "memory"
    assert _off(c2) == pytest.approx(-0.3, abs=0.01)


def test_width_floor_rejects_narrow_pairs():
    """窄于 3.0 道的对不进 W 统计：近场视锥裁切/假缘会给出 2.5~2.7 道的
    "窄走廊"，不能让它定义整局路宽先验（否则真 4.0 道对被判成污染）。"""
    o = smod._EgoRoadObserver()
    for k in range(smod._EgoRoadObserver.W_MIN_SAMPLES + 5):
        o.update(_bnd(-1.3, 1.3), now=1000.0 + 0.05 * k)
    assert o.width is None
    for k in range(smod._EgoRoadObserver.W_MIN_SAMPLES + 5):
        o.update(_bnd(-2.0, 2.0), now=1100.0 + 0.05 * k)
    assert o.width == pytest.approx(4.0)


def test_master_plane_gate_ignores_bad_plane_frame():
    """坏平面拍（cam_h=0.91m，正常 1.4~1.5）的缘点/绿区整帧不进供数：
    沿用近龄墙箱（src=wall_occl），坏帧分带边界落空气墙、plane_health
    降级——当拍 ±1 道的假墙不得改写路心。"""
    o = _master_wall_observer()
    bad = DepthRoadReading(
        left_edge_lane=None, right_edge_lane=None, left_x=None, right_x=None,
        sides=0, latency_ms=1.0, rejects=(),
        edge_pts=tuple(_walls(-3.0, 1.0)), coef=(0.0, 0.0, 0.91))
    c = o.update(bad, now=1001.8, fid=33)
    assert c is not None and c.src == "wall_occl"
    assert _off(c) == pytest.approx(0.0, abs=0.01)
    assert o.last["plane_ok"] is False
    assert c.plane_health == "degraded"
    assert c.bands[0].left.src == INFERRED      # 近带不拿坏帧绿区/缘点
    # 同一坏读数在复用拍被再消费：同样整帧隔离（不能只在 new 拍判）
    c2 = o.update(bad, now=1001.85, fid=34, new=False)
    assert c2 is not None and c2.plane_health == "degraded"


def test_master_jump_gate_waits_for_second_frame():
    """来源跳变 >0.4 道：第一拍沿用记忆，第二拍同向一致才采信
    （一帧野墙/换源不把目标拽飞）。"""
    o = _master_wall_observer()
    wl, wr = -2.0 + 1.2, 2.0 + 1.2        # 双墙同移 +1.2 道（同宽、纯平移）
    c1 = o.update(_bnd(wl, wr, edge_pts=_walls(wl, wr)), now=1001.8, fid=33)
    assert c1 is not None and c1.src == "memory"
    assert _off(c1) == pytest.approx(0.0, abs=0.15)   # EMA 记忆仍在旧路心
    c2 = o.update(_bnd(wl, wr, edge_pts=_walls(wl, wr)), now=1001.85, fid=34)
    assert c2 is not None and c2.src == "wall"
    assert _off(c2) == pytest.approx(-1.2, abs=0.01)


# ---------- 中远场粗带：地面密度水管（2026-10-21 方案定案） ----------

def _ext(lo, hi, n=500, z0=16.0, z1=31.0):
    return (z0, z1, lo, hi, n)


def test_far_coarse_ground_supply_inferred():
    """中远场粗带：同帧地面横向范围 → coarse_bands（src=inferred，「地面可见
    范围」不是车道边界）；带标车头系、n_pts=地面点数、带 z 车头系（相机系减
    基准）。粗带只进 coarse_bands，不进 bands（车道级消费面不吃远带）。"""
    ext = (_ext(-6.8, 6.73), _ext(-6.68, 6.72, z0=31.0, z1=46.0))
    c = build_corridor(None, None, fid=42, center_lane=0.0, width_lane=2.0,
                       src="pair", lane_w_m=LW, far_extents=ext)
    assert len(c.bands) == 9                        # 3m 带不动
    assert len(c.coarse_bands) == 2
    b0 = c.coarse_bands[0]
    assert b0.z0 == pytest.approx(16.0 - smod.CAM_Z_DATUM_M)  # 保险杠：相机系=车头系
    assert b0.z1 == pytest.approx(31.0 - smod.CAM_Z_DATUM_M)
    assert b0.left.src == INFERRED and b0.right.src == INFERRED
    assert b0.left.x_m == pytest.approx(-6.8)
    assert b0.right.x_m == pytest.approx(6.73)
    assert b0.left.last_fid == 42
    assert b0.n_pts == 500
    assert c.coarse_bands[1].z0 == pytest.approx(31.0 - smod.CAM_Z_DATUM_M)


def test_far_coarse_switch_off_empty():
    """开关关（或无同帧范围）→ coarse_bands 为空（车道级行为逐位回空白）。"""
    ext = (_ext(-6.8, 6.73),)
    c = build_corridor(None, None, fid=42, center_lane=0.0, width_lane=2.0,
                       src="pair", lane_w_m=LW, far_extents=ext,
                       far_ground_supply=False)
    assert c.coarse_bands == ()
    c2 = build_corridor(None, None, fid=42, center_lane=0.0, width_lane=2.0,
                        src="pair", lane_w_m=LW, far_extents=(),
                        far_ground_supply=True)
    assert c2.coarse_bands == ()


def test_far_coarse_handover_gate_continues_near_band():
    """交接一致性门：首个粗带范围路心与近带（3m 带空气墙 ±2 道）路心跳变
    超 1.5 道 → 整带延续近带边界（标 inferred）——路不在 16m 处瞬移，远区
    看到别处（混前车/建筑）的污染带诚实降级；正常居中的范围不触发门。"""
    # 污染带：范围 [6,12] 路心 9m，离近带路心 0 差 9m > 1.5 道
    bad = build_corridor(None, None, fid=7, center_lane=0.0, width_lane=2.0,
                         src="pair", lane_w_m=LW, far_extents=(_ext(6.0, 12.0),))
    b0 = bad.coarse_bands[0]
    assert b0.left.src == INFERRED and b0.right.src == INFERRED
    assert b0.left.x_m == pytest.approx(-1.0 * LW)   # 延续近带空气墙（center±半宽）
    assert b0.right.x_m == pytest.approx(+1.0 * LW)
    # 正常带：范围居中（路心 ≈ 0）→ 不触发门，端点即边界
    ok = build_corridor(None, None, fid=7, center_lane=0.0, width_lane=2.0,
                        src="pair", lane_w_m=LW, far_extents=(_ext(-6.8, 6.73),))
    assert ok.coarse_bands[0].left.x_m == pytest.approx(-6.8)
    assert ok.coarse_bands[0].right.x_m == pytest.approx(6.73)


def test_far_coarse_no_evidence_unknown():
    """粗带无地面范围证据（x 为 None）→ 该侧 unknown（不臆造边界）；无中心
    先验时同判。"""
    ext = (_ext(None, None, n=0),)
    c = build_corridor(_bnd(), None, fid=1, center_lane=None, width_lane=None,
                       src="none", lane_w_m=LW, far_extents=ext)
    b0 = c.coarse_bands[0]
    assert b0.left.src == UNKNOWN and b0.right.src == UNKNOWN
    assert b0.left.x_m is None
    assert b0.n_pts == 0


def test_far_coarse_no_center_falls_unknown():
    """无中心先验（W 未成形）且范围缺席 → 粗带 unknown（同 3m 带兜底序）。"""
    c = build_corridor(_bnd(), None, fid=1, center_lane=None, width_lane=None,
                       src="none", lane_w_m=LW,
                       far_extents=(_ext(None, None, n=0),))
    assert c.coarse_bands[0].left.src == UNKNOWN


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


# ---------- 远场宏观锚：粗带中心 ----------


def test_far_center_lane_median_sign_and_filters():
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, CorridorBand, far_center_lane)

    def band(z0, lm, rm, n=10):
        return CorridorBand(z0, z0 + 15.0,
                            BandBoundary(lm, "measured"),
                            BandBoundary(rm, "measured"), n)

    bands = (band(16.0, -4.0, 4.0), band(31.0, -3.0, 5.0),
             band(46.0, -5.0, 3.0))
    # 逐带中心 0 / +1 / −1 m → −0 / −0.296 / +0.296 道，中值 0
    assert far_center_lane(bands, 3.38) == pytest.approx(0.0)
    assert far_center_lane((band(16.0, 0.0, 6.76),), 3.38) \
        == pytest.approx(-1.0)          # 中心 +3.38m → 自车相对路心 −1 道
    assert far_center_lane((band(60.0, -4.0, 4.0),), 3.38) is None
    assert far_center_lane((band(16.0, None, 4.0),), 3.38) is None
    assert far_center_lane((band(16.0, -4.0, 4.0, n=2),), 3.38) is None


# ---------- 同定义对照审计面（2026-10-27：只记录，不参与判定） ----------

def _master_observer(**kw):
    from maaracing_master.plugins.speedrush.config import GridXcheck
    return smod._EgoRoadObserver(xcheck=GridXcheck(mode="master"), **kw)


def test_audit_records_region_clips_from_same_reading():
    """审计值必须来自本拍那条绿区读数本身（触窗比例/绿行数/侧数），
    不是事后重算——同定义对照的全部意义在于"条件与读数同拍"。"""
    o = _master_observer(lane_w_m=LW)
    o.update(_bnd(-2.0, 2.0), now=1000.0, fid=1, grid=_grid(range(12, 24)))
    a = o.last["audit"]
    assert a["n_rows"] == 12 and a["sides"] == 2
    assert a["l_clip"] == 0.0 and a["r_clip"] == 0.0


def test_audit_marks_clipped_side_honestly():
    """左边界贴住视窗最左列 = 假缘：触窗比例>0 即被记下来（该拍绿区双侧
    不成立，不该进"定义重合"子集）。"""
    o = _master_observer(lane_w_m=LW)
    o.update(_bnd(-2.0, 2.0), now=1000.0, fid=1,
             grid=_grid(range(12, 24), col_lo=0))
    assert o.last["audit"]["l_clip"] > 0.0


def test_audit_absent_on_reuse_tick():
    """复用拍没有新的绿区证据——审计面留空，不拿上一拍的值冒充本拍。"""
    o = _master_observer(lane_w_m=LW)
    o.update(_bnd(-2.0, 2.0), now=1000.0, fid=1, grid=_grid(range(12, 24)))
    assert o.last["audit"]["n_rows"] == 12
    o.update(_bnd(-2.0, 2.0), now=1000.05, fid=2, grid=_grid(range(12, 24)),
             new=False)
    assert "n_rows" not in o.last["audit"]


def test_audit_wall_same_bin_flag_tracks_both_sides_live():
    """墙双侧同箱在场 = 同一定义对照的另一个条件：只有两侧都当拍在场才真。"""
    o = _master_observer(lane_w_m=LW, wall_track=WallTrack(CAL))
    o.update(_bnd(-2.0, 2.0, edge_pts=[(-1, 4.0, -5.5), (1, 4.0, 5.5)]),
             now=1000.0, fid=1)
    assert o.last["audit"]["wall_same_bin"] is True
    o2 = _master_observer(lane_w_m=LW, wall_track=WallTrack(CAL))
    o2.update(_bnd(-2.0, 2.0, edge_pts=[(1, 4.0, 5.5)]), now=1000.0, fid=1)
    assert o2.last["audit"]["wall_same_bin"] is False


def test_audit_is_write_only():
    """审计面必须是只写的：类体内除"写 self._audit"与 _snapshot 里的读出打包，
    不得有任何分支读它——一旦有判定读审计面，"加字段不改供数"就不再成立
    （本轮全部结论的前提条件）。"""
    import inspect
    import re

    write = re.compile(r"self\._audit\s*[:\[]")   # 声明 / 整体赋值 / 逐项赋值
    reads = [ln.strip() for ln in inspect.getsource(
        smod._EgoRoadObserver).splitlines()
        if "self._audit" in ln and not write.search(ln)
        and "self._audit =" not in ln]
    assert reads == ['"audit": dict(self._audit)}'], reads
