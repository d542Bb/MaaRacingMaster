# -*- coding: utf-8 -*-
"""混合测距刀一回归锁（docs/plan/speedrush-hybrid-ranging-design.md §2/§4/§6/§8）。

锁定：三态状态机（生命周期语义、RESCALED 单行道）、h 重标公式方向、N=2 门控
防抖、两层有效性弃权链（CLUSTER_SWITCH / TRACK_LOST / EXPIRED / ASSOC_FAIL /
NO_ANCHOR_YET）、UNKNOWN 的 metric=None 类型语义、既有 2D 输出零回归、深度侧
锚点清单口径（entity_z 同口径直方图 + 有效域）。
"""

from __future__ import annotations

import numpy as np
import pytest

from dataclasses import replace

from maaracing_master.plugins.speedrush.depth_geo import (
    DepthAnchorSheet, AnchorEntry, anchor_sheet_from_detections)
from maaracing_master.plugins.speedrush.perception import Detection, PerceptionResult
from maaracing_master.plugins.speedrush.tracking import (
    DistanceMethod, DistanceReason, DistanceSource, Tracker, TrackerParams,
    to_jsonable)
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()
NS = 33_000_000   # 测试帧间隔（33ms），与既有 tracking 测试同钟


def _per(fid: int, *, cars=()) -> PerceptionResult:
    return PerceptionResult(frame_id=fid, ts_ns=fid * NS,
                            cars=list(cars), coins=[], bonuses=[])


def _car(h=40, cx=800, cy=500):
    """域内车框（denom≈176）；h 是门控与重标的主角。"""
    return Detection(cx, cy, int(h * 1.5), h, 0.9)


def _sheet(fid: int, z_m=50.0, h_px=40, det_idx=0) -> DepthAnchorSheet:
    return DepthAnchorSheet(
        fid=fid, ts_ns=fid * NS,
        entries=(AnchorEntry(det_idx=det_idx, x0=700, y0=480, x1=790,
                             y1=480 + h_px, z_m=z_m, h_px=h_px,
                             cy_px=480 + h_px / 2),))


def _dist(obs, tid=1):
    return next(d for d in obs.distances if d.target_id == tid)


# ---------- 契约基础 ----------

def test_no_anchors_yet_unknown_metric_none():
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)
    assert d.source is DistanceSource.UNKNOWN
    assert d.metric_distance_m is None          # 类型层防"有序无米"被当数用
    assert d.reason is DistanceReason.NO_ANCHOR_YET


def test_anchor_hold_reports_anchor_z():
    trk = Tracker()
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    obs = trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
                     anchors=_sheet(1, z_m=50.0))
    d = _dist(obs)
    assert d.source is DistanceSource.ANCHOR
    assert d.method is DistanceMethod.HOLD
    assert d.metric_distance_m == pytest.approx(50.0)
    assert d.anchor_age_ms == pytest.approx(NS / 1e6, rel=1e-3)   # 一帧龄


def test_unknown_reason_is_enum_and_jsonable():
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    j = to_jsonable(_dist(obs))
    assert j["source"] == "UNKNOWN" and j["reason"] == "NO_ANCHOR_YET"


# ---------- 门控与状态机 ----------

def test_single_frame_h_jump_does_not_trigger_rescale():
    trk = Tracker()
    trk.update(_per(1, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1))
    obs = trk.update(_per(3, cars=[_car(h=60)]), frame_age_ms=10.0, stage=1)  # 孤立跳变
    assert _dist(obs).source is DistanceSource.ANCHOR   # 锚龄 33ms < 评估下限：门不开


def test_gate_needs_two_consecutive_positive_evaluations():
    """锚龄达评估下限后，单拍速度尖峰（量化跳变）被 N=2 灭掉。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0, h_px=40))
    for fid in range(3, 7):                       # h 不动：锚龄涨到 132ms，门仍不开
        trk.update(_per(fid, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    obs = trk.update(_per(7, cars=[_car(h=60)]), frame_age_ms=10.0, stage=1)  # 尖峰拍(165ms)
    assert _dist(obs).source is DistanceSource.ANCHOR
    obs = trk.update(_per(8, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)  # 回落
    assert _dist(obs).source is DistanceSource.ANCHOR   # streak 清零，未确认


def _two_anchor_setup(trk, *, z1=50.0, z2=44.0):
    """两锚接近场景：fid2 挂锚 z1（ts=fid1），fid3~6 供帧（帧映射表），
    fid7 挂锚 z2 → 锚间 dt=5×33ms=165ms ≥ 评估下限，v_inst=(z1−z2)/0.165。"""
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=z1))
    for fid in range(3, 7):
        trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(7, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(6, z_m=z2))


def test_interval_velocity_gate_confirms_rescale_and_formula_direction():
    """持续接近：锚间区间差分 v̂（2026-10-13 换装，替代 log-h 框高式——框高
    抖动直接打进 v̂，实机生命表实测荒值 40~70m/s）连续 2 拍超阈 → RESCALED；
    当前距离 = 锚点按 v 外推（RANGE_PROP），方向：目标变近 → 距离 < 锚值。"""
    trk = Tracker()
    _two_anchor_setup(trk)                    # v=(50−44)/0.165≈36.4 > 16
    obs = None
    for fid in range(8, 13):                  # fid11 到龄发布，fid12 N=2 确认
        obs = trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)                            # fid=12：锚龄 198ms，已确认
    assert d.source is DistanceSource.RESCALED_ANCHOR
    assert d.method is DistanceMethod.RANGE_PROP
    v = (50.0 - 44.0) / (5 * NS / 1e9)
    assert d.v_close_mps == pytest.approx(v)
    assert d.metric_distance_m == pytest.approx(44.0 - v * 6 * NS / 1e9)
    assert d.anchor_age_ms == pytest.approx(6 * NS / 1e6, rel=1e-3)


def test_gate_velocity_persisted_on_track_state():
    """L2 状态载体：区间速度在 ingest 落 v_inst、到龄发布到 v_close——
    值与锚间差分同源可复算。"""
    trk = Tracker()
    _two_anchor_setup(trk)
    obs = trk.update(_per(11, cars=[_car()]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)
    expect = (50.0 - 44.0) / (5 * NS / 1e9)
    assert d.v_close_mps == pytest.approx(expect, rel=1e-3)


def test_velocity_persists_across_new_anchor():
    """区间速度跨锚有效（2026-10-13 语义变更）：新锚不清 v_close——清零曾造成
    每锚 150ms 的 t_meet 断供，far 轨威胁信号以 MoGe 节拍闪现（2026-10-12 局）。
    新 v_inst 在下一区间形成后到龄顶替；approach_confirmed 照旧重置。"""
    trk = Tracker()
    _two_anchor_setup(trk)
    for fid in range(8, 13):
        trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    assert _dist(trk._last_obs).v_close_mps is not None      # 已落轨
    # 新锚（z 原地：Δ6<跳变门，接受）：approach_confirmed 重置 → 回 ANCHOR，
    # 但 v_close 保留旧区间值（最佳可用速度证据）
    trk.update(_per(13, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(12, z_m=44.0))
    d = _dist(trk.update(_per(14, cars=[_car()]), frame_age_ms=10.0, stage=1))
    assert d.source is DistanceSource.ANCHOR           # 新锚点 = 新生命周期
    assert d.v_close_mps is not None                   # 速度跨锚保留
    assert d.metric_distance_m == pytest.approx(44.0)  # ANCHOR=持锚原值


def test_new_anchor_resets_rescaled_lifecycle():
    """RESCALED 是锚点生命周期状态：确认后**新锚**重置生命周期（approach_
    confirmed 清零）——新区间速度低于阈值不再确认，回 ANCHOR 持锚，但区间
    速度 v_close 照常发布（跨锚保留语义）。（旧 log-h 时代的"gate 退出不回
    ANCHOR"指同锚生命周期内逐拍回落——v_inst 固定后该场景不复存在。）"""
    trk = Tracker()
    _two_anchor_setup(trk)
    for fid in range(8, 13):
        trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    assert _dist(trk._last_obs).source is DistanceSource.RESCALED_ANCHOR
    # 新锚（新区间几乎无接近）：生命周期重置 → ANCHOR 持锚；速度照常发布
    trk.update(_per(13, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(12, z_m=43.0))           # v=(44−43)/0.198≈5.1<16
    for fid in range(14, 18):                 # fid17 到龄发布
        trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    d = _dist(trk._last_obs)
    v2 = 1.0 / (6 * NS / 1e9)                          # 锚间 dt=(12−6)×33ms
    assert d.source is DistanceSource.ANCHOR
    assert d.v_close_mps == pytest.approx(v2)          # 区间速度跨锚照常发布
    assert d.metric_distance_m == pytest.approx(43.0)  # ANCHOR=持锚原值


# ---------- 弃权链 ----------

def test_cluster_switch_voids_anchor():
    trk = Tracker()
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0))
    obs = trk.update(_per(3, cars=[_car()]), frame_age_ms=10.0, stage=1,
                     anchors=_sheet(2, z_m=73.0))   # 主簇跳变（车体↔背景）
    d = _dist(obs)
    assert d.source is DistanceSource.UNKNOWN
    assert d.reason is DistanceReason.CLUSTER_SWITCH
    assert d.metric_distance_m is None


def test_track_lost_keeps_anchor_for_rematch():
    trk = Tracker()
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0))
    obs = trk.update(_per(3), frame_age_ms=10.0, stage=1)   # 本拍丢框
    d = _dist(obs)
    assert d.reason is DistanceReason.TRACK_LOST and d.metric_distance_m is None
    obs = trk.update(_per(4, cars=[_car()]), frame_age_ms=10.0, stage=1)   # 复匹配
    d = _dist(obs)
    assert d.source is DistanceSource.ANCHOR   # 未超龄，锚点恢复


def test_expired_anchor_abstains():
    trk = Tracker()
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0))
    obs = trk.update(_per(15, cars=[_car()]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)
    assert d.reason is DistanceReason.EXPIRED
    assert d.anchor_age_ms > 350.0


def test_assoc_fail_when_sheet_frame_outside_map_window():
    trk = Tracker(params=TrackerParams(assoc_window=1))   # 窗长 1：滞后 2 拍即击穿
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1)
    # 清单来自 2 拍前（窗外）：整张归不了轨，如实报 ASSOC_FAIL
    obs = trk.update(_per(3, cars=[_car()]), frame_age_ms=10.0, stage=1,
                     anchors=_sheet(1, z_m=50.0))
    assert _dist(obs).reason is DistanceReason.ASSOC_FAIL


# ---------- 既有 2D 输出零回归（§8 验收 6） ----------

def test_zero_regression_on_existing_2d_outputs():
    """同输入下，喂锚点与否，既有输出（targets/far/health/排序）逐位一致——
    混合观测只新增 distances，绝不改动既有 2D 行为。"""
    seq = [
        _per(1, cars=[_car(h=40)]),
        _per(2, cars=[_car(h=42)]),
        _per(3),                                   # 丢框拍
        _per(4, cars=[_car(h=44)]),
        _per(5, cars=[_car(h=46), Detection(400, 550, 50, 30, 0.8)]),
        _per(6, cars=[_car(h=48)]),
    ]
    a, b = Tracker(), Tracker()
    outs_a, outs_b = [], []
    for i, per in enumerate(seq):
        outs_a.append(a.update(per, frame_age_ms=10.0, stage=1))
        sheet = _sheet(i, z_m=50.0, h_px=40) if i in (1, 4) else None
        outs_b.append(b.update(per, frame_age_ms=10.0, stage=1, anchors=sheet))
    for oa, ob in zip(outs_a, outs_b):
        assert oa.targets == ob.targets
        assert oa.far_targets == ob.far_targets
        assert oa.health == ob.health
    assert all(ob.distances for ob in outs_b[1:])   # 锚点面只体现在 distances


# ---------- 深度侧锚点清单口径 ----------

def _pts_with_cluster(z_main=42.0, z_far=80.0):
    pts = np.full((598, 336, 3), np.nan, np.float64)
    pts[:, :, 2] = z_main                     # 全幅主簇
    pts[100:110, 100:110, 2] = z_far          # 小块远景簇
    return pts


def test_anchor_sheet_densest_cluster_median():
    pts = _pts_with_cluster()
    sheet = anchor_sheet_from_detections(pts, [(0, 0, 0, 1280, 720)], fid=7, ts_ns=700)
    assert sheet is not None and sheet.fid == 7
    assert len(sheet.entries) == 1
    e = sheet.entries[0]
    assert e.z_m == pytest.approx(42.0)       # 最密簇中位，不被远景簇带跑
    assert e.h_px == 720


def test_anchor_sheet_drops_out_of_domain_and_degenerate_boxes():
    pts = _pts_with_cluster(z_main=120.0)     # 出有效域（>90m）
    assert anchor_sheet_from_detections(pts, [(0, 0, 0, 1280, 720)], fid=1,
                                        ts_ns=1) .entries == ()
    assert anchor_sheet_from_detections(pts, [], fid=1, ts_ns=1) is None  # 零成本路径
    # 2×2 小框（原生 <3px）：量不出 → 静默弃权
    tiny = anchor_sheet_from_detections(_pts_with_cluster(),
                                        [(0, 636, 356, 640, 360)], fid=1, ts_ns=1)
    assert tiny.entries == ()


# ---------- 米制车状态（C 线 2026-10-12：车世界模型搬米制系，计划稿 §5C） ----------

def _mstate(obs, tid=1):
    return next(m for m in obs.metric_cars if m.target_id == tid)


def test_metric_state_unknown_without_anchor():
    """无锚：metric_cars 仍逐车产出（身份对齐 distances），z/x/v/t 全 None
    ——消费端回退像素仪器，不臆造米制。"""
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    m = _mstate(obs)
    assert m.source is DistanceSource.UNKNOWN
    assert m.z_m is None and m.x_m is None
    assert m.v_close_mps is None and m.t_meet_s is None


def test_metric_state_hold_z_and_x_projection():
    """持锚：z=锚原值；x_m=(cx−vpx)·z/fx（同帧焦距反投影，1280 全幅基准）；
    锚龄 < 评估下限 → v/t 弃权（估计窗未开，消费端回退像素）。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car(cx=800)]), frame_age_ms=10.0, stage=1)
    sheet = replace(_sheet(1, z_m=50.0), fx=700.0)
    obs = trk.update(_per(2, cars=[_car(cx=800)]), frame_age_ms=10.0, stage=1,
                     anchors=sheet)
    m = _mstate(obs)
    assert m.source is DistanceSource.ANCHOR
    assert m.z_m == pytest.approx(50.0)
    assert m.x_m == pytest.approx((800.0 - CAL.vpx) * 50.0 / 700.0)
    assert m.v_close_mps is None and m.t_meet_s is None


def test_metric_state_rescaled_t_meet():
    """RESCALED：z=锚按区间速度外推；v_close>0 → t_meet=z/v（一步除法，无
    px/fid 换算、无 fid 率耦合）。"""
    trk = Tracker()
    _two_anchor_setup(trk, z1=50.0, z2=44.0)
    for fid in range(8, 13):
        obs = trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    m = _mstate(obs)
    v = (50.0 - 44.0) / (5 * NS / 1e9)
    z = 44.0 - v * 6 * NS / 1e9
    assert m.z_m == pytest.approx(z)
    assert m.v_close_mps == pytest.approx(v)
    assert m.t_meet_s == pytest.approx(z / v)
    assert m.t_meet_s > 0


def test_metric_state_receding_car_is_pacing_not_unknown():
    """远离车（锚间 z 增大 → v̂<0）：source 仍有效、t=None——占用语义与
    「无锚」分流靠 source/vcls，消费端不回退像素（米制说话算数）。"""
    trk = Tracker()
    _two_anchor_setup(trk, z1=44.0, z2=50.0)          # 远离：z 增
    obs = trk.update(_per(11, cars=[_car()]), frame_age_ms=10.0, stage=1)
    m = _mstate(obs)
    assert m.source is DistanceSource.ANCHOR
    assert m.v_close_mps is not None and m.v_close_mps < 0
    assert m.t_meet_s is None


def test_metric_state_without_fx_abstains_lateral_only():
    """锚点清单缺焦距（旧路径）：x_m 弃权、z 照供——不拿常数焦距冒充同帧。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    obs = trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
                     anchors=_sheet(1, z_m=50.0))          # fx=None（默认）
    m = _mstate(obs)
    assert m.z_m == pytest.approx(50.0)
    assert m.x_m is None


def test_far_track_still_gets_metric_state():
    """地平线退化带住户（denom<min_denom 降级 far 轨）：米制态照产出——
    这正是本次搬家要治的盲区本体，far 不再等于决策不可见。"""
    trk = Tracker()
    far = _car(cx=660, cy=335)                # denom=335−324.2≈11 < 20：far
    trk.update(_per(1, cars=[far]), frame_age_ms=10.0, stage=1)
    assert trk.update(_per(2, cars=[far]), frame_age_ms=10.0, stage=1,
                      anchors=replace(_sheet(1, z_m=40.0), fx=700.0)) \
        .far_targets                          # 确在 far（x_lane 无效）
    obs = trk.update(_per(3, cars=[far]), frame_age_ms=10.0, stage=1)
    assert obs.targets == ()                  # 像素域内无它
    m = _mstate(obs)
    assert m.z_m == pytest.approx(40.0)
    assert m.x_m == pytest.approx((660.0 - CAL.vpx) * 40.0 / 700.0)


def test_cluster_switch_clears_fx_with_anchor():
    """焦距锚与 z 锚同生命周期：簇跳清锚后 x_m 一并弃权，重锚恢复。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car(cx=800)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car(cx=800)]), frame_age_ms=10.0, stage=1,
               anchors=replace(_sheet(1, z_m=50.0), fx=700.0))
    trk.update(_per(3, cars=[_car(cx=800)]), frame_age_ms=10.0, stage=1,
               anchors=replace(_sheet(3, z_m=89.0), fx=700.0))   # 跳变 >20m：清锚
    m = _mstate(trk.update(_per(4, cars=[_car(cx=800)]),
                           frame_age_ms=10.0, stage=1))
    assert m.z_m is None and m.x_m is None
    assert m.source is DistanceSource.UNKNOWN


def test_sheet_carries_frame_focal():
    """锚点清单携带同帧焦距（契约加法）：fx 原样透传，缺省 None。"""
    pts = _pts_with_cluster()
    s = anchor_sheet_from_detections(pts, [(0, 0, 0, 1280, 720)], fid=7,
                                     ts_ns=700, fx=697.3)
    assert s.fx == pytest.approx(697.3)
    assert anchor_sheet_from_detections(pts, [(0, 0, 0, 1280, 720)],
                                        fid=7, ts_ns=700).fx is None


def test_jump_gate_drops_flip_and_keeps_old_anchor():
    """亚门限翻跳丢弃门（2026-10-13）：相邻锚 |Δz| 超过物理闭速界 → 丢弃该锚
    保旧锚（z 与锚龄都不动），下一正常锚恢复区间差分——量错不是真动。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car()]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=44.0))
    for fid in range(3, 7):
        trk.update(_per(fid, cars=[_car()]), frame_age_ms=10.0, stage=1)
    # Δ=13m > z_jump_m(12) 且 < cluster_switch_m(20)：丢弃，不清锚
    trk.update(_per(7, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(6, z_m=57.0))
    d = _dist(trk.update(_per(11, cars=[_car()]), frame_age_ms=10.0, stage=1))
    assert d.source is DistanceSource.ANCHOR
    assert d.metric_distance_m == pytest.approx(44.0)     # 旧锚原值
    # 下一正常锚（Δ=0.5）：接受，区间差分从旧锚起算（dt=10 帧）
    trk.update(_per(12, cars=[_car()]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(11, z_m=44.5))
    obs = trk.update(_per(16, cars=[_car()]), frame_age_ms=10.0, stage=1)
    d2 = _dist(obs)
    assert d2.v_close_mps == pytest.approx((44.0 - 44.5) / (10 * NS / 1e9))


def test_box_anchor_z_prefers_near_cluster():
    """近簇选择（2026-10-13 换装）：远车框里背景簇比车身簇更密时取**车身**
    （近）簇——实心物体对背景，比车更近的只有噪声级散点；旧"最密簇"口径
    在此场景翻到背景（生命表实测逐锚跳 10~15m）。密度比按实机紧框量级
    （背景:车身 ≈ 5:3，翻跳正发生在这量级）。"""
    pts = np.full((598, 336, 3), np.nan, np.float64)
    pts[300:320, 150:175, 2] = 60.0                       # 背景 60m：500 像素
    pts[300:315, 150:170, 2] = 25.0                       # 车身 25m：300 像素（近）
    sheet = anchor_sheet_from_detections(pts, [(0, 0, 0, 1280, 720)],
                                         fid=1, ts_ns=1)
    assert sheet.entries[0].z_m == pytest.approx(25.0, abs=1.5)
