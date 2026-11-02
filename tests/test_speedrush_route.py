# -*- coding: utf-8 -*-
"""路线承诺层（2026-10-23 V1，2026-10-24 实机两局后收敛）的回归锁。

锁的是**实际会生效的规则**：现任道连续确认被截才换、只换到确实更宽的道、
并线中只认完成与撤销、撤销后冷却期禁入刚放弃的道、参考系换纪/参考道失去几何
时重建到**可行道**。场景数值按 decision.json 起值反推（窗 24 m、门槛 4 m、
确认 0.15 s），参数与场景是一对契约面，改数值会红。

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

LW = 3.5
DT = 0.05
CFG = _read_decision(DECISION_FILE)
# 与 DecisionEngine 同一套装配：尺度/速度类数值只有既有真源
P = RouteParams(
    switch_gain_m=CFG.coarse.switch_margin_m,
    proxy_speed_mps=CFG.coarse.range_speed_mps,
    urgent_confirm_s=CFG.route.urgent_confirm_s,
    opportunity_confirm_s=CFG.route.opportunity_confirm_s,
    cancel_unsafe_s=CFG.route.cancel_unsafe_s,
    cancel_cooldown_s=CFG.route.cancel_cooldown_s,
    finish_tol_lane=CFG.selection.center_band_lane,
    finish_v_tol_lane_s=CFG.route.finish_v_tol_lane_s,
    merge_max_s=CFG.route.merge_max_s)
CLEAR = (24.0, None, False)               # 确认空闲（净空=窗长）
WINDOW_M = CLEAR[0]
# 50 ms 节拍下与旧拍数门的等价拍数：0.15/0.25/0.25 s = 3/5/5 拍
URGENT_TICKS = round(P.urgent_confirm_s / DT)
OPP_TICKS = round(P.opportunity_confirm_s / DT)
CANCEL_TICKS = round(P.cancel_unsafe_s / DT)


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
    """冷启动后连续喂同一读数，直到占用确认（urgent_confirm_s）。"""
    s, _ = _tick(None, report, epoch)
    for _ in range(URGENT_TICKS):
        s, v = _tick(s, report, epoch)
    return s, v


def _confirm_with_ttc(report, lane_ttc, epoch=0):
    """同上，但带车道到站时间表（紧急逃生时间门）。"""
    s, _ = tick_route(None, report, P, dt_s=DT, epoch=epoch, lane_ttc=lane_ttc)
    for _ in range(URGENT_TICKS):
        s, v = tick_route(s, report, P, dt_s=DT, epoch=epoch,
                          lane_ttc=lane_ttc)
    return s, v


def _in_merge(epoch=0):
    """推进成"正在并线"：现任道持续被截，换到 +1（确认空闲；−1 显式判成走不下，
    因为左右同分时按道号取靠左的一侧——与粗层同一套字典序）。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s, _ = _tick(None, _rep(0.0, 0.0), epoch)
    for _ in range(URGENT_TICKS):
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


def test_urgent_requires_time_evidence():
    """紧急逃生要有时间证据：粗层「24m 窗内有车」按代理速度折算永远紧急，
    车还在 3s 外就换道是画龙的主发动机（2026-10-30 90s 局 21 次并线里
    11 次如此）。到站时间 >urgent_ttc_s 时保持本道。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s = _first(_rep(0.0, 0.0, spec))[0]
    for _ in range(URGENT_TICKS):
        s, v = tick_route(s, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                          lane_ttc={0: 3.0})
    assert v.verdict == "keep" and v.why == "not_urgent"
    assert v.lane_ref == 0.0
    # 车真的要到线上（≤urgent_ttc_s）才起意
    s, v = tick_route(s, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                      lane_ttc={0: 0.5})
    assert v.verdict == "merge_start" and v.why == "urgent"


def test_urgent_blind_zone_without_time_evidence():
    """无到站时间（同速/无法测速）时只认近距离盲区：净空 4m 仍逃生，
    12m 不动——不接近的车不该逼出换道。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s = _first(_rep(0.0, 0.0, spec))[0]
    for _ in range(URGENT_TICKS):
        s, v = tick_route(s, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                          lane_ttc={})
    assert v.verdict == "keep" and v.why == "not_urgent"
    near = {0.0: (4.0, "car", False), -1.0: (0.0, "space", False)}
    s, v = _confirm_with_ttc(_rep(0.0, 0.0, near), {})
    assert v.verdict == "merge_start" and v.why == "urgent"


def test_escape_target_with_imminent_car_is_rejected():
    """逃生目标道上有车且到站时间太短 → 不选它：并过去正好撞上。
    目标道无车/车远时仍可选。"""
    spec = {0.0: (12.0, "car", False), -1.0: CLEAR,
            1.0: (0.0, "space", False), 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    _, v = _confirm_with_ttc(_rep(0.0, 0.0, spec), {0: 0.3, -1: 0.4})
    assert v.verdict == "urgent_no_alt" and v.why == "no_alt"
    _, v = _confirm_with_ttc(_rep(0.0, 0.0, spec), {0: 0.3, -1: 2.0})
    assert v.verdict == "merge_start" and v.lane_ref == -1.0


def test_path_clear_starts_from_measured_lane():
    """穿越中间道的起点用**实测横位**：承诺道写 0、车实际在 0.7 道（属于
    +1）时并向 −1 要跨过 0 道，不能因为 ref=0 把 0 道算在起点外。
    20261030_122416 seq1422：从 0.67 道并向 −1，按 ref=0 算中间道为空，
    实际碾过 0 道上 7.4m 的车。"""
    spec = {0.0: (12.0, "car", False), -1.0: CLEAR,
            1.0: (0.0, "space", False), 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    s = _first(_rep(0.7, 0.0, spec))[0]
    for _ in range(URGENT_TICKS + 2):
        s, v = tick_route(s, _rep(0.7, 0.0, spec), P, dt_s=DT, epoch=0,
                          lane_ttc={0: 0.3})
        assert not (v.verdict == "merge_start" and v.lane_ref == -1.0)
    assert v.verdict == "urgent_no_alt"


def test_merge_cannot_cross_an_occupied_lane():
    """跨道并线要横穿的中间道必须确认空闲：跨被占道并线＝从车上碾过去。

    20261027_160949_1 s2 24.3s：现任道与右道都被 0 m 车占着，urgent 并到
    −1——中间道正贴着一辆深度丢失的车，d_min 0.004。"""
    spec = {1.0: (0.0, "car", False), 0.0: (0.0, "car", False),
            -1.0: CLEAR, 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    s, v = _in_merge()                     # 先把参考道立到 +1
    assert v.lane_ref == 1.0
    for _ in range(CANCEL_TICKS + URGENT_TICKS + 2):
        s, v = _tick(s, _rep(1.0, 1.0, spec))
        assert v.lane_ref != -1.0          # 全程不得跨占用车并到 −1
    assert v.verdict == "urgent_no_alt"
    # 中间道确认空闲时不算障碍，但择近仍选邻道——跨道直达本就被净空排序挡住
    spec2 = {1.0: (0.0, "car", False), 0.0: CLEAR, -1.0: CLEAR,
             2.0: (0.0, "space", False), -2.0: (0.0, "space", False)}
    s, v = _in_merge()
    seen = []
    for _ in range(CANCEL_TICKS + URGENT_TICKS + 2):
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
    for _ in range(CANCEL_TICKS - 1):
        s, v = _tick(s, _rep(0.5, 1.0, bad))
    assert v.verdict == "cancel" and v.lane_ref == 0.0
    assert v.blocker == "car" and v.as_trace()["blk"] == "car"
    assert s.ban_lane == 1.0 and v.cooldown_left_s > 0.0
    # 冷却期内：0 道被截、+1 道是唯一更宽的空闲道，但它是刚放弃的那条 → 不动
    spec = {0.0: (12.0, "car", False), 1.0: CLEAR,
            -1.0: (14.0, "car", False), 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    for _ in range(URGENT_TICKS):
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
    for _ in range(URGENT_TICKS):
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
                     BandBoundary(-7.0 + shift_m, "measured"),
                     BandBoundary(7.0 + shift_m, "measured"))
        for i in range(9))
    # 供数门 master 固定在 0：带整体平移才能改变"尺子"（场景 z=0 中心线）；
    # master 跟着带一起动的话，原点会把平移抵消掉，尺子纹丝不动
    return Corridor(bands, 0.0, 14.0 / LW, "pair", "ok")


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
    monkeypatch.setattr(dmod, "_lane_ttc_map", lambda *a, **k: {0: 0.3})
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
    monkeypatch.setattr(dmod, "_lane_ttc_map", lambda *a, **k: {0: 0.3})
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


def test_merge_target_slews_at_merge_rate(monkeypatch):
    """并线拍目标按 4.0 道/s 斜坡到承诺道：不再单拍直通，也不挡路线承诺。

    选择层的目标仍是整道（selection_report.target_x_m），只有交给控制的
    输出被限速；起手阶跃 1.6~1.9 道是贴边/外侧超调的来源。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, 1.0, spec))
    monkeypatch.setattr(dmod, "_lane_ttc_map", lambda *a, **k: {0: 0.3})
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    out = None
    targets = []
    for fid in range(1, 10):
        out = eng.update(_obs(fid), DT, executed_lane=0.0,
                         corridor=_corridor())
        targets.append(out.x_target)
    assert eng.route_verdict.phase == PHASE_MERGING
    assert eng.selection_report.why == "commit"
    assert eng.coarse_lane == 1.0
    assert eng.selection_report.target_x_m == pytest.approx(
        TARGET_1_LANE, abs=0.02)
    steps = [abs(targets[i] - targets[i - 1]) for i in range(1, len(targets))]
    assert max(steps) <= 4.0 * DT + 1e-6          # 4.0 道/s 斜坡
    assert min(targets) < 0.9                     # 首拍没有直通整道
    assert targets[-1] == pytest.approx(TARGET_1_LANE / LW, abs=0.01)
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
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, OPP_TICKS - 1)
    assert v.verdict == "keep" \
        and v.credit == pytest.approx((OPP_TICKS - 1) * DT)
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
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, OPP_TICKS - 1)
    assert v.verdict == "keep"
    s, v = _tick_req(s, _rep(0.0, 0.0), -1.0)       # 换侧：计分重置为一拍
    assert v.verdict == "keep" and v.credit == pytest.approx(DT)
    s, v = _req_run(s, _rep(0.0, 0.0), -1.0, OPP_TICKS - 2)
    assert v.verdict == "keep" \
        and v.credit == pytest.approx((OPP_TICKS - 1) * DT)
    s, v = _tick_req(s, _rep(0.0, 0.0), -1.0)
    assert v.verdict == "merge_start" and v.lane_ref == -1.0


def test_request_rejects_far_blocked_and_current_targets():
    """三号外、无法确认/走不下、请求=现任道——统统维持 keep，不起意。

    每条都带**第一原因码**：离线按段统计「请求为什么没变成并线」直接读
    `route.why`，不需要重放判据。请求口径 ±2（2026-11-02）；三号外目标
    不在粗层候选集（±2）里，第一原因码是 target_missing。"""
    s, v = _first(_rep(0.0, 0.0))
    n = OPP_TICKS + 2
    for req, spec, why in (
            (3.0, None, "target_missing"),
            (-3.0, None, "target_missing"),
            (1.0, {1.0: (0.0, "unknown_car", True)}, "target_unqualified"),
            (1.0, {1.0: (0.0, "space", False)}, "target_unqualified"),
            (0.0, None, "same_lane")):
        s2, v2 = _req_run(s, _rep(0.0, 0.0, spec), req, n)
        assert v2.verdict == "keep" and v2.why == why, req


def test_request_two_lanes_starts_merge_when_path_clear():
    """请求口径 ±2（2026-11-02）：隔一条道的够格机会，中间道确认空闲 →
    起意并线（光锥判据，不是道距）。"""
    s, v = _first(_rep(0.0, 0.0))
    s, v = _req_run(s, _rep(0.0, 0.0), 2.0, OPP_TICKS)
    assert v.verdict == "merge_start" and v.why == "opportunity"
    assert v.lane_ref == 2.0 and s.phase == PHASE_MERGING


def test_request_two_lanes_blocked_by_middle_lane():
    """±2 请求的守卫：横穿的中间道被占（未确认空闲）→ path_blocked，不起意。"""
    s, v = _first(_rep(0.0, 0.0))
    spec = {1.0: (0.0, "car", False)}
    s, v = _req_run(s, _rep(0.0, 0.0, spec), 2.0, OPP_TICKS + 2)
    assert v.verdict == "keep" and v.why == "path_blocked"


def test_epoch_rebuild_clears_pending_credit():
    """换纪重建拍不起意并线，并把累计分清零：参考系翻面后旧计分没有意义。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, OPP_TICKS - 1)
    assert v.verdict == "keep" \
        and v.credit == pytest.approx((OPP_TICKS - 1) * DT)
    s, v = _tick_req(s, _rep(0.0, 0.0), 1.0, epoch=1)   # 换纪当拍
    assert v.verdict == "epoch_reset" and v.phase == PHASE_KEEP
    assert v.lane_ref == 0.0 and s.req_credit == 0.0


def test_request_cancelled_by_cooldown_then_reissued():
    """锁 1：请求被撤销（目标道确认被截）→ 回落现任道且冷却期内不回头；
    冷却结束后同一请求重新起意——被拒/撤销期间目标由选择层回夹本道区间。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)     # 首拍立状态（无请求）
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, OPP_TICKS)
    assert v.verdict == "merge_start"
    bad = {1.0: (10.0, "car", False)}
    for _ in range(CANCEL_TICKS):
        s, v = _tick_req(s, _rep(0.5, 1.0, bad), 1.0)
    assert v.verdict == "cancel" and v.lane_ref == 0.0
    assert v.cooldown_left_s == pytest.approx(P.cancel_cooldown_s)
    banned = int(P.cancel_cooldown_s / DT) - 3       # 冷却尚未走完的区间
    seen_ban = False
    for i in range(banned):
        s, v = _tick_req(s, _rep(0.0, 0.0), 1.0)     # 机会还在，请求逐拍刷新
        assert v.verdict != "merge_start", i
        if v.credit + 1e-9 >= P.opportunity_confirm_s:
            assert v.why == "cooldown_ban", i        # 门槛已到，被冷却挡住
            seen_ban = True
    assert seen_ban
    s, v, _n = _req_until_merge(s, _rep(0.0, 0.0), 1.0, limit=OPP_TICKS)
    assert v.why == "opportunity"


def test_request_needs_current_lane_confirmed_clear():
    """锁 3：现任道没有「确认空闲」证据时，计分攒满也不起意——机会不能插队
    安全判据（被占/无法确认的道要先走逃生路径的节奏）。"""
    spec = {0.0: (2.0, "unknown_car", True), 1.0: CLEAR}
    s, v = _first(_rep(0.0, 0.0, spec))
    assert v.verdict == "keep" and v.why == "blocked_unconfirmed"
    s, v = _req_run(s, _rep(0.0, 0.0, spec), 1.0,
                    OPP_TICKS + 4)
    assert v.verdict == "keep" and v.why == "current_unconfirmed"
    assert v.credit + 1e-9 >= P.opportunity_confirm_s  # 分攒够了，门没开


def test_request_does_not_short_circuit_escape_confirmation():
    """现任道持续被截：确认满门槛那一拍走 urgent，不是 opportunity（同一次
    并线只有一条起意理由，逃生优先）。确认未满的拍请求仍在攒分（why=scoring），
    不冒充拒绝原因。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s, _ = _tick(None, _rep(0.0, 0.0, spec), 0)
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)
    assert v.verdict == "keep" and v.why == "scoring"
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)     # blocked_s=0.10
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
    s2, v2 = _req_run(s2, _rep(1.0, 1.0), -1.0, OPP_TICKS - 1)
    assert v2.credit == pytest.approx((OPP_TICKS - 1) * DT) \
        and s2.req_lane == 0.0
    s2, v2 = _tick_req(s2, _rep(1.0, 1.0), -1.0)
    assert v2.verdict == "merge_start" and v2.lane_ref == 0.0


def test_pending_request_dies_with_its_reference_lane():
    """锁③：待消费请求期间承诺道改变 → 请求与计分同时作废，不得按新参照系换算。

    换算基准取消费拍的 `lane_ref`，正确性全靠这条不变量兜底；逃生分支是它的
    实时检验场——请求还活着（计分未到门槛）时承诺道被逃生改掉。"""
    spec = {0.0: (2.0, "car", False), 1.0: CLEAR, 2.0: CLEAR}
    s = replace(init_state(0), lane_ref=0.0, lane_from=0.0)
    s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)
    assert s.req_lane == 1.0 and s.req_credit == pytest.approx(DT)
    for i in range(URGENT_TICKS - 1):
        s, v = _tick_req(s, _rep(0.0, 0.0, spec), 1.0)
    assert v.verdict == "merge_start" and v.why == "urgent"
    assert s.lane_ref != 0.0                             # 承诺道确实变了
    assert s.req_lane is None and s.req_credit == 0.0    # 旧请求不跟到新参照系


def test_request_beyond_the_road_is_not_substituted():
    """锁⑤：解析出的绝对目标道不存在 → 明确不起意，且**不悄悄换成别的道**。"""
    s = replace(init_state(0), lane_ref=2.0, lane_from=2.0)
    s, v = _req_run(s, _rep(2.0, 2.0), 1.0, OPP_TICKS + 3)
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
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, OPP_TICKS)
    assert v.verdict == "merge_start" and v.lane_ref == 1.0
    s, v = _tick_req(s, _rep(1.0, 1.0), 1.0)         # 到位拍：横速未落稳
    assert s.phase == PHASE_MERGING and s.req_credit == 0.0
    s, v = _tick_req(s, _rep(1.0, 1.0), 1.0)         # 完成拍
    assert v.verdict == "finish" and s.req_lane is None and s.req_credit == 0.0
    s, v, n = _req_until_merge(s, _rep(1.0, 1.0), 1.0, limit=10)
    assert n == OPP_TICKS           # 重新攒满，不继承
    assert v.lane_ref == 2.0                          # 相对 +1 = 绝对 +2


def test_missing_evidence_clears_credit():
    """锁⑦：缺车道级证据的拍把机会计分清零。

    冻结＝断供多久都保进度、恢复首拍即撞门槛；按拍扣分＝断供前后两段不连续的
    证据仍能凑成一次「机会连续被看见」。机会换道是主动改路线，证据不连续时
    重攒的代价远低于错并线的代价。"""
    s, v = _tick_req(None, _rep(0.0, 0.0), 0.0)
    s, v = _req_run(s, _rep(0.0, 0.0), 1.0, OPP_TICKS - 1)
    assert v.credit == pytest.approx((OPP_TICKS - 1) * DT)
    no_lanes = CoarseReport(0.0, 0.0, 0, 24.0, "keep", ())
    for dead in (None, CoarseReport(0.0, 0.0, 0, 24.0, "no_road", ()), no_lanes):
        s2, v2 = _tick_req(s, dead, 1.0)
        assert v2.verdict == "needs_init"
        assert s2.req_credit == 0.0 and s2.req_lane is None
    s2, v2, n = _req_until_merge(s2, _rep(0.0, 0.0), 1.0, limit=10)
    assert n == OPP_TICKS


def test_pass_hold_keeps_lane_while_passing():
    """超车动作进行中路线不抢方向：临近车 0.3s 到站本会起 urgent，但选择层
    正在出擦边目标时保持本道（并线会关闭机会层，把刚要成的擦边改成并线）。
    选择层退出 pass 的下一拍立即恢复逃生判据。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s = _first(_rep(0.0, 0.0, spec))[0]
    for _ in range(URGENT_TICKS):
        s, v = tick_route(s, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                          lane_ttc={0: 0.3}, passing=True)
    assert v.verdict == "keep" and v.why == "pass_hold"
    assert v.lane_ref == 0.0
    s, v = tick_route(s, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                      lane_ttc={0: 0.3}, passing=False)
    assert v.verdict == "merge_start" and v.why == "urgent"
    # 开关关掉：pass 中也照旧逃生
    s = _first(_rep(0.0, 0.0, spec))[0]
    p = replace(P, pass_hold=False)
    seen = []
    for _ in range(URGENT_TICKS):
        s, v = tick_route(s, _rep(0.0, 0.0, spec), p, dt_s=DT, epoch=0,
                          lane_ttc={0: 0.3}, passing=True)
        seen.append((v.verdict, v.why))
    assert ("merge_start", "urgent") in seen


def test_overtake_merges_early_into_a_clear_lane():
    """提前超车：车 2.0s 才到站（未到紧急门），邻道确认空闲、路径空闲 →
    现在就并过去。只认完全空闲的邻道；有车的道不因「超车」而并。"""
    spec = {0.0: (12.0, "car", False), -1.0: CLEAR,
            1.0: (0.0, "space", False), 2.0: (0.0, "space", False),
            -2.0: (0.0, "space", False)}
    s = _first(_rep(0.0, 0.0, spec))[0]
    seen = []
    for _ in range(URGENT_TICKS + 1):
        s, v = tick_route(s, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                          lane_ttc={0: 2.0})
        seen.append((v.verdict, v.why))
    assert ("merge_start", "overtake") in seen
    # 邻道有车（即使更宽）不是提前超车目标
    occ = {0.0: (12.0, "car", False), -1.0: (20.0, "car", False),
           1.0: (0.0, "space", False), 2.0: (0.0, "space", False),
           -2.0: (0.0, "space", False)}
    s2, v2 = _confirm_with_ttc(_rep(0.0, 0.0, occ), {0: 2.0, -1: 5.0})
    assert not (v2.verdict == "merge_start" and v2.why == "overtake")
    # 超车动作进行中：路线不抢方向
    s3 = _first(_rep(0.0, 0.0, spec))[0]
    for _ in range(URGENT_TICKS + 1):
        s3, v3 = tick_route(s3, _rep(0.0, 0.0, spec), P, dt_s=DT, epoch=0,
                            lane_ttc={0: 2.0}, passing=True)
    assert v3.verdict == "keep" and v3.why == "pass_hold"


# ---------- 窗口按秒（第 6 期 6a） ----------

def test_seconds_windows_are_tick_equivalent_at_50ms():
    """等价锁：50 ms 节拍下 0.15/0.25/0.25 秒与旧 3/5/5 拍逐位相同。"""
    assert (URGENT_TICKS, OPP_TICKS, CANCEL_TICKS) == (3, 5, 5)


def test_confirm_window_counts_time_not_ticks():
    """62.5 ms 节拍：0.25 s 只需 4 拍（3 拍=0.1875 s 不够，4 拍=0.25 s 够）。

    按拍数计时会在低节拍下把窗口拉长，按秒才与真实时间一致。"""
    dt = 0.0625
    s, _ = tick_route(None, _rep(0.0, 0.0), P, dt_s=dt, epoch=0)
    for _ in range(3):
        s, v = tick_route(s, _rep(0.0, 0.0), P, dt_s=dt, epoch=0,
                          request=1.0)
        assert v.verdict == "keep" and v.why == "scoring"
    s, v = tick_route(s, _rep(0.0, 0.0), P, dt_s=dt, epoch=0, request=1.0)
    assert v.verdict == "merge_start" and v.why == "opportunity"


# ---------- 请求证据/动作两位（第 6 期 6c） ----------

PH = replace(P, request_hold=True)


def _tick_hold(state, request, live=True, oid=None, p=PH, dt=DT):
    return tick_route(state, _rep(0.0, 0.0), p, dt_s=dt, epoch=0,
                      request=request, request_live=live, request_oid=oid)


def test_request_hold_freezes_short_dropouts():
    """丢检 1~2 拍冻结证据；恢复后接着攒满门槛（不要求重新来一遍）。"""
    s, _ = _tick_hold(None, None)
    for _ in range(3):
        s, v = _tick_hold(s, 1.0, oid=7)
    assert v.credit == pytest.approx(3 * DT) and v.req_state == "active"
    for _ in range(2):
        s, v = _tick_hold(s, None)
        assert v.credit == pytest.approx(3 * DT)
        assert v.req_state == "paused" and v.why == "paused"
    for _ in range(2):                      # 0.15 + 0.10 = 门槛 0.25
        s, v = _tick_hold(s, 1.0, oid=7)
    assert v.verdict == "merge_start" and v.why == "opportunity"


def test_request_hold_resets_after_gap_exceeds_limit():
    """无读数超过 request_gap_s 清零：断供前后的证据不拼成一次确认。"""
    s, _ = _tick_hold(None, None)
    for _ in range(4):
        s, v = _tick_hold(s, 1.0)
    assert v.credit == pytest.approx(4 * DT)
    for _ in range(4):                  # 0.20 s：仍在上限内
        s, v = _tick_hold(s, None)
    assert v.req_state == "paused" and v.credit == pytest.approx(4 * DT)
    s, v = _tick_hold(s, None)          # 0.25 s：超限
    assert v.req_state == "none" and v.credit == 0.0 and v.why == "no_request"
    assert s.req_lane is None


def test_request_hold_stale_tick_does_not_accumulate_or_start():
    """无当拍读数的拍只冻结：不能加分，也不能起意。"""
    s, _ = _tick_hold(None, None)
    for _ in range(4):
        s, v = _tick_hold(s, 1.0)
    s, v = _tick_hold(s, 1.0, live=False)
    assert v.credit == pytest.approx(4 * DT) and v.req_state == "paused"
    assert v.verdict == "keep"
    s, v = _tick_hold(s, 1.0)               # 0.20 + 0.05 = 门槛 0.25
    assert v.verdict == "merge_start"


def test_request_hold_blocks_start_without_live_source():
    """证据已满但本拍无有效来源：报 no_reach，不把旧机会推成并线。"""
    s = replace(init_state(0), req_lane=1.0,
                req_credit=PH.opportunity_confirm_s, req_side=1,
                req_age_s=0.1, req_pause_s=0.05)
    s, v = _tick_hold(s, 1.0, live=False)
    assert v.verdict == "keep" and v.why == "no_reach"
    assert v.req_state == "paused"
    s, v = _tick_hold(s, 1.0, live=True)
    assert v.verdict == "merge_start" and v.why == "opportunity"


def test_request_hold_resets_on_side_switch():
    """换向是新意图：左右各攒一半不能拼成一次确认。"""
    s, _ = _tick_hold(None, None)
    for _ in range(4):
        s, v = _tick_hold(s, 1.0)
    s, v = _tick_hold(s, -1.0)
    assert v.credit == pytest.approx(DT) and v.req_state == "active"
    assert s.req_lane == -1.0 and s.req_side == -1


def test_request_hold_keeps_credit_across_same_lane_object_switch():
    """同道同向换对象不清零（身份只用于诊断），起意仍需当拍有效来源。"""
    s, _ = _tick_hold(None, None)
    for _ in range(3):
        s, v = _tick_hold(s, 1.0, oid=1)
    s, v = _tick_hold(s, 1.0, oid=2)
    assert v.credit == pytest.approx(4 * DT) and s.req_oid == 2
    assert v.verdict == "keep"
    s, v = _tick_hold(s, 1.0, oid=2)        # 0.20 + 0.05 = 门槛 0.25
    assert v.verdict == "merge_start" and v.why == "opportunity"
    assert v.req_state == "none"          # 起意即清请求状态


def test_request_hold_intent_age_caps_stale_evidence():
    """同一意图超过 request_intent_age_s 清零，即使来源一直可达。"""
    p = replace(PH, opportunity_confirm_s=1.0, request_intent_age_s=0.3)
    s, _ = _tick_hold(None, None, p=p)
    for _ in range(6):
        s, v = _tick_hold(s, 1.0, p=p)
        assert v.verdict == "keep"
    s, v = _tick_hold(s, 1.0, p=p)      # 0.35 s > 0.30 s
    assert v.req_state == "none" and v.credit == 0.0


def test_request_gap_is_time_based_at_62_5ms():
    """62.5 ms 节拍：3 拍无读数=0.1875 s 冻结，4 拍=0.25 s 清零。"""
    dt = 0.0625
    s, _ = _tick_hold(None, None, dt=dt)
    for _ in range(3):
        s, v = _tick_hold(s, 1.0, dt=dt)
    for _ in range(3):
        s, v = _tick_hold(s, None, dt=dt)
        assert v.req_state == "paused"
    s, v = _tick_hold(s, None, dt=dt)
    assert v.req_state == "none" and v.credit == 0.0


# ---------- 承诺道跟随实测道（6g 归因） ----------

PAF = replace(P, anchor_follow=True)
AF_TICKS = round(PAF.anchor_follow_s / DT)      # 0.30 s = 6 拍


def _tick_af(state, report, request=None, p=PAF, dt=DT):
    return tick_route(state, report, p, dt_s=dt, epoch=0, request=request)


def test_anchor_follow_reanchors_after_dwell():
    """实测道可行、偏离承诺道超过 0.6 道持续 0.3 s：静默重锚，不是并线。"""
    rep = _rep(1.4, 1.0)
    s, _ = _tick_af(None, rep)
    for _ in range(AF_TICKS - 1):
        s, v = _tick_af(s, rep)
        assert v.verdict != "anchor_follow" and s.lane_ref == 0.0
    s, v = _tick_af(s, rep)
    assert v.verdict == "anchor_follow" and v.why == "lane_drift"
    assert s.lane_ref == 1.0 and v.lane_ref == 1.0
    assert s.phase == PHASE_KEEP and s.cooldown_left_s == 0.0


def test_anchor_follow_off_keeps_committed_lane():
    """开关关：同一读数下承诺道不动（逐位回旧行为）。"""
    rep = _rep(1.4, 1.0)
    s, _ = _tick(None, rep)
    for _ in range(2 * AF_TICKS):
        s, v = _tick(s, rep)
    assert s.lane_ref == 0.0 and v.verdict != "anchor_follow"


def test_anchor_follow_needs_full_lane_drift():
    """半道多的偏移仍在防抖带内：不重锚（0.55 < 0.6 道）。"""
    rep = _rep(0.55, 1.0)
    s, _ = _tick_af(None, rep)
    for _ in range(2 * AF_TICKS):
        s, v = _tick_af(s, rep)
    assert s.lane_ref == 0.0


def test_anchor_follow_follows_car_lane_even_if_space():
    """车就开在这条道上：走廊单帧判 space 不能阻断跟随（存在即可为落点）。"""
    rep = _rep(-1.3, 0.0, {-1.0: (0.0, "space", False)})
    s, _ = _tick_af(None, rep)
    for _ in range(AF_TICKS):
        s, v = _tick_af(s, rep)
    assert s.lane_ref == -1.0 and v.verdict == "anchor_follow"


def _rep_shoulder(ego):
    """外侧 3 m 肩道：space 且没有有效绿区带（width_margin=None）。"""
    lanes = [CoarseLane(-2.0, 3.0, None, "space", False),
             CoarseLane(-1.0, 24.0, 1.0, None, False),
             CoarseLane(0.0, 24.0, 1.0, None, False),
             CoarseLane(1.0, 24.0, 1.0, None, False),
             CoarseLane(2.0, 3.0, None, "space", False)]
    return CoarseReport(ego, -1.0, -1, 24.0, "keep", tuple(lanes))


def test_anchor_follow_does_not_follow_shoulder():
    """车压到无绿区带的肩道：重锚落到最近的真车道，不跟肩道。"""
    rep = _rep_shoulder(-1.95)
    s, _ = _tick_af(None, rep)
    for _ in range(AF_TICKS):
        s, v = _tick_af(s, rep)
    assert v.verdict == "anchor_follow" and s.lane_ref == -1.0


def test_shoulder_reference_rebuilds_inward():
    """承诺道落在肩道上：单拍就重建到内侧真车道（不朝墙外打）。"""
    rep = _rep_shoulder(-1.95)
    s0 = replace(init_state(0), lane_ref=-2.0, lane_from=-2.0)
    s, v = _tick_af(s0, rep)
    assert v.verdict == "rebuild" and s.lane_ref == -1.0


def test_anchor_follow_keeps_car_lane_through_space_flicker():
    """承诺道就是车所在道时，单帧 space 不触发重建（旧行为是推开到邻道）。"""
    rep = _rep(-1.2, 0.0, {-1.0: (0.0, "space", False)})
    s0 = replace(init_state(0), lane_ref=-1.0, lane_from=-1.0)
    s, v = _tick_af(s0, rep)
    assert s.lane_ref == -1.0 and v.verdict != "rebuild"
    # 开关关：逐位回旧行为——重建到最近的可行道
    s, v = _tick(s0, rep)
    assert v.verdict == "rebuild" and s.lane_ref == 0.0


def test_anchor_follow_does_not_interrupt_merge():
    """并线中车在两道之间是正常过渡：不重锚。"""
    spec = {0.0: (12.0, "car", False), -1.0: (0.0, "space", False)}
    s, _ = _tick_af(None, _rep(0.0, 0.0))
    for _ in range(URGENT_TICKS):
        s, v = _tick_af(s, _rep(0.0, 0.0, spec))
    assert v.verdict == "merge_start" and s.phase == PHASE_MERGING
    s, v = _tick_af(s, _rep(0.0, 0.0, spec))
    assert v.verdict != "anchor_follow" and s.phase == PHASE_MERGING


def test_anchor_follow_skips_escape_into_current_lane():
    """车实际在 −1 道且空闲、承诺道 0 被占：先重锚，不向 −1 道空转一次并线。"""
    rep = _rep(-1.3, -1.0, {0.0: (12.0, "car", False)})
    s, _ = _tick_af(None, rep)
    anchored = False
    for _ in range(AF_TICKS + URGENT_TICKS):
        s, v = _tick_af(s, rep)
        assert v.verdict != "merge_start"
        if v.verdict == "anchor_follow":
            anchored = True
            break
    assert anchored and s.lane_ref == -1.0


def test_anchor_follow_off_still_escapes_into_current_lane():
    """开关关：旧行为原样保留（即使目标是车已经在的 −1 道也起并线）。"""
    rep = _rep(-1.3, -1.0, {0.0: (12.0, "car", False)})
    s, _ = _tick(None, rep)
    for _ in range(URGENT_TICKS):
        s, v = _tick(s, rep)
    assert v.verdict == "merge_start" and v.lane_target == -1.0


def test_anchor_follow_carries_request_credit_across_reanchor():
    """重锚换基准：请求目标道（绝对道号）不变则证据跟着道走，不清零。

    旧契约是重锚同时作废计分；台架坐实清掉的多是攒满在即的证据——
    换基准不改变道的几何语义，同一目标仍落在 ±2 请求跨度内就保留：
    证据位保留、动作位降级（paused），起意仍须下一拍活请求。"""
    rep = _rep(1.4, 1.0)
    s, _ = _tick_af(None, rep)
    for _ in range(AF_TICKS - 1):
        # 请求目标 ±3 不合法（口径 ±2），只累计证据、不起并线
        s, v = _tick_af(s, rep, request=3.0)
        assert v.verdict != "anchor_follow"
    s, v = _tick_af(s, rep, request=3.0)
    assert v.verdict == "anchor_follow" and s.lane_ref == 1.0
    # 目标 3.0 距新参考 1.0 = +2 道，跨度内：证据保留、侧别按新基准重算
    assert s.req_lane == 3.0 and s.req_credit > 0.0 and s.req_side == 1
    assert s.req_state == "paused"          # 动作位降级，起意等活请求
    credit = s.req_credit
    s, v = _tick_af(s, rep, request=2.0)    # 相对新基准 +2 → 同一绝对道
    assert s.req_state == "active"          # 活请求把动作位接回来
    assert s.req_credit == pytest.approx(credit + DT)   # 续上，不重起


def test_anchor_follow_clears_request_out_of_new_span():
    """换基准后目标落在新参考的请求跨度外（绝对差 >2.5 道，±2 也够不着）：清。"""
    rep = _rep(1.4, 1.0)
    s, _ = _tick_af(None, rep)
    for _ in range(AF_TICKS):
        s, v = _tick_af(s, rep, request=-3.0)
    assert v.verdict == "anchor_follow" and s.lane_ref == 1.0
    assert s.req_credit == 0.0 and s.req_state == "none"


def test_drift_band_does_not_block_opportunity():
    """车压在 0.5~0.6 道边界带：既不重锚，也不拦机会并线。

    此前 `_drifted_free` 用半道阈值、重锚用 0.6 道，这一带里请求计分涨到
    0.53 也起不了意（183241 局机会并线 0 段的直接原因）。"""
    rep = _rep(0.55, 0.0)
    s, _ = _tick_af(None, rep)
    v = None
    for _ in range(OPP_TICKS):
        s, v = _tick_af(s, rep, request=1.0)
    assert v.verdict == "merge_start" and v.why == "opportunity"
    assert s.lane_ref == 1.0 and s.phase == PHASE_MERGING
