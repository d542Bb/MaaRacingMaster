# -*- coding: utf-8 -*-
"""第 4 期观测面：开度/拥挤度量与 60 m 锚的回归锁。"""
from __future__ import annotations

from types import SimpleNamespace

from maaracing_master.plugins.speedrush.anchor import (
    AnchorTracker, Candidate)
from maaracing_master.plugins.speedrush.openness import assess


def _scene(width_lane=None, lane_w=3.5):
    return SimpleNamespace(
        lane_w_m=lane_w,
        road=SimpleNamespace(
            width_m=None if width_lane is None else width_lane * lane_w))


def _car(z=None, t=None):
    return SimpleNamespace(z_m=z, t_arrive_s=t)


def test_openness_levels():
    assert assess(_scene(2.6), []).level == "open"
    assert assess(_scene(2.0), []).level == "crowded"
    assert assess(_scene(2.3), []).level == "normal"
    # 两辆 2s 内到站 = 拥挤，与宽度无关
    assert assess(_scene(3.0), [_car(t=1.0), _car(t=1.8)]).level == "crowded"
    # 60m 外且无到站时间的不算近场车
    o = assess(_scene(3.0), [_car(z=80.0)])
    assert o.cars_near == 0 and o.level == "open"
    assert assess(_scene(None), []).level == "unknown"


def _c(i, x=0.0, t=2.0, kind="coin", conf=0.9):
    return Candidate(kind, i, x, t, conf)


def test_anchor_switches_after_confirm_and_holds():
    a = AnchorTracker(confirm_ticks=3, hysteresis_lane=1.0, hold_s=1.0)
    assert a.step([_c(1)], 0.05).id == 1        # 首个锚立即建立
    # 最短持有 1s：新锚确认了也不换，先记 hold
    for _ in range(10):
        assert a.step([_c(2, x=2.0)], 0.05).id == 1
        assert a.why == "hold"
    for _ in range(12):                          # 1s 后且确认已满 → 切换
        out = a.step([_c(2, x=2.0)], 0.05)
        if out.id == 2:
            break
    assert out.id == 2
    # 刚切换后 1s 内不再换
    assert a.step([_c(3, x=-2.0)], 0.05).id == 2
    assert a.step([_c(3, x=-2.0)], 0.05).id == 2


def test_anchor_hysteresis_keeps_nearby_element():
    a = AnchorTracker(confirm_ticks=3, hysteresis_lane=1.0, hold_s=0.0)
    a.step([_c(1, x=0.0)], 0.05)
    out = a.step([_c(2, x=0.8)], 0.05)
    assert out.id == 1 and a.why == "keep"       # 迟滞内不换身份


def test_anchor_releases_without_candidates():
    a = AnchorTracker()
    a.step([_c(1)], 0.05)
    assert a.step([], 0.05) is None
    assert a.why == "none"


def test_anchor_filters_time_and_offset():
    a = AnchorTracker()
    assert a.step([_c(1, t=7.0)], 0.05) is None
    assert a.step([_c(2, t=None)], 0.05) is None
    assert a.step([_c(3, x=3.0)], 0.05) is None
    assert a.step([_c(4, x=2.0, t=1.5)], 0.05).id == 4


def test_horizon_dwell_and_step_limits():
    from maaracing_master.plugins.speedrush.horizon import HorizonGovernor
    h = HorizonGovernor()
    assert h.step("open", 0.05).current_s == 3.0
    n = 0
    while True:                          # 驻留满 0.5 s 才切换
        st = h.step("crowded", 0.05)
        n += 1
        if st.why == "switch":
            break
        assert st.current_s == 3.0
    assert st.current_s == 2.0 and n >= 10
    n = 0
    while True:
        st = h.step("normal", 0.05)
        n += 1
        if st.why == "switch":
            break
    assert st.current_s == 2.5 and n >= 10


def test_trigger_log_records_each_event_once():
    from maaracing_master.plugins.speedrush.triggers import TriggerLog
    t = TriggerLog()
    assert t.step(open_level="open", anchor={"k": "coin", "id": 1},
                  layer=4, map_support=10.0,
                  route_phase="keep", route_ref=0.0) == []
    ev = t.step(open_level="crowded", anchor={"k": "coin", "id": 2},
                layer=3, map_support=16.0,
                route_phase="merging", route_ref=1.0)
    assert set(ev) == {"open", "anchor", "layer", "map", "route"}
    assert t.step(open_level="crowded", anchor={"k": "coin", "id": 2},
                  layer=3, map_support=16.0,
                  route_phase="merging", route_ref=1.0) == []
