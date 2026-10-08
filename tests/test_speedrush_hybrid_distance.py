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


def test_interval_velocity_gate_confirms_rescale_and_formula_direction():
    """持续接近：锚龄过评估下限后 v̂=Z_a×ln(h/h_a)/(t−t_a) 连续 2 拍超阈 →
    RESCALED；公式方向：目标变大（接近）→ 距离变近。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0, h_px=40))
    hs = {3: 40, 4: 41, 5: 42, 6: 43, 7: 44, 8: 45}   # 165ms 起 v̂≈29>16，连续确认
    for fid, h in hs.items():
        obs = trk.update(_per(fid, cars=[_car(h=h)]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)                                    # fid=8：锚龄 198ms，已确认
    assert d.source is DistanceSource.RESCALED_ANCHOR
    assert d.method is DistanceMethod.HEIGHT_RATIO
    assert d.metric_distance_m == pytest.approx(50.0 * 40 / 45)
    assert d.anchor_age_ms == pytest.approx(7 * NS / 1e6, rel=1e-3)


def test_gate_velocity_persisted_on_track_state():
    """L2 状态载体（时序状态层刀一）：门控区间速度 v̂ 落轨并透传
    ObjectDistance——此前算完即扔只用于确认位；值与公式同源可复算。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0, h_px=40))
    for fid, h in ((3, 40), (4, 41), (5, 42), (6, 43), (7, 44), (8, 45)):
        obs = trk.update(_per(fid, cars=[_car(h=h)]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)
    expect = 50.0 * np.log(45 / 40) / (7 * NS / 1e9)   # 7 帧龄≈231ms→0.231s，与门控同式
    assert d.v_close_mps == pytest.approx(expect, rel=1e-3)


def test_velocity_resets_with_anchor_lifecycle():
    """速度与锚点同生命周期：无锚恒 None；新锚点重置估计窗（旧速度清零），
    锚龄未达评估下限期间保持 None——不拿旧窗速度冒充新锚状态。"""
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    assert _dist(obs).v_close_mps is None              # 无锚：UNKNOWN 恒 None
    trk.update(_per(2, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0, h_px=40))
    for fid, h in ((3, 41), (4, 42), (5, 43), (6, 44), (7, 45), (8, 46)):
        trk.update(_per(fid, cars=[_car(h=h)]), frame_age_ms=10.0, stage=1)
    obs = trk.update(_per(9, cars=[_car(h=47)]), frame_age_ms=10.0, stage=1)
    assert _dist(obs).v_close_mps is not None          # 锚窗内已落轨
    trk.update(_per(10, cars=[_car(h=47)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(9, z_m=45.0, h_px=47))
    d = _dist(trk.update(_per(11, cars=[_car(h=47)]), frame_age_ms=10.0, stage=1))
    assert d.v_close_mps is None                       # 新锚点 = 新估计窗
    assert d.source is DistanceSource.ANCHOR           # 锚龄 2 帧 < 评估下限：直接持锚


def test_rescaled_is_one_way_until_new_anchor():
    """RESCALED 是锚点生命周期状态：gate 退出不回 ANCHOR（防 42→51 回弹），
    新锚点到达重置基线后才重新判。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_car(h=40)]), frame_age_ms=10.0, stage=1,
               anchors=_sheet(1, z_m=50.0, h_px=40))
    for fid, h in ((3, 40), (4, 41), (5, 42), (6, 43), (7, 44), (8, 45)):
        trk.update(_per(fid, cars=[_car(h=h)]), frame_age_ms=10.0, stage=1)
    assert _dist(trk._last_obs).source is DistanceSource.RESCALED_ANCHOR
    # gate 退出（框缩小=远离）：仍是 RESCALED，公式跟随新 h，绝不吐原始 50
    obs = trk.update(_per(9, cars=[_car(h=44)]), frame_age_ms=10.0, stage=1)
    d = _dist(obs)
    assert d.source is DistanceSource.RESCALED_ANCHOR
    assert d.metric_distance_m == pytest.approx(50.0 * 40 / 44)
    # 新锚点 = 新生命周期：无接近 → 回 ANCHOR，基线重置
    obs = trk.update(_per(10, cars=[_car(h=44)]), frame_age_ms=10.0, stage=1,
                     anchors=_sheet(9, z_m=42.0, h_px=44))
    d = _dist(obs)
    assert d.source is DistanceSource.ANCHOR
    assert d.metric_distance_m == pytest.approx(42.0)


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
