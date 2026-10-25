# -*- coding: utf-8 -*-
"""路线承诺层（2026-10-23 V1，2026-10-24 实机两局后收敛）的回归锁。

锁的是**实际会生效的规则**：现任道连续确认被截才换、只换到确实更宽的道、
并线中只认完成与撤销、撤销后冷却期禁入刚放弃的道、参考系换纪/参考道失去几何
时重建到**可行道**。场景数值按 decision.json 起值反推（窗 24 m、门槛 4 m、
确认 3 拍），参数与场景是一对契约面，改数值会红。

原先的「普通换道 + 承诺期 + 整段并线时间窗」三套判据已删除：24 m 窗径下
普通换道结构上不可达，实机一局 327 拍卡在承诺期而其中没有一次真正的决定。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.coarse import CoarseLane, CoarseReport
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.corridor import (
    BandBoundary, Corridor, CorridorBand)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.route import (
    PHASE_KEEP, PHASE_MERGING, RouteParams, init_state, tick_route)
from maaracing_master.plugins.speedrush.tracking import (
    PerceptionHealth, WorldObservation)

LW = 3.38
DT = 0.05
CFG = _read_decision(DECISION_FILE)
# 与 DecisionEngine 同一套装配：尺度/速度类数值只有既有真源
P = RouteParams(
    switch_gain_m=CFG.coarse.switch_margin_m,
    proxy_speed_mps=CFG.coarse.range_speed_mps,
    urgent_confirm_ticks=CFG.route.urgent_confirm_ticks,
    cancel_unsafe_ticks=CFG.route.cancel_unsafe_ticks,
    cancel_cooldown_s=CFG.route.cancel_cooldown_s,
    finish_tol_lane=CFG.selection.center_band_lane,
    finish_v_tol_lane_s=CFG.route.finish_v_tol_lane_s,
    merge_max_s=CFG.route.merge_max_s)
CLEAR = (24.0, None, False)               # 确认空闲（净空=窗长）
WINDOW_M = CLEAR[0]


# ---------- 原始粗层报告 helper ----------

def _rep(ego=0.0, chosen=0.0, spec=None):
    """五条候选道（±2，与粗层同集合）的原始报告。

    `spec`：{道号: (净空米, 截断来源, 无法确认)}；未列出的道=确认空闲。截断
    来源沿用粗层语义：None=空闲、"car"/"space"=确认被占、"unknown_car"=无法确认。"""
    spec = spec or {}
    lanes = []
    for k in (-2.0, -1.0, 0.0, 1.0, 2.0):
        free, blocker, unknown = spec.get(k, CLEAR)
        lanes.append(CoarseLane(k, free, 1.0, blocker, unknown))
    return CoarseReport(ego, chosen, 0, WINDOW_M, "keep", tuple(lanes))


def _tick(state, report, epoch=0):
    return tick_route(state, report, P, dt_s=DT, epoch=epoch)


def _first(report, epoch=0):
    """冷启动首拍之后的一拍。"""
    s, _ = _tick(None, report, epoch)
    return _tick(s, report, epoch)


def _confirm(report, epoch=0):
    """冷启动后连续喂同一读数，直到占用确认（urgent_confirm_ticks 拍）。"""
    s, _ = _tick(None, report, epoch)
    for _ in range(P.urgent_confirm_ticks):
        s, v = _tick(s, report, epoch)
    return s, v


def _in_merge(epoch=0):
    """推进成"正在并线"：现任道持续被截，换到 +1（确认空闲；−1 显式判成走不下，
    因为左右同分时按道号取靠左的一侧——与粗层同一套字典序）。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s, _ = _tick(None, _rep(0.0, 0.0), epoch)
    for _ in range(P.urgent_confirm_ticks):
        s, v = _tick(s, _rep(0.0, 0.0, spec), epoch)
    return s, v


# ---------- 逃生换道 ----------

def test_escape_needs_consecutive_confirmation():
    """单帧闪检不起意（帧计数确认），持续占用才换——远端一闪就变道是噪声驱动。"""
    spec = {0.0: (12.0, "car", False)}
    s, v = _first(_rep(0.0, 0.0, spec))
    assert v.verdict == "keep" and v.why == "blocked_unconfirmed"
    assert v.lane_ref == 0.0
    s, v = _confirm(_rep(0.0, 0.0, spec))
    assert v.verdict == "merge_start" and v.why == "urgent"
    assert v.lane_ref == -1.0 and v.evidence_m == pytest.approx(12.0)


def test_escape_requires_a_wider_lane():
    """换过去必须确实更宽：完全空闲直接合格；有车的道要多出一个门槛。

    现任道 20 m 被截，旁边两条只有 22/23 m（差 2/3 m < 门槛 4 m）→ 不动。"""
    spec = {0.0: (20.0, "car", False), 1.0: (22.0, "car", False),
            -1.0: (23.0, "car", False), 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    s, v = _confirm(_rep(0.0, 0.0, spec))
    assert v.verdict == "urgent_no_alt" and v.why == "no_alt"
    assert v.lane_ref == 0.0 and v.evidence_m == pytest.approx(20.0)
    # 旁边变空 → 直接合格（不看门槛）
    spec2 = {0.0: (20.0, "car", False), 1.0: CLEAR,
             -1.0: (23.0, "car", False), 2.0: (0.0, "space", False),
             -2.0: (0.0, "space", False)}
    s, v = _confirm(_rep(0.0, 0.0, spec2))
    assert v.verdict == "merge_start" and v.lane_ref == 1.0


def test_unknown_lane_is_never_a_destination():
    """目标道「无法确认」不能当逃生方向——宁可去一条确认有车但更宽的道。"""
    spec = {0.0: (16.0, "car", False),
            1.0: (WINDOW_M, "unknown_car", True),   # 读数最空但没判据
            -1.0: (22.0, "car", False),
            2.0: (0.0, "space", False), -2.0: (0.0, "space", False)}
    s, v = _confirm(_rep(0.0, 0.0, spec))
    assert v.verdict == "merge_start" and v.lane_ref == -1.0


def test_unknown_current_lane_is_not_an_escape_reason():
    """现任道「无法确认」不单独构成逃生理由：逃生要现任道的**确认**证据。"""
    spec = {0.0: (2.0, "unknown_car", True), 1.0: CLEAR}
    s, v = _confirm(_rep(0.0, 0.0, spec))
    assert v.verdict == "keep" and v.why == "blocked_unconfirmed"
    assert v.lane_ref == 0.0


def test_clear_lane_holds_without_a_reason_to_move():
    """现任道确认空闲：没有理由动（净空全被窗长封顶，主动换道无动机）。"""
    s, v = _first(_rep(0.0, 1.0))
    assert v.verdict == "keep" and v.why == "clear" and v.lane_ref == 0.0


# ---------- 并线生命周期 ----------

def test_merge_finishes_on_position_and_velocity():
    """完成判据=位置进目标道中心带 **且** 横速落稳：只看位置会带着横速"完成"。"""
    s, v = _in_merge()
    assert v.phase == PHASE_MERGING and v.lane_ref == 1.0
    s2, v2 = _tick(s, _rep(0.9, 1.0))              # 横位 0→0.9：横速 18 道/s
    assert v2.verdict == "keep" and v2.why == "merging"
    s3, v3 = _tick(s2, _rep(0.9, 1.0))             # 速率归零、位差 0.1 < 带宽
    assert v3.verdict == "finish" and v3.why == "arrived"
    assert v3.lane_ref == 1.0 and v3.phase == PHASE_KEEP


def test_merge_cancel_bans_the_abandoned_lane_during_cooldown():
    """撤销：目标道连续确认被截 → 回落到实测可达道，并**禁入**刚放弃的道；
    冷却结束后才重新可选。"""
    s, v = _in_merge()
    bad = {1.0: (10.0, "car", False)}
    s, v = _tick(s, _rep(0.5, 1.0, bad))           # 单帧野值不撤
    assert v.verdict == "keep" and v.why == "merging"
    for _ in range(P.cancel_unsafe_ticks - 1):
        s, v = _tick(s, _rep(0.5, 1.0, bad))
    assert v.verdict == "cancel" and v.lane_ref == 0.0
    assert v.blocker == "car" and v.as_trace()["blk"] == "car"
    assert s.ban_lane == 1.0 and v.cooldown_left_s > 0.0
    # 冷却期内：0 道被截、+1 道是唯一更宽的空闲道，但它是刚放弃的那条 → 不动
    spec = {0.0: (12.0, "car", False), 1.0: CLEAR,
            -1.0: (14.0, "car", False), 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    for _ in range(P.urgent_confirm_ticks):
        s, v = _tick(s, _rep(0.0, 0.0, spec))
    assert v.verdict == "urgent_no_alt" and v.lane_ref == 0.0
    # 冷却结束那一拍就会重新评估：同一条道重新可选
    for _ in range(60):
        s, v = _tick(s, _rep(0.0, 0.0, spec))
        if v.verdict == "merge_start":
            break
    assert v.verdict == "merge_start" and v.lane_ref == 1.0


def test_merge_timeout_lands_on_measured_lane():
    """并线超时：车可能停在半道，落回**实测所在道**而不是硬判目标道完成。"""
    s, v = _in_merge()
    for _ in range(200):
        s, v = _tick(s, _rep(0.1, 1.0))            # 卡在 0.1 道，横速 0
        if v.verdict == "timeout":
            break
    assert v.verdict != "finish"                   # 超时不得冒充完成
    assert v.why == "merge_timeout" and v.lane_ref == 0.0
    assert s.phase == PHASE_KEEP


# ---------- 参考系身份 / 参考道几何 ----------

def test_epoch_change_rebuilds_to_drivable_lane():
    """换纪=参考系重基：并线作废、参考道落到实测可达道，定案时钟不被重置。"""
    s, v = _in_merge(epoch=3)
    assert v.phase == PHASE_MERGING
    decided_before = s.decided_t
    s, v = _tick(s, _rep(0.4, 1.0), epoch=4)
    assert v.verdict == "epoch_reset" and v.phase == PHASE_KEEP
    assert v.lane_ref == 0.0 and s.epoch == 4
    assert s.decided_t == decided_before         # 重建不是决定


def test_reference_lane_on_space_rebuilds_to_drivable():
    """参考道被走廊判成走不下（space）：立刻重建到可行道，不钉在空道上。"""
    s, v = _tick(None, _rep(0.0, 0.0), 0)
    s = replace(s, lane_ref=-1.0, lane_from=-1.0)
    s, v = _tick(s, _rep(-0.6, 0.0, {-1.0: (0.0, "space", False)}))
    assert v.verdict == "rebuild" and v.why == "lane_dropped"
    assert v.blocker == "space" and v.as_trace()["blk"] == "space"
    assert v.lane_ref == 0.0


def test_cold_start_holds_centerline_not_nearest_lane():
    """冷启动参考道=路心道（0）：车偏在两道之间时，启动拍不平白压向最近一条道。"""
    s, v = _tick(None, _rep(1.45, 1.0))
    assert v.verdict == "init" and v.lane_ref == 0.0


def test_no_evidence_frame_freezes_decision_not_clock():
    """无路拍：不推进定案、也不拿旧坐标硬撑（needs_init），时钟继续走。"""
    s = init_state(0)
    assert s.lane_ref == 0.0 and s.phase == PHASE_KEEP
    s2, v = tick_route(s, None, P, dt_s=DT, epoch=0)
    assert v.verdict == "needs_init" and v.lane_ref == 0.0
    assert s2.elapsed_s == pytest.approx(s.elapsed_s + DT)
    assert s2.lane_ref == s.lane_ref


# ---------- 唯一控制接口（接线面） ----------

def _corridor(shift_m=0.0):
    """对称走廊；`shift_m` 平移整个分带（改变路线层用的尺子 z=0 中心线）。"""
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(-6.76 + shift_m, "measured"),
                     BandBoundary(6.76 + shift_m, "measured"))
        for i in range(9))
    # 供数门 master 固定在 0：带整体平移才能改变"尺子"（场景 z=0 中心线）；
    # master 跟着带一起动的话，原点会把平移抵消掉，尺子纹丝不动
    return Corridor(bands, 0.0, 13.52 / LW, "pair", "ok")


def _obs(fid):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, False, False),
        boundary=None, targets=(), far_targets=(), coin_groups=(),
        fid_rate_hz=20.0)


TARGET_1_LANE = 0.7 * LW      # L4 带外只去近侧带边：中心 +1 道 → 2.366 m


def test_gate_off_follows_raw_coarse_flip(monkeypatch):
    """边界 A：`route.control=false` 时接控制的道号逐位是粗层自选道，选择目标
    随它移动（今天的行为）；路线照样逐拍定案，只是影子对照面。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    monkeypatch.setattr(dmod, "choose_lane", lambda *a, **k: _rep(0.0, 1.0))
    cfg = replace(CFG, route=replace(CFG.route, control=False))
    eng = DecisionEngine(cfg)
    for fid in range(1, 10):              # 过选择层目标驻留窗（0.2 s）
        eng.update(_obs(fid), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.coarse_lane == 1.0
    assert eng.selection_report.target_x_m == pytest.approx(TARGET_1_LANE,
                                                            abs=0.02)
    assert eng.route_verdict.lane_ref == 0.0        # 路线判不动，但没人消费


def test_gate_on_raw_flip_does_not_move_target(monkeypatch):
    """锁 2：闸开且路线无定案时，原始粗层翻面不得移动目标——承诺道与选择层
    目标逐位不动。候选道读数全同（都空闲），翻面纯粹是粗层字典序产物。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    holder = [0.0]
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, holder[0]))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    eng.update(_obs(1), DT, executed_lane=0.0, corridor=_corridor())
    before = eng.selection_report.target_x_m
    assert eng.coarse_lane == 0.0
    holder[0] = 1.0                                  # 原始粗层翻面
    for fid in range(2, 20):
        eng.update(_obs(fid), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.coarse_report.lane == 1.0             # 原始读数确实翻了
    assert eng.coarse_lane == 0.0                    # 接控制的没翻
    assert eng.selection_report.target_x_m == pytest.approx(before)
    assert eng.route_verdict.why == "clear"


def test_gate_on_confirmed_block_moves_target(monkeypatch):
    """正常场景：闸开 + 现任道持续被截 → 承诺道与选择目标一起挪到新道。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, 1.0, spec))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    for fid in range(1, 10):
        eng.update(_obs(fid), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.route_verdict.phase == PHASE_MERGING
    assert eng.route_verdict.lane_ref == 1.0
    assert eng.coarse_lane == 1.0
    assert eng.selection_report.target_x_m == pytest.approx(TARGET_1_LANE,
                                                            abs=0.02)


def test_ruler_jump_rebases_but_small_drift_does_not(monkeypatch):
    """参考系纪元只跟**尺子**（选择帧 z=0 中心线）走：小幅漂移不作废，
    跳超过半道才换纪。绝对路心在记忆↔遮挡墙之间的摆动不再是作废理由。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    monkeypatch.setattr(dmod, "choose_lane", lambda *a, **k: _rep(0.0, 0.0))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    for fid, shift in ((1, 0.0), (2, 0.3), (3, 0.9)):   # 漂移 < 半道（1.69 m）
        eng.update(_obs(fid), DT, executed_lane=0.0,
                   corridor=_corridor(shift))
        assert eng.route_verdict.verdict != "epoch_reset"
        assert eng.route_verdict.epoch == 0
    eng.update(_obs(4), DT, executed_lane=0.0, corridor=_corridor(0.9 + LW))
    assert eng.route_verdict.verdict == "epoch_reset"
    assert eng.route_verdict.epoch == 1
