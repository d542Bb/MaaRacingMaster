# -*- coding: utf-8 -*-
"""逐箱墙假设跟踪（wall_track.WallTrack）的回归锁。

合成用例锁语义：同箱紧容差重复攒置信、超容差重置不拼接、无观测衰减遮挡、
遮挡标记必须有历史证据、fid 倒退/重复跳过。数据锚点（83 包序列）锁跨帧
行为：直路场景远墙攒成一条高置信假设。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from maaracing_master.plugins.speedrush.wall_track import (
    CONF_CAP, OCCLUDED, TOL_M, WallTrack)
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()


@dataclass
class _StubReading:
    """鸭子同构 DepthRoadReading：只带 edge_pts。"""
    edge_pts: list = field(default_factory=list)


def test_wall_track_accumulates_confidence():
    """同箱观测在容差内重复：置信累积（1→2→…），位置随新观测走。"""
    t = WallTrack(CAL)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=100)
    t.update(_StubReading([(-1, 14.0, -10.6)]), fid=101)
    bins = t.bins(-1)
    assert len(bins) == 1
    zc, pos, conf, state, last_fid = bins[0]
    assert zc == 14.0 and abs(pos + 10.6) < 1e-6
    assert conf == 2.0 and state == "measured" and last_fid == 101


def test_wall_track_resets_on_jump():
    """观测跳变超容差：槽位重置（conf 归 1、位置换新）——不拼接错误轨迹。"""
    t = WallTrack(CAL)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=100)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=101)
    assert t.bins(-1)[0][2] == 2.0
    t.update(_StubReading([(-1, 14.0, -10.7 - TOL_M - 0.5)]), fid=102)
    zc, pos, conf, state, last_fid = t.bins(-1)[0]
    assert conf == 1.0, "跳变后必须重置，不许沿用旧置信"
    assert abs(pos + 10.7 + TOL_M + 0.5) < 1e-6


def test_wall_track_marks_occluded_with_history():
    """无观测的箱：有历史置信（攒到门槛）标 occluded（位置沿用），单帧孤证作废。"""
    t = WallTrack(CAL)
    t.update(_StubReading([(-1, 14.0, -10.7), (-1, 8.0, -5.0)]), fid=100)
    t.update(_StubReading([(-1, 14.0, -10.7), (-1, 8.0, -5.0)]), fid=101)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=102)   # zc=8 本帧被挡
    d = {zc: (pos, conf, st) for zc, pos, conf, st, _f in t.bins(-1)}
    assert d[14.0][2] == "measured"
    assert d[8.0][2] == OCCLUDED, "攒到门槛的被挡箱应标遮挡"
    assert abs(d[8.0][0] + 5.0) < 1e-6, "遮挡段位置沿用上一帧"
    # 单帧观测（conf=1）直接缺席：1.0 < OCCL_MIN_CONF → 不标遮挡
    t2 = WallTrack(CAL)
    t2.update(_StubReading([(-1, 6.0, -3.0)]), fid=100)
    t2.update(_StubReading([]), fid=101)
    assert t2.bins(-1) == [], "单帧孤证不许标遮挡"


def test_wall_track_confidence_decays_to_death():
    """持续无观测：置信衰减到底失效——墙不假装自己还在场。"""
    t = WallTrack(CAL)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=100)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=101)   # conf=2
    fid = 102
    while t.bins(-1):
        t.update(_StubReading([]), fid=fid)
        fid += 1
    assert not t.bins(-1), "置信衰减到底必须失效"


def test_wall_track_fid_repeats_and_rewinds_skipped():
    """fid 重复（同帧重发）与倒退：跳过，不重复进账。"""
    t = WallTrack(CAL)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=100)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=100)   # 同 fid 重发
    assert t.bins(-1)[0][2] == 1.0, "同 fid 不许重复攒置信"
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=99)    # 倒退
    assert t.bins(-1)[0][2] == 1.0


def test_wall_track_reobserve_unoccludes():
    """遮挡墙重新可见：状态回 measured（重观测=重新在场），置信续攒。"""
    t = WallTrack(CAL)
    t.update(_StubReading([(-1, 14.0, -10.7), (-1, 8.0, -5.0)]), fid=100)
    t.update(_StubReading([(-1, 14.0, -10.7), (-1, 8.0, -5.0)]), fid=101)
    t.update(_StubReading([(-1, 14.0, -10.7)]), fid=102)   # zc=8 被挡 → occluded
    d = {zc: st for zc, _p, _c, st, _f in t.bins(-1)}
    assert d[8.0] == OCCLUDED
    t.update(_StubReading([(-1, 8.0, -5.0)]), fid=103)     # 车挪开：重新可见
    d = {zc: (conf, st) for zc, _p, conf, st, _f in t.bins(-1)}
    assert d[8.0][1] == "measured", "重观测必须解除遮挡"
    assert d[8.0][0] > 1.0, "遮挡期衰减后重观测续攒置信"


def test_wall_track_confidence_capped():
    """置信封顶：连续一致攒到 CONF_CAP 为止（场景切换的重置不被淹没）。"""
    t = WallTrack(CAL)
    for fid in range(100, 120):
        t.update(_StubReading([(-1, 14.0, -10.7)]), fid=fid)
    assert t.bins(-1)[0][2] == CONF_CAP
