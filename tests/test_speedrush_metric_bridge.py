# -*- coding: utf-8 -*-
"""速度像素桥（闸 metric_bridge，证据闪 2026-10-17 方向 1）的回归锁。

锁定：闸关逐位回今天（桥接字段恒 False/None、t_meet 缺席不桥）、桥接进入
（米制 t_meet 缺席=UNKNOWN 五因/估计窗未开/丢框保锚）、桥接值与标记
（bridged/bridge_reason，公式真源 t_meet_car_s）、每侧最短驻留迟滞（换侧
须当前侧驻留 ≥ stay 拍）、不确定度消费面（碰撞判据威胁降权）。
"""

from __future__ import annotations

import math
from dataclasses import fields

from maaracing_master.plugins.speedrush.config import DecisionConfig
from maaracing_master.plugins.speedrush.corridor_reach import (
    CarSource, CoinSource, CorridorBandSpan, ReachParams, evaluate)
from maaracing_master.plugins.speedrush.perception import Detection, PerceptionResult
from maaracing_master.plugins.speedrush.tracking import (
    DistanceSource, Tracker)
from maaracing_master.plugins.speedrush.depth_geo import (
    DepthAnchorSheet, AnchorEntry)

NS = 33_000_000   # 测试帧间隔（33ms），与既有 tracking 测试同钟


def _per(fid: int, *, cars=()) -> PerceptionResult:
    return PerceptionResult(frame_id=fid, ts_ns=fid * NS,
                            cars=list(cars), coins=[], bonuses=[])


def _car(cx=800, cy=500):
    return Detection(cx, cy, 60, 40, 0.9)


def _sheet(fid: int, z_m=50.0) -> DepthAnchorSheet:
    return DepthAnchorSheet(
        fid=fid, ts_ns=fid * NS,
        entries=(AnchorEntry(det_idx=0, x0=700, y0=480, x1=790,
                             y1=520, z_m=z_m, h_px=40, cy_px=500),))


def _m(obs):
    return obs.metric_cars[0]


# ---------- 闸默认关：逐位回今天 ----------

def test_metric_bridge_defaults_off():
    f = {x.name: x for x in fields(DecisionConfig)}
    b = f["metric_bridge"].default
    assert b.enabled is False and b.stay == 2 \
        and b.weight == 0.5 and b.margin_add_s == 0.15


def test_bridge_off_absent_fields_and_no_t():
    trk = Tracker()
    obs = None
    for fid in range(1, 12):
        obs = trk.update(_per(fid, cars=[_car(cy=500 + 5 * fid)]),
                         frame_age_ms=10.0, stage=1)
    m = _m(obs)
    assert m.source is DistanceSource.UNKNOWN
    assert m.t_meet_s is None            # 无锚：米制 t 缺席（闸关不桥）
    assert m.bridged is False and m.bridge_reason is None


# ---------- 桥接进入与标记 ----------

def test_bridge_engages_on_unknown_approach():
    """从未锚定的逼近车：首个米制缺席拍即进桥（初驻留即满），fid 率预热后
    出桥接 t（同一公式真源 t_meet_car_s），进入原因=UNKNOWN 原值。"""
    trk = Tracker(bridge_enabled=True, bridge_stay=2)
    obs = None
    for fid in range(1, 12):
        obs = trk.update(_per(fid, cars=[_car(cy=500 + 5 * fid)]),
                         frame_age_ms=10.0, stage=1)
    m = _m(obs)
    assert m.bridged is True
    assert m.t_meet_s is not None and math.isfinite(m.t_meet_s) \
        and m.t_meet_s > 0.0
    assert m.bridge_reason == "NO_ANCHOR_YET"


def test_bridge_engages_on_est_window():
    """有锚但估计窗未开（单锚无区间）：米制 t 缺席同样进桥。"""
    trk = Tracker(bridge_enabled=True, bridge_stay=2)
    obs = None
    for fid in range(1, 12):
        kw = {}
        if fid == 6:
            kw["anchors"] = _sheet(1, z_m=50.0)
        obs = trk.update(_per(fid, cars=[_car(cy=500 + 5 * fid)]),
                         frame_age_ms=10.0, stage=1, **kw)
    m = _m(obs)
    assert m.source is DistanceSource.ANCHOR
    assert m.v_close_mps is None          # 估计窗未开
    assert m.bridged is True
    assert m.t_meet_s is not None and m.t_meet_s > 0.0


# ---------- 迟滞：换侧须驻留，v_close 回归不立刻翻 ----------

def test_bridge_hysteresis_stay_and_exit():
    """丢框（TRACK_LOST，z=None）→ 进桥（原因=TRACK_LOST）→ 复匹配：v_close
    在场但驻留未满（1<2）拍仍报桥接值（防双源翻脸），驻留满换回米制侧。"""
    trk = Tracker(bridge_enabled=True, bridge_stay=2)
    obs = None
    for fid in range(1, 18):
        obs = trk.update(_per(fid, cars=[_car(cy=500 + 5 * fid)]),
                         frame_age_ms=10.0, stage=1)
    assert _m(obs).bridged is True        # 无锚车已进桥（fid 率已预热）
    # 锚点+区间成形 → v_close 发布 → 驻留已满，换回米制
    obs = trk.update(_per(18, cars=[_car(cy=500 + 5 * 18)]),
                     frame_age_ms=10.0, stage=1,
                     anchors=_sheet(6, z_m=50.0))
    obs = trk.update(_per(23, cars=[_car(cy=500 + 5 * 23)]),
                     frame_age_ms=10.0, stage=1,
                     anchors=_sheet(18, z_m=44.0))
    m = _m(obs)
    assert m.v_close_mps is not None and not m.bridged
    for fid in range(24, 28):                 # 中间帧：米制侧驻留建立
        obs = trk.update(_per(fid, cars=[_car(cy=500 + 5 * fid)]),
                         frame_age_ms=10.0, stage=1)
    # 丢框：z=None → 进桥（原因=TRACK_LOST），桥接值出场
    obs = trk.update(_per(28), frame_age_ms=10.0, stage=1)
    m = _m(obs)
    assert m.source is DistanceSource.UNKNOWN
    assert m.bridged is True and m.bridge_reason == "TRACK_LOST"
    # 复匹配：v_close 在场（保锚语义）但驻留未满（1<2）→ 仍报桥接值
    obs = trk.update(_per(29, cars=[_car(cy=500 + 5 * 29)]),
                     frame_age_ms=10.0, stage=1)
    m = _m(obs)
    assert m.v_close_mps is not None
    assert m.bridged is True              # 驻留未满：仍桥接（防翻脸）
    # 再一拍驻留满 → 换回米制
    obs = trk.update(_per(30, cars=[_car(cy=500 + 5 * 30)]),
                     frame_age_ms=10.0, stage=1)
    m = _m(obs)
    assert m.bridged is False and m.v_close_mps is not None


# ---------- 不确定度消费面（corridor_reach） ----------

_BANDS = [CorridorBandSpan(z0=0.0, z1=3.0, left=-1.7, right=1.7)]
_COIN = CoinSource(x_m=0.0, observed_count=3.0, conf=0.9, t_miss_s=1.5)


def test_bridge_threat_downweight_spares_crash_gate():
    """闸关（weight=None）：桥接无差别，t=0.7<crash_t 判碰撞；闸开：桥接车
    威胁时间放大 1/weight（0.5→×2），同一辆车不再判碰撞。"""
    p_off = ReachParams(need_of_dx_m=lambda dx: 0.3 * dx)
    p_on = ReachParams(need_of_dx_m=lambda dx: 0.3 * dx,
                       bridge_weight=0.5, bridge_margin_add_s=0.15)
    plain = [CarSource(x_m=0.0, t_meet_s=0.7)]
    bridged = [CarSource(x_m=0.0, t_meet_s=0.7, bridged=True)]
    rep_off = evaluate(_BANDS, [_COIN], plain, 0.0, p_off)
    rep_on = evaluate(_BANDS, [_COIN], bridged, 0.0, p_on)
    assert min(c.score for c in rep_off.candidates) <= -p_off.crash_penalty
    assert all(c.score > -p_on.crash_penalty for c in rep_on.candidates)


def test_bridge_threat_downweight_gated_off_for_plain_car():
    """非桥接车不吃降权（bridged=False 原值逐位）——闸开着也不变。"""
    p_on = ReachParams(need_of_dx_m=lambda dx: 0.3 * dx,
                       bridge_weight=0.5, bridge_margin_add_s=0.15)
    plain = [CarSource(x_m=0.0, t_meet_s=0.7)]
    rep = evaluate(_BANDS, [_COIN], plain, 0.0, p_on)
    assert min(c.score for c in rep.candidates) <= -p_on.crash_penalty
