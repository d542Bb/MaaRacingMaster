# -*- coding: utf-8 -*-
"""R5 §10 远场粗源（decision._reach_bonus_far_sources）的回归锁。

锁面：x_lane_of 同一公式真源读粗横位（**框底边**=cy+h/2 地面触点代理——
平台是抬升物体，框中心更贴地平线，X/H 按地面投影会系统性放大横位）、
夹进走廊 band edges 并集（误差被路宽兜住）、到站走 car_t_meet_s 同一仪器
链、每源宽容差 bonus_far_tol_m。门同式：年龄门槛/不接近不收/地平线防御/
粗读有限。闸语义（bonus_value 总闸）锁在 corridor_reach_bonus，本文件锁
源构建本身。
行为收益不在此锁（那是实机 A/B 的验收面）。

场景算式（真源 gate0：vpx=640.7、y_h=324.2、v_ego=716、a_x=0.587、
lane_w=3.38）：远台 cy=330 h=60 → 底边 360，d=35.8；cx=700 →
((700−640.7)/35.8 − (640−640.7)/391.8)×0.587 ≈ 0.97 道 → 夹进 ±6m/3.38
=±1.78 道内不触发；x_m=(0.97+0)×3.38≈3.29m。
"""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.corridor import (
    BandBoundary, Corridor, CorridorBand)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.tracking import (
    FarTarget, PerceptionHealth, WorldObservation)

_CFG = _read_decision(DECISION_FILE)
# 走廊收益场闸开（仅 enabled——shadow 面即可喂 _reach_bonus_far_sources），
# 其余段沿真源：本文件锁远场粗源构建，不继承部署态其他翻转。
CFG = replace(_CFG, corridor_reach=replace(
    _CFG.corridor_reach, enabled=True, control=False, bonus_value=True))

LANE_W = 3.38


def _engine() -> DecisionEngine:
    eng = DecisionEngine(CFG)
    eng._corridor = _corridor()
    eng.scorer.fid_hz = 50.0            # 采集帧率估计（live 由 obs 下发）
    return eng


def _corridor(left=-6.0, right=6.0) -> Corridor:
    band = CorridorBand(
        0.0, 3.0,
        left=BandBoundary(x_m=left, src="measured", conf=1.0),
        right=BandBoundary(x_m=right, src="measured", conf=1.0))
    return Corridor(bands=(band,), center_lane=0.0, width_lane=3.5,
                    src="wall", plane_health="ok", far_trend=None)


def _obs(*, fid=100, fars=()):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 20_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(True, True, True, True, False),
        boundary=None, targets=(), far_targets=tuple(fars), coin_groups=())


def _far(*, fid=100, cx=700, cy=330, h=60, rel=8.0, kind="bonus",
         first_seen=None, id=7):
    return FarTarget(id=id, kind=kind, lane_side=1, cy=cy, conf=0.9, h=h,
                     last_x_lane=None,
                     first_seen_fid=first_seen if first_seen is not None
                     else fid - 10,
                     last_seen_fid=fid, validity_until_fid=fid + 8,
                     cx=cx, rel_approach=rel)


# ---------- 生效：远场粗源入账 ----------

def test_far_bonus_source_built():
    """远台奖励车（横位缺位）→ BonusSource：粗横位≈0.97 道（框底边）、
    夹进走廊内、到站有限、每源宽容差 3.4。"""
    eng = _engine()
    out = eng._reach_bonus_far_sources(_obs(fars=(_far(),)), 0.0)
    assert len(out) == 1
    s = out[0]
    assert s.x_m == pytest.approx(3.29, abs=0.05)   # 算式见文件头
    assert math.isfinite(s.t_miss_s) and s.t_miss_s > 0.0
    assert s.tol_m == pytest.approx(3.4)
    assert s.conf == pytest.approx(0.9)


def test_non_bonus_far_target_ignored():
    """远台车/币不入奖励车粗源（kind 闸同近场源）。"""
    eng = _engine()
    out = eng._reach_bonus_far_sources(
        _obs(fars=(_far(kind="car", id=8), _far(kind="coin", id=9))), 0.0)
    assert out == []


def test_not_approaching_not_collected():
    """rel_approach≤0（远离/静止 EMA 未热）不收——同近场源「不接近不收」。"""
    eng = _engine()
    out = eng._reach_bonus_far_sources(_obs(fars=(_far(rel=0.0),)), 0.0)
    assert out == []
    out2 = eng._reach_bonus_far_sources(_obs(fars=(_far(rel=-3.0),)), 0.0)
    assert out2 == []


def test_age_gate_same_as_near_source():
    """年龄门槛同近场源：first_seen 距今 < min_obs_ticks（3）不收——单帧噪声。"""
    eng = _engine()
    out = eng._reach_bonus_far_sources(
        _obs(fars=(_far(first_seen=99),)), 0.0)      # 距今 1 拍 < 3
    assert out == []


def test_corridor_clamp_bounds_coarse_read():
    """粗读夹进走廊已知路缘：单目在近地平线噪声放大（小 d 大横位），路宽
    兜住不再发散——粗读 5 道也只到走廊边。"""
    eng = _engine()
    # cx 拉远：底边 d=35.8 不变，(cx−vpx)/d 大 → 粗读超走廊
    out = eng._reach_bonus_far_sources(_obs(fars=(_far(cx=1000),)), 0.0)
    assert len(out) == 1
    assert out[0].x_m == pytest.approx(6.0, abs=0.01)   # 夹到右缘 6m
    # 走廊全 unknown（无 edges）：不夹，仅要求有限
    eng2 = _engine()
    eng2._corridor = Corridor(bands=(), center_lane=0.0, width_lane=None,
                              src="none", plane_health="none", far_trend=None)
    out2 = eng2._reach_bonus_far_sources(_obs(fars=(_far(cx=1000),)), 0.0)
    assert len(out2) == 1
    assert math.isfinite(out2[0].x_m)


def test_executed_lane_offset_applied():
    """executed 平移同近场源：x_m=(粗读+executed)×lane_w（自车系→路心系）。"""
    eng = _engine()
    out = eng._reach_bonus_far_sources(_obs(), 1.0)
    assert out == []                     # 无远台：空
    out2 = eng._reach_bonus_far_sources(_obs(fars=(_far(),)), 1.0)
    assert len(out2) == 1
    assert out2[0].x_m == pytest.approx(3.29 + LANE_W, abs=0.05)


def test_raised_platform_uses_box_bottom():
    """框底边=地面触点代理：同一远台框变矮（h 小、中心更贴地平线）横位
    更被放大——底边代理对抬升物体稳健（粗横位随 d 增大而收敛）。"""
    eng = _engine()
    tall = eng._reach_bonus_far_sources(_obs(fars=(_far(cy=330, h=60),)), 0.0)
    flat = eng._reach_bonus_far_sources(_obs(fars=(_far(cy=330, h=12),)), 0.0)
    assert len(tall) == 1 and len(flat) == 1
    # 矮框底边 d 小 → X/H 大 → |x_m| 更大（更被放大）
    assert abs(flat[0].x_m) > abs(tall[0].x_m)
