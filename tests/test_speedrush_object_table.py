# -*- coding: utf-8 -*-
"""动态对象表回归锁（第 1 期）：身份键、safety_ok 四态、超龄保留/退役、
币组引用成员、新 id 不继承旧状态。"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.object_table import (
    SAFETY_NO_LATERAL, SAFETY_NOT_CAR, SAFETY_OK, SAFETY_STALE, ObjectTable)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DistanceSource, FarTarget, MetricCarState, PerceptionHealth,
    TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_CAR, KIND_COIN)

FPS = 20.0


def _obs(fid, *, targets=(), far=(), groups=(), metric=(), stage=1):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=stage,
        health=PerceptionHealth(True, True, True, bool(targets or far), False),
        boundary=None, targets=tuple(targets), far_targets=tuple(far),
        coin_groups=tuple(groups), metric_cars=tuple(metric),
        fid_rate_hz=FPS)


def _car(tid, x, *, kind=KIND_CAR, cy=500, last=1, first=1, rel=2.0):
    return TrackedTarget(
        id=tid, kind=kind, x_lane=x, x_sigma=0.1, cy=cy, w=30, h=30,
        conf=0.9, rel_approach=rel, first_seen_fid=first,
        last_seen_fid=last, validity_until_fid=last + 15)


def _table():
    return ObjectTable(safety_age_s=0.6, plan_age_s=2.0)


def test_identity_keys_separate_kinds():
    t = _table()
    t.step(obs=_obs(1, targets=(_car(7, 0.1), _car(7, -0.2, kind=KIND_COIN))),
           fps=FPS)
    assert t.track(KIND_CAR, 7) is not None
    assert t.track(KIND_COIN, 7) is not None
    assert t.track(KIND_CAR, 7) is not t.track(KIND_COIN, 7)


def test_safety_ok_fresh_occluded_stale_and_no_lateral():
    t = _table()
    t.step(obs=_obs(1, targets=(_car(1, 0.2), _car(3, 0.0, kind=KIND_COIN))),
           fps=FPS)
    assert t.track(KIND_CAR, 1).safety_ok is True
    assert t.track(KIND_CAR, 1).safety_why == SAFETY_OK
    assert t.track(KIND_COIN, 3).safety_ok is False
    assert t.track(KIND_COIN, 3).safety_why == SAFETY_NOT_CAR
    # 宽限内的短暂遮挡：仍在安全龄内，位置是最后一次实测（不外推）
    t.step(obs=_obs(7, targets=(_car(1, 0.2, last=1),)), fps=FPS)
    e = t.track(KIND_CAR, 1)
    assert e.seen is False and e.age_ms == pytest.approx(300.0)
    assert e.safety_ok is True
    # 超过安全龄：仍留在表里（plan_age 内），但不再进安全判断
    t.step(obs=_obs(14, targets=(_car(1, 0.2, last=1),)), fps=FPS)
    e = t.track(KIND_CAR, 1)
    assert e.age_ms == pytest.approx(650.0)
    assert e.safety_ok is False and e.safety_why == SAFETY_STALE
    # 域外、无米制解：没有可用横位，不是安全对象
    far = FarTarget(id=9, kind=KIND_CAR, lane_side=1, cy=300, conf=0.6,
                    h=20, last_x_lane=None, first_seen_fid=14,
                    last_seen_fid=14, validity_until_fid=29)
    t.step(obs=_obs(14, targets=(_car(1, 0.2, last=1),), far=(far,)), fps=FPS)
    e = t.track(KIND_CAR, 9)
    assert e.safety_ok is False and e.safety_why == SAFETY_NO_LATERAL


def test_metric_only_far_car_is_safety_object():
    """域外车有米制横位时，今天的安全链就吃它；safety_ok 不得因像素缺席而丢。"""
    t = _table()
    far = FarTarget(id=5, kind=KIND_CAR, lane_side=1, cy=320, conf=0.7,
                    h=24, last_x_lane=None, first_seen_fid=1,
                    last_seen_fid=1, validity_until_fid=16)
    m = MetricCarState(target_id=5, source=DistanceSource.ANCHOR, z_m=40.0,
                       x_m=1.8,
                       v_close_mps=None, t_meet_s=None, anchor_age_ms=0.0)
    t.step(obs=_obs(1, far=(far,), metric=(m,)), metric_cars=(m,), fps=FPS)
    e = t.track(KIND_CAR, 5)
    assert e.x_usable is True and e.safety_ok is True


def test_retention_until_plan_age_then_drop():
    t = _table()
    t.step(obs=_obs(1, targets=(_car(1, 0.2),)), fps=FPS)
    t.step(obs=_obs(31, targets=()), fps=FPS)      # 1.5 s 未见：仍在表内
    e = t.track(KIND_CAR, 1)
    assert e is not None and e.seen is False and e.safety_ok is False
    t.step(obs=_obs(43, targets=()), fps=FPS)      # 2.1 s：超过计划视界，退役
    assert t.track(KIND_CAR, 1) is None


def test_new_id_at_same_position_does_not_inherit():
    t = _table()
    t.step(obs=_obs(1, targets=(_car(1, 0.2),)), fps=FPS)
    t.step(obs=_obs(5, targets=(_car(2, 0.2, first=5, last=5),)), fps=FPS)
    old, new = t.track(KIND_CAR, 1), t.track(KIND_CAR, 2)
    assert old is not None and new is not None
    assert old.first_seen_fid == 1 and old.last_seen_fid == 1
    assert new.first_seen_fid == 5 and new.age_ms == 0.0
    # 上游不再输出 = 跟踪器已判退役：旧条目留着但不能再进安全判断
    assert old.seen is False and old.safety_ok is False
    assert new.safety_ok is True


def test_coin_group_is_its_own_key_space():
    t = _table()
    g = CoinGroup(
        group_id=4, member_ids=(10, 11), observed_count=2,
        estimated_count=2, x_center=0.4, x_span=0.2, cy_min=470,
        cy_max=480, conf_min=0.8, partial_observation=False,
        first_seen_fid=1, last_seen_fid=1, validity_until_fid=25, lead_id=11)
    t.step(obs=_obs(1, groups=(g,)), fps=FPS)
    assert t.groups[0].group_id == 4
    assert t.groups[0].member_ids == (10, 11)
    assert t.groups[0].lead_id == 11
    assert not any(k[0] == "cg" for k in
                   {(e.kind, e.id) for e in t.tracks})
    assert t.track(KIND_COIN, 4) is None


def test_safety_cars_only_returns_ok_cars():
    t = _table()
    t.step(obs=_obs(1, targets=(_car(1, 0.2), _car(2, -0.3, kind=KIND_COIN))),
           fps=FPS)
    t.step(obs=_obs(14, targets=(_car(1, 0.2, last=1),
                                 _car(2, -0.3, kind=KIND_COIN, last=1))),
           fps=FPS)
    assert [e.id for e in t.safety_cars] == []


def test_trace_shape():
    t = _table()
    t.step(obs=_obs(1, targets=(_car(1, 0.2),)), fps=FPS)
    tr = t.as_trace()
    assert tr["n"] == 1 and tr["nsafe"] == 1
    assert len(tr["t"][0]) == 11 and len(tr["g"]) == 0
    assert tr["t"][0][0] == KIND_CAR and tr["t"][0][1] == 1


# ---------- 引擎接线：trace 可见 + 阶段切换清表 ----------

def _corridor():
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand)
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(-7.0, "measured"),
                     BandBoundary(7.0, "measured"))
        for i in range(9))
    return Corridor(bands, 0.0, 14.0 / 3.5, "pair", "ok")


def _engine():
    from maaracing_master.plugins.speedrush import DECISION_FILE
    from maaracing_master.plugins.speedrush.config import _read_decision
    from maaracing_master.plugins.speedrush.decision import DecisionEngine
    return DecisionEngine(_read_decision(DECISION_FILE))


def test_engine_trace_and_stage_reset():
    eng = _engine()
    eng.update(_obs(1, targets=(_car(42, 0.3),)), 0.05,
               executed_lane=0.0, corridor=_corridor())
    tr = eng.object_table_trace()
    row = [r for r in tr["t"] if r[0] == KIND_CAR and r[1] == 42][0]
    assert row[6] is True and row[7] is True and row[8] == SAFETY_OK
    # 下一阶段：上游 id 空间已换（track id 不复用），表也必须换账
    eng.update(_obs(2, stage=2), 0.05, executed_lane=0.0, corridor=_corridor())
    assert eng.object_table_trace()["n"] == 0


def test_engine_keeps_occluded_car_past_scene_age():
    """表比 scene 多留：超龄车仍可查，但 safety_ok 已失效（安全隔离的前提）。"""
    eng = _engine()
    eng.update(_obs(1, targets=(_car(7, 0.2),)), 0.05,
               executed_lane=0.0, corridor=_corridor())
    eng.update(_obs(20, targets=()), 0.05,
               executed_lane=0.0, corridor=_corridor())
    e = eng._objects.track(KIND_CAR, 7)
    assert e is not None and e.age_ms == pytest.approx(950.0)
    assert e.safety_ok is False and e.safety_why == SAFETY_STALE


def test_unknown_fps_does_not_retain_retired_track():
    """帧率未预热时算不出龄：宁可立即退役，也不带未知年龄进规划。"""
    t = _table()
    t.step(obs=_obs(1, targets=(_car(1, 0.2),)), fps=FPS)
    t.step(obs=_obs(2), fps=None)
    assert t.track(KIND_CAR, 1) is None


def test_metric_anchor_age_is_the_safety_age():
    """米制车的横位/纵深来自锚点：安全龄按锚龄截断（与场景现有硬删同源），
    观测再新鲜也不能把 0.6 s 外的锚带进安全判断。"""
    t = _table()
    car = _car(5, 0.3, cy=320)
    old_anchor = MetricCarState(
        target_id=5, source=DistanceSource.RESCALED_ANCHOR, z_m=40.0,
        x_m=1.8, v_close_mps=None, t_meet_s=None, anchor_age_ms=700.0)
    t.step(obs=_obs(1, targets=(car,)), metric_cars=(old_anchor,), fps=FPS)
    e = t.track(KIND_CAR, 5)
    assert e.age_ms == 0.0 and e.src_age_ms == 700.0
    assert e.safety_ok is False and e.safety_why == SAFETY_STALE
    fresh = MetricCarState(
        target_id=5, source=DistanceSource.RESCALED_ANCHOR, z_m=40.0,
        x_m=1.8, v_close_mps=None, t_meet_s=None, anchor_age_ms=100.0)
    t.step(obs=_obs(2, targets=(_car(5, 0.3, cy=320, last=2),)),
           metric_cars=(fresh,), fps=FPS)
    assert t.track(KIND_CAR, 5).safety_ok is True


def test_box_size_metric_has_no_anchor_age():
    """框高粗距离没有锚点：场景按 0 龄收它，表也不得按锚龄误杀。"""
    t = _table()
    car = _car(6, 0.3, cy=300)
    box = MetricCarState(
        target_id=6, source=DistanceSource.BOX_SIZE, z_m=30.0, x_m=None,
        v_close_mps=None, t_meet_s=None, anchor_age_ms=None)
    t.step(obs=_obs(1, targets=(car,)), metric_cars=(box,), fps=FPS)
    e = t.track(KIND_CAR, 6)
    assert e.src == "metric_coarse" and e.src_age_ms == 0.0
    assert e.safety_ok is True


def test_engine_scene_retains_past_safety_age():
    """1c 引擎接线：0.9 s 的车仍在场景里（规划价值窗=计划视界），
    但对象表与场景都标不安全；安全消费点看不到它。"""
    from maaracing_master.plugins.speedrush.traffic import CarView
    eng = _engine()
    view = CarView(id=9, x_lane=0.3, cy=500, rel_approach=2.0,
                   d_min=0.3, age_ticks=19, last_seen_fid=1)
    eng.update(_obs(19, targets=(_car(9, 0.3, last=1),)), 0.05,
               executed_lane=0.0, corridor=_corridor(),
               traffic=((view,), ()))
    obj = [o for o in eng.scene.objs if o.kind == KIND_CAR][0]
    assert obj.age_ms == pytest.approx(900.0)
    assert obj.safety_ok is False
    assert eng.object_table_trace()["nsafe"] == 0
