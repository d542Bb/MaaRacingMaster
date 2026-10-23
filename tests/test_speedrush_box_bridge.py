# -*- coding: utf-8 -*-
"""框高比桥接与锚点框高一致门回归锁（2026-10-23 四局离线验证的落地）。

锁定：锚间插值只在 8 m 外/新鲜框/未翻跳时生效；trace_only 不改发布距离；
闸开时“框高比同意”的真高速接近不再被清锚，“框高比不同意”的单锚保旧、
连续两个一致候选锚才重基；闸关逐位保留旧口径。
"""

from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.depth_geo import (
    AnchorEntry, DepthAnchorSheet)
from maaracing_master.plugins.speedrush.perception import (
    Detection, PerceptionResult)
from maaracing_master.plugins.speedrush.tracking import (
    DistanceMethod, DistanceSource, Tracker, to_jsonable)

NS = 33_000_000


def _per(fid: int, *, cars=()) -> PerceptionResult:
    return PerceptionResult(frame_id=fid, ts_ns=fid * NS,
                            cars=list(cars), coins=[], bonuses=[])


def _car(h=40, cx=800, cy=500):
    return Detection(cx, cy, int(h * 1.5), h, 0.9)


def _sheet(fid: int, z_m=50.0, h_px=40, det_idx=0) -> DepthAnchorSheet:
    return DepthAnchorSheet(
        fid=fid, ts_ns=fid * NS,
        entries=(AnchorEntry(det_idx=det_idx, x0=700, y0=480, x1=790,
                             y1=480 + h_px, z_m=z_m),))


def _dist(obs, tid=1):
    return next(d for d in obs.distances if d.target_id == tid)


def _anchor(trk, z=50.0, h=30, fid=1):
    """建一条已锚定的车轨（锚点帧 fid=1，框高 h）。"""
    trk.update(_per(1, cars=[_car(h=h)]), frame_age_ms=5.0, stage=1)
    trk.update(_per(2, cars=[_car(h=h)]), frame_age_ms=5.0, stage=1,
               anchors=_sheet(1, z_m=z, h_px=h))


# ---------- 桥接 ----------

def test_bridge_trace_only_keeps_published_distance():
    """首局验证口径：桥接值只进 trace，发布距离仍是持锚。"""
    trk = Tracker(box_bridge_enabled=True, box_bridge_trace_only=True)
    _anchor(trk, z=50.0, h=30)
    obs = trk.update(_per(3, cars=[_car(h=40)]), frame_age_ms=5.0, stage=1)
    d = _dist(obs)
    assert d.metric_distance_m == pytest.approx(50.0)
    assert d.method is DistanceMethod.HOLD
    assert d.box_bridge_m == pytest.approx(50.0 * 30 / 40)
    assert to_jsonable(d)["box_bridge_m"] == pytest.approx(37.5)


def test_bridge_publishes_when_trace_only_off():
    trk = Tracker(box_bridge_enabled=True, box_bridge_trace_only=False)
    _anchor(trk, z=50.0, h=30)
    obs = trk.update(_per(3, cars=[_car(h=40)]), frame_age_ms=5.0, stage=1)
    d = _dist(obs)
    assert d.metric_distance_m == pytest.approx(37.5)
    assert d.method is DistanceMethod.BOX_RATIO
    assert d.source is DistanceSource.ANCHOR


def test_bridge_exits_after_box_height_flip():
    """框高单拍翻跳（框头↔框全车）后本锚期不再信任框高比。"""
    trk = Tracker(box_bridge_enabled=True, box_bridge_trace_only=False)
    _anchor(trk, z=50.0, h=30)
    obs = trk.update(_per(3, cars=[_car(h=36)]), frame_age_ms=5.0, stage=1)
    assert _dist(obs).box_bridge_m == pytest.approx(50.0 * 30 / 36)
    obs = trk.update(_per(4, cars=[_car(h=60)]), frame_age_ms=5.0, stage=1)
    d = _dist(obs)
    assert d.box_bridge_m is None
    assert d.metric_distance_m == pytest.approx(50.0)   # 退回持锚


def test_bridge_not_used_near_field():
    trk = Tracker(box_bridge_enabled=True, box_bridge_trace_only=False)
    _anchor(trk, z=7.0, h=120)
    obs = trk.update(_per(3, cars=[_car(h=150)]), frame_age_ms=5.0, stage=1)
    d = _dist(obs)
    assert d.box_bridge_m is None
    assert d.metric_distance_m == pytest.approx(7.0)


def test_bridge_publishes_from_eight_meters():
    """保险杠机位：8 m 外即发布框比（旧追尾镜头前移约 4 m 的等效口径）。"""
    trk = Tracker(box_bridge_enabled=True, box_bridge_trace_only=False)
    _anchor(trk, z=9.0, h=120)
    obs = trk.update(_per(3, cars=[_car(h=150)]), frame_age_ms=5.0, stage=1)
    d = _dist(obs)
    assert d.box_bridge_m == pytest.approx(9.0 * 120 / 150)
    assert d.method is DistanceMethod.BOX_RATIO


# ---------- 锚点框高一致门 ----------

def test_gate_accepts_fast_real_approach_when_box_agrees():
    """30 m/0.3 s（100 m/s）在旧 45 m/s 门下会被清锚；框高比同意则接受。"""
    trk = Tracker(anchor_box_gate_enabled=True)
    _anchor(trk, z=80.0, h=20)
    for fid, h in zip(range(3, 11), (21, 22, 24, 25, 27, 28, 30, 32)):
        if fid == 10:
            obs = trk.update(
                _per(fid, cars=[_car(h=h)]), frame_age_ms=5.0, stage=1,
                anchors=_sheet(fid, z_m=50.0, h_px=h))
        else:
            obs = trk.update(_per(fid, cars=[_car(h=h)]),
                             frame_age_ms=5.0, stage=1)
    d = _dist(obs)
    assert d.source is DistanceSource.ANCHOR       # 没有被清成 BOX_SIZE
    assert d.metric_distance_m == pytest.approx(50.0)


def test_gate_rejects_disagreeing_anchor_then_rebases_on_second():
    """框高不动而距离掉 19 m：单锚保旧，第二个一致候选锚才重基。"""
    trk = Tracker(anchor_box_gate_enabled=True)
    _anchor(trk, z=80.0, h=20)
    # 第二个锚：框高 23、z=79（框高比同意），并建立 v_close≈6 m/s
    obs = trk.update(_per(6, cars=[_car(h=23)]), frame_age_ms=5.0, stage=1,
                     anchors=_sheet(6, z_m=79.0, h_px=23))
    assert _dist(obs).metric_distance_m == pytest.approx(79.0)
    for fid in range(7, 13):
        trk.update(_per(fid, cars=[_car(h=23)]), frame_age_ms=5.0, stage=1)
    # 框高不动、距离掉 19 m：框高比残差 31.7% → 怀疑，保旧锚
    obs = trk.update(_per(13, cars=[_car(h=23)]), frame_age_ms=5.0, stage=1,
                     anchors=_sheet(13, z_m=60.0, h_px=23))
    d = _dist(obs)
    assert d.metric_distance_m == pytest.approx(79.0)
    assert d.v_close_mps is not None                # 被拒锚不动旧锚生命周期
    assert trk._ingest_stats["rej"] == 1
    for fid in range(14, 20):
        trk.update(_per(fid, cars=[_car(h=23)]), frame_age_ms=5.0, stage=1)
    # 第二个彼此一致的候选锚 → 旧锚才是错的，重基
    obs = trk.update(_per(20, cars=[_car(h=23)]), frame_age_ms=5.0, stage=1,
                     anchors=_sheet(20, z_m=60.0, h_px=23))
    d = _dist(obs)
    assert d.metric_distance_m == pytest.approx(60.0)
    assert d.v_close_mps is None                    # 重基=换基线，旧速度作废
    assert trk._ingest_stats["rebase"] == 1


def test_gate_off_keeps_old_cluster_clear():
    """闸关=今天：30 m 跳变仍走 20 m 簇跳变门清锚。"""
    trk = Tracker()
    _anchor(trk, z=80.0, h=20)
    obs = trk.update(_per(4, cars=[_car(h=20)]), frame_age_ms=5.0, stage=1,
                     anchors=_sheet(4, z_m=50.0, h_px=20))
    d = _dist(obs)
    assert d.source is DistanceSource.BOX_SIZE
    assert d.reason.value == "CLUSTER_SWITCH"


def test_gate_off_never_rebases_on_repeated_rejects():
    """闸关=今天：连续被速度门拒的锚只保旧锚，不学闸开后的“两次重基”。"""
    trk = Tracker()
    _anchor(trk, z=80.0, h=20)
    for fid in (4, 5):
        obs = trk.update(_per(fid, cars=[_car(h=20)]), frame_age_ms=5.0,
                         stage=1, anchors=_sheet(fid, z_m=62.0, h_px=20))
        assert _dist(obs).metric_distance_m == pytest.approx(80.0)
        assert trk._ingest_stats["rej"] == 1
        assert trk._ingest_stats["rebase"] == 0


def test_box_bridge_config_is_loaded():
    from maaracing_master.plugins.speedrush.config import load_decision
    cfg = load_decision()
    assert cfg.box_bridge.enabled is True
    assert cfg.box_bridge.trace_only is False
    assert cfg.box_bridge.anchor_gate is True
