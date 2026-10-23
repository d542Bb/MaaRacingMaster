# -*- coding: utf-8 -*-
"""路线承诺层（2026-10-23 V1）的回归锁。

锁两件事：**定案规则**（三路径、承诺期、冷却、占用三态、整段并线检查、换纪
作废）与**唯一控制接口**（闸开时原始粗层翻面不得移动目标；闸关时逐位回今天）。

场景距离按 decision.json 起值反推（窗 24 m、代理 45 m/s → 窗长仅 0.53 s；
收益门槛 4 m；紧急阈 0.35 s ≈ 15.75 m；1 道保守完成时间 3 s）——参数与场景
是一对契约面，改数值会红，这是刻意的。
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
    commit_s=CFG.route.commit_s,
    switch_gain_m=CFG.coarse.switch_margin_m,
    t_meas_per_lane_s=CFG.route.t_meas_per_lane_s,
    v_lat_max_lane_s=CFG.planner.v_lat_max,
    proxy_speed_mps=CFG.coarse.range_speed_mps,
    t_urgent_s=CFG.route.t_urgent_s,
    cancel_unsafe_ticks=CFG.route.cancel_unsafe_ticks,
    cancel_cooldown_s=CFG.route.cancel_cooldown_s,
    finish_tol_lane=CFG.selection.center_band_lane,
    finish_v_tol_lane_s=CFG.route.finish_v_tol_lane_s,
    merge_max_s=CFG.route.merge_max_s)
CLEAR = (24.0, None, False)               # 确认空闲（净空=窗长）
WINDOW_M = CLEAR[0]
URGENT_M = P.t_urgent_s * P.proxy_speed_mps        # ≈15.8 m：紧急阈对应距离
MOTIVATE_M = 18.0                                  # 0.40 s：有动机、还不算紧急
AGED_TICKS = int(P.commit_s / DT) + 2              # 过承诺期所需拍数


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
    """冷启动首拍（只建立身份）之后的一拍判定——承诺期还满着。"""
    s, _ = _tick(None, report, epoch)
    return _tick(s, report, epoch)


def _aged(report, epoch=0):
    """先用无可换读数（全空闲=零收益）把承诺期喂空，再喂当拍读数判一拍。

    直接重复喂同一份"有动机"的读数会在承诺期一到就定案，之后每拍都是并线中，
    就量不到"普通换道判据"本身了。"""
    s, _ = _tick(None, _rep(0.0, 0.0), epoch)
    for _ in range(AGED_TICKS):
        s, _ = _tick(s, _rep(0.0, 0.0), epoch)
    return _tick(s, report, epoch)


def _in_merge(epoch=0):
    """用逃生路径推进成"正在并线"（承诺期与收益门槛都不参与逃生定案）。"""
    spec = {0.0: (6.0, "car", False)}
    s, _ = _tick(None, _rep(0.0, 0.0), epoch)
    return _tick(s, _rep(0.0, 1.0, spec), epoch)


# ---------- 定案规则：三条互斥路径 ----------

def test_normal_merge_grants_when_target_lane_has_time():
    """正常场景（回归锁必须含过门用例）：现任道 18 m 处确认有车（净空差 6 m
    过 4 m 门槛、0.40 s 未进紧急阈），目标道窗内确认空闲 → 普通换道放行。"""
    s, v = _aged(_rep(0.0, 1.0, {0.0: (MOTIVATE_M, "car", False)}))
    assert v.verdict == "merge_start" and v.why == "gain"
    assert v.lane_ref == 1.0 and v.phase == PHASE_MERGING
    # 放行判据用保守完成时间（3 s/道），不是最快可能用时（0.4 s/道）
    assert v.t_need_s == pytest.approx(P.t_meas_per_lane_s)
    assert s.decided_t == pytest.approx(s.elapsed_s)       # 定案时刻已刷新


def test_commitment_period_holds_the_better_candidate():
    """承诺期内不动：到期只解锁重新评估。1.5 s 内 +1 道再空也不起意。"""
    v = _first(_rep(0.0, 1.0, {0.0: (MOTIVATE_M, "car", False)}))[1]
    assert v.verdict == "keep" and v.why == "commit_pending"
    assert v.lane_ref == 0.0


def test_merging_ignores_other_candidates():
    """并线中不换主意：目标道走不下、别侧更空，也不翻面重选（推迟=先做完）。"""
    spec = {0.0: (MOTIVATE_M, "car", False), 1.0: (0.0, "space", False)}
    s, v = _aged(_rep(0.0, 1.0, spec))            # +1 走不下 → 去 −1
    assert v.lane_ref == -1.0 and v.phase == PHASE_MERGING
    s2, v2 = _tick(s, _rep(-1.0, 1.0), 0)         # 现在 +1 全空
    assert v2.phase == PHASE_MERGING and v2.lane_ref == -1.0
    assert v2.why == "merging"


def test_zero_gain_never_grants_even_after_commitment():
    """锁 4：收益不足门槛=0 次放行，即使承诺期已过。

    20261023 实测 109 次翻面里 75 次净空改善 ≤4 m、中位 0 m——正是这一门拦的
    那类「换了个不更好的理由」。"""
    s, v = _aged(_rep(0.0, 1.0, {0.0: (WINDOW_M, "car", False)}))   # 0.53 s
    assert v.verdict == "keep" and v.why == "no_gain"
    assert v.lane_ref == 0.0                       # 时间过了也不动


def test_far_flash_does_not_start_a_merge():
    """锁 3（远端闪检）：23 m 外的车出现又消失，普通换道 0 次放行。"""
    s, v = _aged(_rep(0.0, 1.0, {0.0: (23.0, "car", False)}))       # 差 1 m
    assert v.verdict == "keep" and v.why == "no_gain"
    s, v = _tick(s, _rep(0.0, 0.0), 0)             # 消失
    assert v.verdict == "keep" and v.phase == PHASE_KEEP and v.lane_ref == 0.0


def test_urgent_escape_breaks_commitment_and_gain():
    """紧急逃生：现任道 6 m 处确认有车（0.13 s < 阈）→ 立即去最近的空闲道，
    承诺期与收益门槛都不拦。"""
    spec = {0.0: (6.0, "car", False), 1.0: (6.0, None, False)}
    s, v = _first(_rep(0.0, 0.0, spec))            # 承诺期未满
    assert v.verdict == "merge_start" and v.why == "urgent"
    assert v.lane_ref == 1.0 and v.evidence_m == pytest.approx(6.0)


def test_urgent_without_feasible_alternative_keeps_reference_lane():
    """锁 8：紧急但两侧都进不去（横移途中会穿到已知占用前面）→ `urgent_no_alt`，
    参考道不动，证据距离入 trace（V3 恢复模式要统计的事件）。"""
    spec = {k: (10.0, "car", False) for k in (-2.0, -1.0, 1.0, 2.0)}
    spec[0.0] = (6.0, "car", False)
    s, v = _first(_rep(0.0, 0.0, spec))
    assert v.verdict == "urgent_no_alt" and v.lane_ref == 0.0
    assert v.evidence_m == pytest.approx(6.0)


def test_unknown_target_lane_is_never_treated_as_clear():
    """锁 5 之一：目标道"无法确认"（有车没时刻）不得被当作确认空闲起意。

    +1 道读数最空（24 m）但标 unknown，−1 道 22 m 确认空闲 → 放行的是 −1。"""
    spec = {0.0: (16.0, "car", False),             # 0.356 s：不紧急
            1.0: (24.0, "unknown_car", True),
            -1.0: (22.0, None, False),
            2.0: (10.0, "car", False), -2.0: (10.0, "car", False)}
    s, v = _aged(_rep(0.0, 1.0, spec))
    assert v.verdict == "merge_start" and v.lane_ref == -1.0


def test_unknown_on_current_lane_is_not_an_urgent_reason():
    """锁 5 之二：现任道"无法确认"不单独构成紧急理由——紧急要正面证据。

    2 m 处的无法确认若被当成紧急会突破承诺期直接改道；实际走普通路径，
    于是被承诺期按住。"""
    spec = {0.0: (2.0, "unknown_car", True), 1.0: (0.0, "space", False)}
    s, v = _first(_rep(0.0, 0.0, spec))
    assert v.verdict == "keep" and v.why == "commit_pending"


def test_t_lower_rejects_and_t_grant_delays():
    """锁 6：`T_lower` 判"明显不可能"→ 否决（no_time）；`T_grant` 不足 → 推迟
    （window_short：参考道不动，等下一个窗口，不翻面重选）。"""
    # 2 道：最快也要 0.8 s；目标道车在窗末（0.53 s）→ 明显不可能
    spec = {0.0: (MOTIVATE_M, "car", False), 1.0: (0.0, "space", False),
            -1.0: (0.0, "space", False), 2.0: (24.0, "car", False),
            -2.0: (24.0, "car", False)}
    s, v = _aged(_rep(0.0, 2.0, spec))
    assert v.verdict == "keep" and v.why == "no_time" and v.lane_ref == 0.0
    assert v.t_need_s == pytest.approx(2.0 / P.v_lat_max_lane_s)
    # 1 道：目标道车在 20.5 m（0.456 s）过下界 0.4 s、不到保守值 3 s → 推迟
    spec2 = {0.0: (16.0, "car", False), 1.0: (20.5, "car", False),
             -1.0: (10.0, "car", False), 2.0: (10.0, "car", False),
             -2.0: (10.0, "car", False)}
    s2, v2 = _aged(_rep(0.0, 1.0, spec2))
    assert v2.verdict == "keep" and v2.why == "window_short"
    assert v2.lane_ref == 0.0 and v2.t_need_s == pytest.approx(
        P.t_meas_per_lane_s)


# ---------- 并线生命周期 ----------

def test_merge_finishes_on_position_and_velocity():
    """完成判据=位置到位**且**横速落稳：只看位置会带着横速"完成"（过冲）。"""
    s, v = _in_merge()
    assert v.phase == PHASE_MERGING and v.lane_ref == 1.0
    s2, v2 = _tick(s, _rep(0.9, 1.0), 0)           # 横位 0→0.9：横速 18 道/s
    assert v2.verdict == "keep" and v2.why == "merging"    # 位置近也不落定
    s3, v3 = _tick(s2, _rep(0.9, 1.0), 0)          # 速率归零、位差 0.1 < 带宽
    assert v3.verdict == "finish" and v3.why == "arrived"
    assert v3.lane_ref == 1.0 and v3.phase == PHASE_KEEP


def test_merge_cancel_needs_consecutive_evidence_then_cooldown():
    """取消：目标道连续确认占用（帧计数，单帧不过）→ 按实测所在道落回并进入
    冷却；冷却期内普通换道一律不放行（只留紧急逃生）。"""
    s, v = _in_merge()
    bad = {1.0: (10.0, "car", False)}
    s, v = _tick(s, _rep(0.5, 1.0, bad), 0)        # 单帧野值不撤
    assert v.verdict == "keep" and v.why == "merging" and v.lane_ref == 1.0
    for _ in range(P.cancel_unsafe_ticks - 1):
        s, v = _tick(s, _rep(0.5, 1.0, bad), 0)
    assert v.verdict == "cancel" and v.lane_ref == 0.0     # 半道落回
    assert v.cooldown_left_s == pytest.approx(P.cancel_cooldown_s)
    # 承诺期已过（1.5 s < 冷却 2.0 s）：此刻只剩冷却门在拦
    ok = {0.0: (MOTIVATE_M, "car", False)}          # 收益够、时间够，只等门开
    for _ in range(AGED_TICKS):
        s, v = _tick(s, _rep(0.0, 1.0, ok), 0)
    assert v.verdict == "keep" and v.why == "cooldown"


def test_merge_timeout_lands_on_measured_lane():
    """并线超时：车可能停在半道，落回**实测所在道**而不是硬判目标道完成。"""
    s, v = _in_merge()
    for _ in range(200):                            # 卡在 0.4 道，横速 0
        s, v = _tick(s, _rep(0.4, 1.0), 0)
        if v.verdict == "finish":
            break
    assert v.why == "merge_timeout"
    assert v.lane_ref == 0.0                       # round(0.4)=0：不钉在 1 道
    assert s.phase == PHASE_KEEP


def test_cold_start_holds_centerline_not_nearest_lane():
    """冷启动参考道=路心道（0）：车偏在两道之间时，启动拍不平白压向最近一条道
    ——V1 的差异只该来自"道号被承诺门控"，不该来自启动瞬间的横向动作。"""
    s, v = _tick(None, _rep(1.45, 1.0), 0)
    assert v.verdict == "init" and v.lane_ref == 0.0
    assert s.lane_ref == 0.0


def test_missing_anchor_yields_centerline_not_stale_lane():
    """缺锚点拍：已承诺的道号作废、参考道交回路心道，控制面给 None（纯中心带）。"""
    s, _ = _tick(None, _rep(1.0, 1.0), epoch=5)
    s = replace(s, lane_ref=2.0, lane_from=2.0)      # 造一个已承诺的外道
    s, v = _tick(s, _rep(2.0, 2.0), epoch=None)
    assert v.verdict == "needs_init" and v.why == "no_anchor"
    assert v.lane_ref == 0.0 and s.epoch is None


# ---------- 参考系身份（锚点纪元） ----------

def test_epoch_change_voids_route_to_measured_lane():
    """锁 7：换纪=参考系重基，并线中的路线作废并降级 KEEP(实测所在道)。"""
    s, v = _in_merge(epoch=3)
    assert v.phase == PHASE_MERGING
    s, v = _tick(s, _rep(0.4, 1.0), epoch=4)
    assert v.verdict == "epoch_reset" and v.phase == PHASE_KEEP
    assert v.lane_ref == 0.0 and s.epoch == 4


def test_reference_lane_outside_candidates_rebuilds():
    """命令位落到候选集外（弯道把车甩出 ±2 道）：夹回候选域重建，不钉死。"""
    s, _ = _tick(None, _rep(0.0, 0.0), 0)
    s = replace(s, lane_ref=5.0, lane_from=5.0)    # 造一个越界参考道
    s, v = _tick(s, _rep(1.9, 1.0), 0)
    assert v.verdict == "rebuild" and v.why == "lane_out_of_range"
    assert v.lane_ref == 2.0 and s.phase == PHASE_KEEP


def test_no_evidence_frame_freezes_decision_not_clock():
    """无路拍：不推进定案、也不拿旧坐标硬撑（needs_init），时钟继续走。"""
    s = init_state(0)
    assert s.lane_ref == 0.0 and s.phase == PHASE_KEEP
    s2, v = tick_route(s, None, P, dt_s=DT, epoch=0)
    assert v.verdict == "needs_init" and v.lane_ref == 0.0
    assert s2.elapsed_s == pytest.approx(s.elapsed_s + DT)
    assert s2.lane_ref == s.lane_ref


def test_anchor_epoch_increments_only_on_confirmed_rebase():
    """边界 C 生产侧：纪元只在**确认采信的超容差跳变**那一拍 +1。

    被拒的跳变拍（不供数）、超 OFF_MAX 的垃圾拍、之后稳态拍都不计数——按坏帧
    计数的话路线承诺逐帧作废，换纪判据就名存实亡。跳变先要过中值窗跟进 +
    连续确认，所以序列是「稳态→拒一拍→采信」。"""
    import maaracing_master.plugins.speedrush.module as smod
    o = smod._EgoRoadObserver()

    def feed(l, r, t):
        o._frame_gate_used = False        # update() 每拍开头的复位
        c = o._abs_tick(l, r, t, None, "region", None, None, None)
        return (None if c is None else c.anchor_epoch), o.last.get("reject")

    assert [feed(-1.0, 1.0, 1000.0 + i * 0.05)[0] for i in range(4)] == [0] * 4
    seq = [feed(-2.0, 0.0, 1000.25 + i * 0.05) for i in range(4)]
    assert any(rej == "jump" for _, rej in seq)          # 先有被拒的跳变拍
    assert [e for e, _ in seq if e is not None] == [0, 1, 1]   # 只在采信拍 +1
    assert feed(-6.0, -2.0, 1000.60)[1] == "off_max"     # 垃圾拍不供数
    assert [feed(-2.0, 0.0, 1000.65 + i * 0.05)[0] for i in range(3)] == [1] * 3


# ---------- 唯一控制接口（接线面） ----------

def _corridor(epoch=0):
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(-6.76, "measured"),
                     BandBoundary(6.76, "measured"))
        for i in range(9))
    return Corridor(bands, 0.0, 13.52 / LW, "pair", "ok", anchor_epoch=epoch)


def _obs(fid):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, False, False),
        boundary=None, targets=(), far_targets=(), coin_groups=(),
        fid_rate_hz=20.0)


# L4 带外只去近侧带边：中心 +1 道=3.38 m，带宽 0.3 道 → 目标 2.366 m
TARGET_1_LANE = 0.7 * LW


def test_gate_off_follows_raw_coarse_flip(monkeypatch):
    """边界 A：`route.control=false` 时接控制的道号逐位是粗层自选道，选择目标
    随它移动（今天的行为）；路线照样逐拍定案，但只是影子对照面。"""
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
    """锁 2：闸开且路线未定案时，原始粗层翻面不得移动目标——承诺道与选择层
    目标逐位不动（没有这条锁，路线对象就只是多一份状态）。

    候选道读数全同（都确认空闲），翻面纯粹是粗层字典序产物——正是实测
    75/109 零收益翻面的形态。"""
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
    for fid in range(2, 45):        # 翻面后 2 秒：承诺期也过了，仍是零收益
        eng.update(_obs(fid), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.coarse_report.lane == 1.0             # 原始读数确实翻了
    assert eng.coarse_lane == 0.0                    # 接控制的没翻
    assert eng.selection_report.target_x_m == pytest.approx(before)
    assert eng.route_verdict.why == "no_gain"


def test_gate_on_granted_merge_moves_target(monkeypatch):
    """正常场景：闸开且路线定案（紧急逃生）后，承诺道与选择目标一起挪到新道。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    spec = {0.0: (6.0, "car", False)}
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, 1.0, spec))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    for fid in range(1, 9):   # 过选择层目标驻留窗（0.2 s）
        eng.update(_obs(fid), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.route_verdict.phase == PHASE_MERGING
    assert eng.route_verdict.lane_ref == 1.0
    assert eng.coarse_lane == 1.0
    assert eng.selection_report.target_x_m == pytest.approx(TARGET_1_LANE,
                                                            abs=0.02)


def test_missing_anchor_rebuilds_instead_of_substituting(monkeypatch):
    """边界 C（锁 9）：缺锚点拍不回退旧坐标——按实测重建（init/no_anchor），
    路线侧不自行膨胀纪元；锚点回来首拍不跟旧候选走。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    holder = [0.0]
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _rep(0.0, holder[0]))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    eng.update(_obs(1), DT, executed_lane=0.0, corridor=_corridor(epoch=7))
    assert eng.route_verdict.epoch == 7
    assert eng.coarse_lane == 0.0         # 冷启动参考道=路心道
    for fid in (2, 3, 4):                 # 连续缺锚点拍（供数门无路心）
        eng.update(_obs(fid), DT, executed_lane=0.0,
                   corridor=_corridor(epoch=None))
        assert eng.route_verdict.verdict == "needs_init"
        assert eng.route_verdict.why == "no_anchor"
        assert eng.route_verdict.epoch is None
    holder[0] = 1.0                       # 影子臂翻了面
    eng.update(_obs(5), DT, executed_lane=0.0, corridor=_corridor(epoch=8))
    assert eng.route_verdict.verdict == "epoch_reset"
    assert eng.coarse_lane == 0.0         # 不跟旧候选走


def test_no_road_frame_gives_no_committed_lane(monkeypatch):
    """无路拍：接控制的道号给 None（退回纯中心带），不拿上一拍承诺值冒充。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    monkeypatch.setattr(dmod, "choose_lane", lambda *a, **k: _rep(0.0, 0.0))
    cfg = replace(CFG, route=replace(CFG.route, control=True))
    eng = DecisionEngine(cfg)
    eng.update(_obs(1), DT, executed_lane=0.0, corridor=_corridor())
    assert eng.coarse_lane == 0.0
    out = eng.update(_obs(2), DT, executed_lane=0.0, corridor=None)
    assert out.reason == "sel:hold:no_road"
    assert eng.coarse_lane is None
    assert eng.route_verdict.verdict == "needs_init"
