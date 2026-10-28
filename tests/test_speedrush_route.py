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
    ATTR_NORMAL, ATTR_OTHER, ATTR_SHIFT_HELD, ATTR_UNKNOWN,
    ArrivalAttribution, PHASE_KEEP, PHASE_MERGING, PositionLineage,
    RouteParams, init_state, tick_route)
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
    opportunity_confirm_ticks=CFG.route.opportunity_confirm_ticks,
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


def test_merge_cannot_cross_an_occupied_lane():
    """跨道并线要横穿的中间道必须确认空闲：跨被占道并线＝从车上碾过去。

    20261027_160949_1 s2 24.3s：现任道与右道都被 0 m 车占着，urgent 并到
    −1——中间道正贴着一辆深度丢失的车，d_min 0.004。"""
    spec = {1.0: (0.0, "car", False), 0.0: (0.0, "car", False),
            -1.0: CLEAR, 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    s, v = _in_merge()                     # 先把参考道立到 +1
    assert v.lane_ref == 1.0
    for _ in range(P.cancel_unsafe_ticks + P.urgent_confirm_ticks + 2):
        s, v = _tick(s, _rep(1.0, 1.0, spec))
        assert v.lane_ref != -1.0          # 全程不得跨占用车并到 −1
    assert v.verdict == "urgent_no_alt"
    # 中间道确认空闲时不算障碍，但择近仍选邻道——跨道直达本就被净空排序挡住
    spec2 = {1.0: (0.0, "car", False), 0.0: CLEAR, -1.0: CLEAR,
             2.0: (0.0, "space", False), -2.0: (0.0, "space", False)}
    s, v = _in_merge()
    seen = []
    for _ in range(P.cancel_unsafe_ticks + P.urgent_confirm_ticks + 2):
        s, v = _tick(s, _rep(1.0, 1.0, spec2))
        seen.append((v.verdict, v.lane_ref))
    assert ("merge_start", 0.0) in seen and \
        all(ref != -1.0 for _, ref in seen)


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
    """现任道确认空闲且无请求：没有理由动（净空全被窗长封顶）。

    why 记 `no_request` 而不是含糊的 `clear`：离线要能把「本拍没有请求」与
    「有请求但被拒」分开分账（后者带具体原因码）。"""
    s, v = _first(_rep(0.0, 1.0))
    assert v.verdict == "keep" and v.why == "no_request" and v.lane_ref == 0.0


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


# ---------- 到达位置门（C，2026-10-28） ----------

def _arrival_tick(gate: bool, kind: str):
    """走到"本会完成"的那一拍，再按门开/关与归因重跑一次。"""
    s, _ = _in_merge()
    s2, _ = _tick(s, _rep(0.9, 1.0))
    return tick_route(s2, _rep(0.9, 1.0), P, dt_s=DT, epoch=0,
                      arrival=ArrivalAttribution(kind),
                      reject_shift_arrival=gate)


def test_arrival_gate_blocks_only_shift_held():
    """门开时只有 SHIFT_HELD 被拦：位置/速度都成立也不完成，且留 blkfin。"""
    _, v = _arrival_tick(True, ATTR_SHIFT_HELD)
    assert v.verdict == "keep" and v.phase == PHASE_MERGING
    assert v.blocked_finish is True
    for kind in (ATTR_NORMAL, ATTR_OTHER, ATTR_UNKNOWN):
        _, ok = _arrival_tick(True, kind)
        assert ok.verdict == "finish" and ok.blocked_finish is False, kind


def test_arrival_gate_off_is_the_production_path():
    """门关时与今天逐位一致：SHIFT_HELD 也照常完成，blkfin 恒 False。"""
    _, v = _arrival_tick(False, ATTR_SHIFT_HELD)
    assert v.verdict == "finish" and v.why == "arrived"
    assert v.blocked_finish is False


def test_blocked_finish_continues_and_finishes_on_a_trusted_reading():
    """被拦后继续并线；归因恢复 NORMAL 的下一拍照常完成（不是永久卡死）。"""
    s, _ = _in_merge()
    s2, _ = _tick(s, _rep(0.9, 1.0))
    s3, v3 = tick_route(s2, _rep(0.9, 1.0), P, dt_s=DT, epoch=0,
                        arrival=ArrivalAttribution(ATTR_SHIFT_HELD),
                        reject_shift_arrival=True)
    assert v3.verdict == "keep" and v3.blocked_finish is True
    s4, v4 = tick_route(s3, _rep(0.9, 1.0), P, dt_s=DT, epoch=0,
                        arrival=ArrivalAttribution(ATTR_NORMAL),
                        reject_shift_arrival=True)
    assert v4.verdict == "finish" and s4.phase == PHASE_KEEP


def _step(ln: PositionLineage, seq):
    got = None
    for ro, fresh, hop in seq:
        got = ln.step(fresh=fresh, hop=hop, ro=ro, fid=1)
    return got


@pytest.mark.parametrize("name,seq,kind,why", [
    ("首帧新鲜", [(0.0, True, None)], ATTR_UNKNOWN, "first_fresh"),
    ("新鲜 hop=0", [(0.2, True, 0), (0.5, True, 0)], ATTR_NORMAL, None),
    ("新鲜 hop=1", [(0.2, True, 0), (1.3, True, 1)], ATTR_SHIFT_HELD, None),
    ("新鲜 hop=2 黑视", [(0.2, True, 0), (0.4, True, 2)], ATTR_NORMAL, None),
    ("hop=1 的保持值", [(0.2, True, 0), (1.3, True, 1), (1.3, False, None)],
     ATTR_SHIFT_HELD, None),
    ("hop=0 的保持值", [(0.2, True, 0), (0.5, True, 0), (0.5, False, None)],
     ATTR_NORMAL, None),
    ("hop=2 的保持值", [(0.2, True, 0), (0.4, True, 2), (0.4, False, None)],
     ATTR_NORMAL, None),
    ("保持值变了", [(0.2, True, 0), (0.5, True, 0), (0.9, False, None)],
     ATTR_OTHER, "held_changed"),
    ("还没有新鲜读数", [(0.5, False, None)], ATTR_UNKNOWN, "no_fresh"),
    ("缺读数", [(None, False, None)], ATTR_UNKNOWN, "no_reading"),
    ("新鲜度未知", [(0.2, None, None)], ATTR_UNKNOWN, "freshness_unknown"),
    ("新新鲜读数清旧标签",
     [(0.2, True, 0), (1.3, True, 1), (0.4, True, 0), (0.4, False, None)],
     ATTR_NORMAL, None),
    ("旧值晚到不归因",
     [(0.2, True, 0), (1.3, True, 1), (0.4, True, 0), (1.3, False, None)],
     ATTR_OTHER, "held_changed"),
    ("新鲜但 hop 缺失", [(0.2, True, 0), (0.4, True, None)],
     ATTR_UNKNOWN, "no_hop"),
])
def test_position_lineage_contract(name, seq, kind, why):
    """归因契约的边界：端点、黑视、保持值变、首帧、缺失与容差。"""
    got = _step(PositionLineage(), seq)
    assert (got.kind, got.reason) == (kind, why), name


def test_position_lineage_tolerance_boundary():
    """保持值相等用 1e-3 道：边界内归因，边界外是 OTHER（不做近似匹配）。"""
    ln = PositionLineage(tol=1e-3)
    ln.step(fresh=True, hop=1, ro=1.3, fid=1)
    assert ln.step(fresh=False, hop=None, ro=1.301, fid=2).kind == ATTR_SHIFT_HELD
    assert ln.step(fresh=False, hop=None, ro=1.302, fid=3).kind == ATTR_OTHER


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


def test_soft_car_keeps_merging_through_a_car():
    """soft_car：目标道确认有车也不再撤销——车是时间成本，不是几何不可行。"""
    s, v = _in_merge()
    p = replace(P, soft_car=True)
    bad = {1.0: (10.0, "car", False)}
    seen = []
    for _ in range(120):
        s, v = tick_route(s, _rep(0.1, 1.0, bad), p, dt_s=DT, epoch=0)
        seen.append(v.verdict)
        if v.verdict == "timeout":
            break
    assert "cancel" not in seen
    assert v.verdict == "timeout" and v.why == "merge_timeout"


def test_soft_car_space_still_abandons_the_merge():
    """目标道走不下（space）与开关无关：参考道失去几何 → 重建到可行道。"""
    for soft in (False, True):
        s, _ = _in_merge()
        s, v = tick_route(s, _rep(0.5, 1.0, {1.0: (0.0, "space", False)}),
                          replace(P, soft_car=soft), dt_s=DT, epoch=0)
        assert v.verdict == "rebuild" and v.why == "lane_dropped"
        assert v.blocker == "space" and v.lane_ref == 0.0


def test_soft_car_leaves_escape_rule_unchanged():
    """soft_car 只改并线撤销；保道态的逃生起意规则逐位不动（车仍是起意理由）。"""
    p = replace(P, soft_car=True)
    spec = {0.0: (12.0, "car", False)}
    s, _ = tick_route(None, _rep(0.0, 0.0, spec), p, dt_s=DT, epoch=0)
    for _ in range(p.urgent_confirm_ticks):
        s, v = tick_route(s, _rep(0.0, 0.0, spec), p, dt_s=DT, epoch=0)
    assert v.verdict == "merge_start" and v.why == "urgent"


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


_HALF_LANE = (0.5 - CFG.selection.ego_half_lane
              - CFG.selection.lane_clearance_lane)
TARGET_1_LANE = (1.0 - _HALF_LANE) * LW   # L4 区间外只去最近边界


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
    assert eng.route_verdict.why == "no_request"


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


def test_soft_car_config_reaches_the_route(monkeypatch):
    """接线：`route.soft_car=true` 传到路线参数——目标道有车，并线不撤销。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    holder = [{0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}]
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, 1.0, holder[0]))
    cfg = replace(CFG, route=replace(CFG.route, control=True, soft_car=True))
    eng = DecisionEngine(cfg)
    for fid in range(1, 10):                     # 现任道确认被截 → 并线 +1
        eng.update(_obs(fid), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.route_verdict.phase == PHASE_MERGING
    assert eng.route_verdict.lane_ref == 1.0
    holder[0] = {0.0: (12.0, "car", False), 1.0: (10.0, "car", False),
                 -1.0: (0.0, "space", False)}
    for fid in range(10, 30):                    # 目标道有车：不撤
        eng.update(_obs(fid), DT, executed_lane=0.5, corridor=_corridor())
        assert eng.route_verdict.verdict != "cancel"
    assert eng.route_verdict.phase == PHASE_MERGING


def test_merge_target_commits_without_slew(monkeypatch):
    """并线拍目标直接是承诺道带边：承诺期不走 slew，机会层也不参与。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, 1.0, spec))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    out = None
    for fid in range(1, 10):
        out = eng.update(_obs(fid), DT, executed_lane=0.0,
                         corridor=_corridor())
    assert eng.route_verdict.phase == PHASE_MERGING
    assert eng.selection_report.why == "commit"
    assert eng.coarse_lane == 1.0
    assert eng.selection_report.target_x_m == pytest.approx(
        TARGET_1_LANE, abs=0.02)
    assert out.x_target == pytest.approx(TARGET_1_LANE / LW, abs=0.01)
    assert out.phase == "merging"


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


def test_executed_reanchor_resets_route(monkeypatch):
    """执行层重锚（executed 单拍跳 >半道）也换纪：⑥b 确认参考系翻面后，
    道号语义跟着换，旧承诺指的道已不是车所在的道。20261027_154626_1 s1
    13.3s：executed −1.38→+0.58 后路线仍守 −1，空路上满舵左打 1.4 道。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    monkeypatch.setattr(dmod, "choose_lane", lambda *a, **k: _rep(0.0, 0.0))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    for fid, ex in ((1, 0.0), (2, 0.2), (3, 0.4)):   # 正常横移：不作废
        eng.update(_obs(fid), DT, executed_lane=ex, corridor=_corridor())
        assert eng.route_verdict.verdict != "epoch_reset"
        assert eng.route_verdict.epoch == 0
    eng.update(_obs(4), DT, executed_lane=1.3, corridor=_corridor())
    assert eng.route_verdict.verdict == "epoch_reset"
    assert eng.route_verdict.epoch == 1


# ---------- 机会换道请求（跨线唯一入口，2026-10-27） ----------

def _tick_req(state, report, request, epoch=0):
    return tick_route(state, report, P, dt_s=DT, epoch=epoch, request=request)


def _req_run(state, report, request, n, epoch=0):
    """连续 n 拍同侧请求（起意计分门槛用），返回末拍状态与定案。"""
    for _ in range(n):
        state, v = _tick_req(state, report, request, epoch)
    return state, v


def _req_until_merge(state, report, request, limit=30):
    """一拍一拍喂请求直到起意，返回 (状态, 定案, 用时拍数)。"""
    for i in range(1, limit + 1):
        state, v = _tick_req(state, report, request)
        if v.verdict == "merge_start":
            return state, v, i
    raise AssertionError("never merged")


def test_opportunity_request_starts_a_merge():
    """现任道确认空闲 + 请求=相邻合格道且连续到门槛 → 起意并线（why=opportunity）。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)     # 首拍立状态（无请求）
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, P.opportunity_confirm_ticks - 1)
    assert v.verdict == "keep" and v.credit == P.opportunity_confirm_ticks - 1
    assert v.why == "scoring"                       # 有请求、未到门槛——不是拒绝
    s, v = _tick_req(s, _rep(0.0, 0.0), 1.0)        # 补满门槛那一拍起意
    assert v.verdict == "merge_start" and v.why == "opportunity"
    assert v.lane_ref == 1.0 and s.phase == PHASE_MERGING
    s, v = _tick_req(s, _rep(0.9, 1.0), 1.0)        # 到位一拍：横速未落稳
    assert v.verdict == "keep" and v.why == "merging"
    s, v = _tick_req(s, _rep(0.9, 1.0), 1.0)        # 速率归零、位差进带宽
    assert v.verdict == "finish" and v.why == "arrived"


def test_flicker_requests_never_fire():
    """断一拍扣一分：闪检（请求 ↔ 无请求交替）永远攒不到门槛。

    165841 局原始请求 61 段里 40 段只有 1~2 拍（币组检测闪断）——不设这道门
    一局要并线 60 次，正是「毫无章法」的另一种穿法。"""
    s, v = _first(_rep(0.0, 0.0))
    for i in range(40):
        s, v = _tick_req(s, _rep(0.0, 0.0), 1.0 if i % 2 == 0 else None)
        assert v.verdict == "keep"
    assert v.credit <= 1.0


def test_side_switch_restarts_the_credit():
    """请求换到另一侧重新起计：左右两边各闪一下不该凑成一次并线。"""
    s, v = _first(_rep(0.0, 0.0))
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, P.opportunity_confirm_ticks - 1)
    assert v.verdict == "keep"
    s, v = _tick_req(s, _rep(0.0, 0.0), -1.0)       # 换侧：计分重置为 1
    assert v.verdict == "keep" and v.credit == 1.0
    s, v = _req_run(s, _rep(0.0, 0.0), -1.0, P.opportunity_confirm_ticks - 2)
    assert v.verdict == "keep" and v.credit == P.opportunity_confirm_ticks - 1
    s, v = _tick_req(s, _rep(0.0, 0.0), -1.0)
    assert v.verdict == "merge_start" and v.lane_ref == -1.0


def test_request_rejects_far_blocked_and_current_targets():
    """两号外、无法确认/走不下、请求=现任道——统统维持 keep，不起意。

    每条都带**第一原因码**：离线按段统计「请求为什么没变成并线」直接读
    `route.why`，不需要重放判据。"""
    s, v = _first(_rep(0.0, 0.0))
    n = P.opportunity_confirm_ticks + 2
    for req, spec, why in (
            (2.0, None, "not_adjacent"),
            (-2.0, None, "not_adjacent"),
            (1.0, {1.0: (0.0, "unknown_car", True)}, "target_unqualified"),
            (1.0, {1.0: (0.0, "space", False)}, "target_unqualified"),
            (0.0, None, "same_lane")):
        s2, v2 = _req_run(s, _rep(0.0, 0.0, spec), req, n)
        assert v2.verdict == "keep" and v2.why == why, req


def test_epoch_rebuild_clears_pending_credit():
    """换纪重建拍不起意并线，并把累计分清零：参考系翻面后旧计分没有意义。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, P.opportunity_confirm_ticks - 1)
    assert v.verdict == "keep" and v.credit == P.opportunity_confirm_ticks - 1
    s, v = _tick_req(s, _rep(0.0, 0.0), 1.0, epoch=1)   # 换纪当拍
    assert v.verdict == "epoch_reset" and v.phase == PHASE_KEEP
    assert v.lane_ref == 0.0 and s.req_credit == 0.0


def test_request_cancelled_by_cooldown_then_reissued():
    """锁 1：请求被撤销（目标道确认被截）→ 回落现任道且冷却期内不回头；
    冷却结束后同一请求重新起意——被拒/撤销期间目标由选择层回夹本道区间。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)     # 首拍立状态（无请求）
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, P.opportunity_confirm_ticks)
    assert v.verdict == "merge_start"
    bad = {1.0: (10.0, "car", False)}
    for _ in range(P.cancel_unsafe_ticks):
        s, v = _tick_req(s, _rep(0.5, 1.0, bad), 1.0)
    assert v.verdict == "cancel" and v.lane_ref == 0.0
    assert v.cooldown_left_s == pytest.approx(P.cancel_cooldown_s)
    banned = int(P.cancel_cooldown_s / DT) - 3       # 冷却尚未走完的区间
    seen_ban = False
    for i in range(banned):
        s, v = _tick_req(s, _rep(0.0, 0.0), 1.0)     # 机会还在，请求逐拍刷新
        assert v.verdict != "merge_start", i
        if v.credit >= P.opportunity_confirm_ticks:
            assert v.why == "cooldown_ban", i        # 门槛已到，被冷却挡住
            seen_ban = True
    assert seen_ban
    s, v, _n = _req_until_merge(s, _rep(0.0, 0.0), 1.0, limit=6)
    assert v.why == "opportunity"


def test_request_needs_current_lane_confirmed_clear():
    """锁 3：现任道没有「确认空闲」证据时，计分攒满也不起意——机会不能插队
    安全判据（被占/无法确认的道要先走逃生路径的节奏）。"""
    spec = {0.0: (2.0, "unknown_car", True), 1.0: CLEAR}
    s, v = _first(_rep(0.0, 0.0, spec))
    assert v.verdict == "keep" and v.why == "blocked_unconfirmed"
    s, v = _req_run(s, _rep(0.0, 0.0, spec), 1.0,
                    P.opportunity_confirm_ticks + 4)
    assert v.verdict == "keep" and v.why == "current_unconfirmed"
    assert v.credit > P.opportunity_confirm_ticks      # 分攒够了，门没开


def test_request_does_not_short_circuit_escape_confirmation():
    """现任道持续被截：确认满门槛那一拍走 urgent，不是 opportunity（同一次
    并线只有一条起意理由，逃生优先）。确认未满的拍请求仍在攒分（why=scoring），
    不冒充拒绝原因。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s, _ = _tick(None, _rep(0.0, 0.0, spec), 0)
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)
    assert v.verdict == "keep" and v.why == "scoring"
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)     # blocked_ticks=2
    assert v.verdict == "keep" and v.why == "scoring"
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)     # 满 3 拍：urgent 先行
    assert v.verdict == "merge_start" and v.why == "urgent"


# ---------- 请求契约：相对偏移 → 绝对目标道（2026-10-27 冻结） ----------

def test_request_resolves_relative_offset_on_nonzero_lane_ref():
    """锁①②：承诺道非 0 时请求 ±1 必须解析成**绝对**相邻道。

    实跑 20261027_183143 的机会换道 0 触发就死在这里——`ref=+1` 且 `req=+1`
    （本意"再往右一道"）被按绝对道号查成"换到当前道"，`_try_request` 首条
    同道判据静默拒绝；反号则差两道同样被拒。旧测试全在 `lane_ref=0` 上喂
    请求，两种口径恰好重合，锁不住契约。"""
    s = replace(init_state(0), lane_ref=1.0, lane_from=1.0)
    s, v, _n = _req_until_merge(s, _rep(1.0, 1.0), 1.0, limit=10)
    assert v.verdict == "merge_start" and v.why == "opportunity"
    assert v.lane_ref == 2.0 and s.req_lane is None      # 起意即清，目标=绝对 +2
    s2 = replace(init_state(0), lane_ref=1.0, lane_from=1.0)
    s2, v2 = _req_run(s2, _rep(1.0, 1.0), -1.0, P.opportunity_confirm_ticks - 1)
    assert v2.credit == P.opportunity_confirm_ticks - 1 and s2.req_lane == 0.0
    s2, v2 = _tick_req(s2, _rep(1.0, 1.0), -1.0)
    assert v2.verdict == "merge_start" and v2.lane_ref == 0.0


def test_pending_request_dies_with_its_reference_lane():
    """锁③：待消费请求期间承诺道改变 → 请求与计分同时作废，不得按新参照系换算。

    换算基准取消费拍的 `lane_ref`，正确性全靠这条不变量兜底；逃生分支是它的
    实时检验场——请求还活着（计分未到门槛）时承诺道被逃生改掉。"""
    spec = {0.0: (2.0, "car", False), 1.0: CLEAR, 2.0: CLEAR}
    s = replace(init_state(0), lane_ref=0.0, lane_from=0.0)
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)
    assert s.req_lane == 1.0 and s.req_credit == 1.0
    for i in range(P.urgent_confirm_ticks - 1):
        s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)
    assert v.verdict == "merge_start" and v.why == "urgent"
    assert s.lane_ref != 0.0                             # 承诺道确实变了
    assert s.req_lane is None and s.req_credit == 0.0    # 旧请求不跟到新参照系


def test_request_beyond_the_road_is_not_substituted():
    """锁⑤：解析出的绝对目标道不存在 → 明确不起意，且**不悄悄换成别的道**。"""
    s = replace(init_state(0), lane_ref=2.0, lane_from=2.0)
    s, v = _req_run(s, _rep(2.0, 2.0), 1.0, P.opportunity_confirm_ticks + 3)
    assert v.verdict != "merge_start"
    assert v.why == "target_missing"                      # 目标道不存在，不是拒绝得含糊
    assert s.lane_ref == 2.0 and v.lane_ref == 2.0        # 原地不动，不找替补


def test_credit_does_not_leak_across_a_merge():
    """锁⑥：信用生命周期——并线中不累计，完成后第二跳重新攒满门槛。

    两跳全部真实推进（不摆 `lane_ref`）：0→1 起意、到位完成，再在承诺道 1 上
    请求相对 +1。并线期照攒分时第二跳 4 拍就起意（继承上一跳攒下的 2 分）——
    等于让已经结束的判断替这一拍做决定；换算也只在非零承诺道上才验得出来，
    第二跳的落点必须是**绝对 +2**。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)      # 冷启动
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, P.opportunity_confirm_ticks)
    assert v.verdict == "merge_start" and v.lane_ref == 1.0
    s, v = _tick_req(s, _rep(1.0, 1.0), 1.0)         # 到位拍：横速未落稳
    assert s.phase == PHASE_MERGING and s.req_credit == 0.0
    s, v = _tick_req(s, _rep(1.0, 1.0), 1.0)         # 完成拍
    assert v.verdict == "finish" and s.req_lane is None and s.req_credit == 0.0
    s, v, n = _req_until_merge(s, _rep(1.0, 1.0), 1.0, limit=10)
    assert n == P.opportunity_confirm_ticks           # 重新攒满，不继承
    assert v.lane_ref == 2.0                          # 相对 +1 = 绝对 +2


def test_missing_evidence_clears_credit():
    """锁⑦：缺车道级证据的拍把机会计分清零。

    冻结＝断供多久都保进度、恢复首拍即撞门槛；按拍扣分＝断供前后两段不连续的
    证据仍能凑成一次「机会连续被看见」。机会换道是主动改路线，证据不连续时
    重攒的代价远低于错并线的代价。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, P.opportunity_confirm_ticks - 1)
    assert v.credit == P.opportunity_confirm_ticks - 1
    no_lanes = CoarseReport(0.0, 0.0, 0, 24.0, "keep", ())
    for dead in (None, CoarseReport(0.0, 0.0, 0, 24.0, "no_road", ()), no_lanes):
        s2, v2 = _tick_req(s, dead, 1.0)
        assert v2.verdict == "needs_init"
        assert s2.req_credit == 0.0 and s2.req_lane is None
    s2, v2, n = _req_until_merge(s2, _rep(0.0, 0.0), 1.0, limit=10)
    assert n == P.opportunity_confirm_ticks
