# -*- coding: utf-8 -*-
"""存在性占用影子（只读观测）的回归锁。

覆盖：近场建占、距离失效保持、横向滞回、离道不建占、横向未知的 8m
保守建占、500ms 丢失兜底、多目标释放隔离。
"""
from maaracing_master.plugins.speedrush.presence_occupancy import (
    OccObs, PresenceOccupancy)
from maaracing_master.plugins.speedrush.config import load_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.selection import SelectionReport

LW = 3.38


def _obs(tid=1, h=200.0, cy=500.0, x=0.2, m=2.0, fresh=True):
    return OccObs(tid=tid, h=h, cy=cy, x=x, m=m, fresh=fresh)


def test_near_metric_holds_when_distance_expires():
    occ = PresenceOccupancy(LW)
    assert occ.update(0.0, [_obs(m=2.0)], 0.0, set())["ids"] == [1]
    # 距离断供：m=None，但 2D 框仍在近场，占用不能消失
    snap = occ.update(0.1, [_obs(m=None)], 0.0, set())
    assert snap is not None and snap["ids"] == [1]
    # pass 事件是独立证据：只释放该目标
    assert occ.update(0.2, [_obs(m=None)], 0.0, {1}) is None


def test_lateral_hysteresis():
    occ = PresenceOccupancy(LW)
    occ.update(0.0, [_obs()], 0.0, set())
    # 1.8m：滞回带内，保持
    for i in range(1, 4):
        assert occ.update(i * 0.05, [_obs(x=1.8 / LW)], 0.0,
                          set()) is not None
    # 2.1m 第一拍：还不够
    assert occ.update(0.20, [_obs(x=2.1 / LW)], 0.0, set()) is not None
    # 第二拍新鲜 >2.0m：释放
    assert occ.update(0.25, [_obs(x=2.1 / LW)], 0.0, set()) is None


def test_out_of_lane_does_not_acquire():
    occ = PresenceOccupancy(LW)
    assert occ.update(0.0, [_obs(x=1.0)], 0.0, set()) is None


def test_near_metric_requires_near_box_bottom():
    """远处水平线小框 + 旧的小 m：不能靠 m 单独建占。"""
    occ = PresenceOccupancy(LW)
    assert occ.update(0.0, [_obs(h=20.0, cy=330.0, m=2.0)], 0.0,
                      set()) is None


def test_unknown_lateral_only_with_very_near_box():
    occ = PresenceOccupancy(LW)
    # 落地行 600px ≈ 8m 内：横向未知也保守建占
    assert occ.update(0.0, [_obs(x=None, cy=500.0)], 0.0,
                      set()) is not None
    # 落地行 380px：约 20m，横向不可归，不建占
    occ2 = PresenceOccupancy(LW)
    assert occ2.update(0.0, [_obs(x=None, h=100.0, cy=330.0)], 0.0,
                       set()) is None


def test_lost_release_is_bounded():
    occ = PresenceOccupancy(LW)
    occ.update(0.0, [_obs()], 0.0, set())
    occ.update(0.10, [_obs()], 0.0, set())
    # 最后新鲜后 400ms：仍占
    assert occ.update(0.50, [], 0.0, set()) is not None
    # 550ms：有界丢失释放
    assert occ.update(0.65, [], 0.0, set()) is None


def test_release_one_target_keeps_another():
    occ = PresenceOccupancy(LW)
    snap = occ.update(0.0, [_obs(1), _obs(2, x=-0.2)], 0.0, set())
    assert snap["ids"] == [1, 2]
    snap = occ.update(0.1, [_obs(1), _obs(2, x=-0.2)], 0.0, {1})
    assert snap is not None and snap["ids"] == [2]


def test_occ_snap_veto_only_when_selection_stays_in_blocked_lane():
    eng = DecisionEngine(load_decision())
    eng._occ_snap = {"blocked": True, "ids": [1], "m": {1: 2.0},
                     "modes": {1: "2d"}, "since_ms": 100}
    eng._sel_ego_m = 0.0
    eng._sel_report = SelectionReport(4, (), 0.2, "center", True, False)
    assert eng.occ_snap()["veto"] is True
    # 选择已经横移到旁边 2m：影子仍在，但不再判本拍选择落在被占车道
    eng._sel_report = SelectionReport(2, (), 2.0, "pass", True, True)
    assert eng.occ_snap()["veto"] is False
