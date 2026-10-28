# -*- coding: utf-8 -*-
"""四层选择器接线回归锁（DecisionEngine + Corridor，2026-10-18 走廊重建）。

锁定：layer 开关生效后，输出目标与层一一对应（不再有加性场/轨迹否决），
中心带内不修正、带外只去带边。
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
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, PerceptionHealth, TrackedTarget, WorldObservation)

LW = 3.38
_CFG_BASE = _read_decision(DECISION_FILE)
CFG = _CFG_BASE


def _corridor(left=-6.76, right=6.76, *, executed=0.0):
    """走廊带在自车系；路心随 executed 平移（与 road_offset 契约一致）。"""
    c = -executed * LW
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(c + left, "measured"),
                     BandBoundary(c + right, "measured"))
        for i in range(9))
    return Corridor(bands, -executed, (right - left) / LW, "pair", "ok")


def _obs(fid, *, groups=(), targets=(), presence=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, presence, False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=20.0)


def _group(fid, gid=1, n=3, x=0.4, cy=470, rel=2.5, conf=0.85):
    members = tuple(TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n))
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in members),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy,
                  cy_max=cy + 3 * (n - 1), conf_min=conf,
                  partial_observation=False, first_seen_fid=fid,
                  last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, members


def test_layered_coin_targets_coin_line():
    eng = DecisionEngine(CFG)
    g, ms = _group(1)
    out = None
    for fid in (1, 2, 3):
        out = eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                         executed_lane=0.0, corridor=_corridor())
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
    assert out is not None
    assert out.reason == "sel:coin"
    assert out.maneuver is True
    assert eng.selection_report is not None
    assert eng.selection_report.layer == 3
    # 币组心 0.4 道（仍在本道跨度内）→ 选择目标约 0.4 道（币线，不是路心 0）；
    # 出跨度的目标才转路线请求，同道偏一侧吃币不受闸门影响
    assert eng.selection_report.target_x_m / LW == pytest.approx(0.4, abs=0.1)


def test_layered_reward_wins_over_coin():
    eng = DecisionEngine(CFG)
    g, ms = _group(1)
    bonus = TrackedTarget(
        id=900, kind="bonus", x_lane=0.1, x_sigma=0.1, cy=430, w=30, h=30,
        conf=0.99, rel_approach=2.5, first_seen_fid=1, last_seen_fid=4,
        validity_until_fid=20)
    out = None
    for fid in (1, 2, 3, 4):
        out = eng.update(_obs(fid, groups=(g,), targets=(bonus,)),
                         0.05, executed_lane=0.0, corridor=_corridor())
    assert out is not None
    assert out.reason == "sel:reward"
    assert eng.selection_report.layer == 1
    assert eng.selection_report.target_x_m / LW == pytest.approx(0.1, abs=0.1)


def test_layered_center_band_holds_current_lane():
    eng = DecisionEngine(CFG)
    out = eng.update(_obs(1), 0.05, executed_lane=0.1,
                     corridor=_corridor(executed=0.1))
    assert out.reason == "sel:center"
    assert eng.selection_report.layer == 4
    assert out.x_target == pytest.approx(0.1, abs=0.02)   # 区间内不修正
    assert out.maneuver is False


def test_layered_center_outside_band_targets_edge_not_center():
    half = (0.5 - CFG.selection.ego_half_lane
            - CFG.selection.lane_clearance_lane)
    eng = DecisionEngine(CFG)
    out = eng.update(_obs(1), 0.05, executed_lane=1.5,
                     corridor=_corridor(executed=1.5))
    assert out.reason == "sel:center"
    assert out.x_target == pytest.approx(half, abs=0.02)   # 最近边界
    assert out.maneuver is True


def test_layered_no_corridor_holds():
    eng = DecisionEngine(CFG)
    out = eng.update(_obs(1), 0.05, executed_lane=0.4, corridor=None)
    assert out.reason == "sel:hold:no_road"
    assert out.x_target == pytest.approx(0.4, abs=0.02)


def test_road_frame_origin_is_master_not_band_mid():
    """车道原点=供数门 master 路心，不是分带不对称中点：带中心相对原点
    保留（只供宽度/包络），自车横位输出为「相对 master 的执行位」——
    旧口径用带中点当原点，会让选择目标与 planner 路心系差一个带偏。"""
    eng = DecisionEngine(CFG)
    master = -0.5                       # master 在自车左侧 0.5 道
    bands = tuple(
        CorridorBand(-2.0 + i * 3.0, -2.0 + (i + 1) * 3.0,
                     BandBoundary(0.0, "measured"),
                     BandBoundary(4.0, "measured"))
        for i in range(9))              # 带中心在自车右侧 2.0m（不对称）
    eng._corridor = Corridor(bands, master, 4.0 / LW, "pair", "ok")
    road, ego_m, origin = eng._road_frame(0.7, 1)
    assert ego_m == pytest.approx(-master * LW)          # 相对 master 的观察位
    assert origin == pytest.approx((master + 0.7) * LW)   # master 在带平移帧里
    assert road.center_at(0.0) == pytest.approx(2.0 - master * LW)


def test_coarse_control_takes_center_layer(monkeypatch):
    """粗层接控制：只接管 L4，目标=粗层选定车道（-1 道）。

    这里锁的是**路线闸关**时的接管链（本层自选道直接进控制）；闸开时接控制
    的是路线承诺道，锁在路线层测试文件里。被喂的报告候选集为空，承诺层会按
    「无车道级证据」如实弃权，所以本测必须显式关闸，否则测的是另一条链。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    from maaracing_master.plugins.speedrush.coarse import CoarseReport
    monkeypatch.setattr(
        dmod, "choose_lane",
        lambda *a, **k: CoarseReport(0.0, -1.0, -1, 24.0, "space", ()))
    cfg = replace(CFG, route=replace(CFG.route, control=False))
    eng = DecisionEngine(cfg)
    out = eng.update(_obs(1), 0.05, executed_lane=0.0, corridor=_corridor())
    half = (0.5 - CFG.selection.ego_half_lane
            - CFG.selection.lane_clearance_lane)
    assert out.reason == "sel:center:coarse"
    assert out.x_target == pytest.approx(-1.0 + half, abs=0.02)
    assert out.maneuver is True


def test_coarse_control_gate_off_keeps_pure_center(monkeypatch):
    """control=false 退回影子期：粗层不参与选择。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    from maaracing_master.plugins.speedrush.coarse import CoarseReport
    monkeypatch.setattr(
        dmod, "choose_lane",
        lambda *a, **k: CoarseReport(0.0, -1.0, -1, 24.0, "space", ()))
    cfg = replace(CFG, coarse=replace(CFG.coarse, control=False))
    out = DecisionEngine(cfg).update(_obs(1), 0.05, executed_lane=0.0,
                                     corridor=_corridor())
    assert out.reason == "sel:center"
    assert out.x_target == pytest.approx(0.0, abs=0.02)


def test_adjacent_coin_line_requests_then_merges():
    """接线锁：邻道币组持续出现 → 目标先待在本道跨度内（贴请求侧边界准备），
    同侧请求计满分满后由**路线**起意并线（选择层自己不跨线）。"""
    eng = DecisionEngine(CFG)
    g, ms = _group(1, x=1.0)                    # 币线心在右邻道（1.0 道）
    seen = []
    for fid in range(1, 40):
        out = eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                         executed_lane=0.0, corridor=_corridor())
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
        ms = tuple(replace(m, first_seen_fid=1, last_seen_fid=fid,
                           validity_until_fid=fid + 8) for m in ms)
        rt = eng.route_verdict
        seen.append((out.reason, rt.verdict, rt.why, rt.credit,
                     eng.selection_report.lane_request,
                     eng.selection_report.target_x_m / LW))
    req_ticks = [s for s in seen if s[4] == 1.0]
    assert req_ticks, [s[0] for s in seen[:8]]
    fired_i = next(i for i, s in enumerate(seen) if s[1] == "merge_start")
    # 起意之前目标从未越过本道外缘（0.5 道）——不会停在两道中间
    assert max(abs(s[5]) for s in seen[:fired_i]) <= 0.5
    fired = seen[fired_i]
    assert fired[2] == "opportunity"
    assert fired[3] >= CFG.route.opportunity_confirm_ticks
    # 起意之后目标才是那条道（承诺道区间边界：1.0 − 0.17）
    assert seen[-1][0] == "sel:commit" and seen[-1][5] == pytest.approx(
        1.0 - (0.5 - CFG.selection.ego_half_lane
               - CFG.selection.lane_clearance_lane), abs=0.02)


# ---------- 机会换道：两跳真实承诺链（契约换算 + 信用生命周期） ----------

def _flat_coarse(ego: float) -> CoarseReport:
    """五条道全「确认空闲」的原始粗层报告——只用来把「车到位」固定住。"""
    return CoarseReport(ego, ego, 0, 24.0, "keep",
                        tuple(CoarseLane(k, 24.0, 1.0, None, False)
                              for k in (-2.0, -1.0, 0.0, 1.0, 2.0)))


def test_second_hop_resolves_relative_request_on_a_real_commit(monkeypatch):
    """接线锁：第一跳完成后，相对 +1 的请求必须落在**绝对 +2**。

    单元测试摆 `lane_ref=1.0` 只锁得住换算本身，锁不住生产接线里的四件事：
    完成拍的状态是否干净、请求延迟一拍消费时参照系是否还是生成拍的承诺道、
    并线期会不会替下一跳攒进度、断供拍是否把计分清零。选择层与路线层全程
    真实，只有粗层横位用假报告与 `executed` 同值渐进（一步跳满一道会触发
    尺子换纪，那不是本锁要验的东西）。"""
    from maaracing_master.plugins.speedrush import decision as dmod
    n_confirm = CFG.route.opportunity_confirm_ticks
    ego = [0.0]
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _flat_coarse(ego[0]))
    eng = DecisionEngine(CFG)
    g, ms = _group(1, x=1.0)                 # 币线心在右邻道
    fid = 0

    def feed(n, executed, *, road_ok=True):
        nonlocal fid
        out = []
        for _ in range(n):
            fid += 1
            ego[0] = executed
            gg = replace(g, first_seen_fid=1, last_seen_fid=fid,
                         validity_until_fid=fid + 8)
            mms = tuple(replace(m, first_seen_fid=1, last_seen_fid=fid,
                                validity_until_fid=fid + 8) for m in ms)
            eng.update(_obs(fid, groups=(gg,), targets=mms), 0.05,
                       executed_lane=executed,
                       corridor=_corridor(executed=executed) if road_ok else None)
            rt = eng.route_verdict
            out.append((rt.verdict, rt.why, rt.lane_ref, rt.credit,
                        None if eng.selection_report is None
                        else eng.selection_report.lane_request))
        return out

    hop1 = feed(15, 0.0)
    i = [h[0] for h in hop1].index("merge_start")
    assert hop1[i][1] == "opportunity" and hop1[i][2] == pytest.approx(1.0)
    arrived = None
    for ex in (0.3, 0.6, 0.9, 1.0):          # 渐进横移：车真的挪了一道
        hop = feed(2, ex)
        if "finish" in [h[0] for h in hop]:
            arrived = hop[[h[0] for h in hop].index("finish")]
            break
    assert arrived is not None and arrived[2] == pytest.approx(1.0)
    pre = feed(2, 1.0)                       # 承诺道已是 1，机会仍在隔壁一道
    assert [h[0] for h in pre] == ["keep", "keep"]
    assert [h[4] for h in pre] == [1, 1]     # 选择层出的始终是相对偏移
    dead = feed(1, 1.0, road_ok=False)[0]
    assert dead[0] == "needs_init" and dead[3] == 0.0
    hop2 = feed(n_confirm + 3, 1.0)
    j = [h[0] for h in hop2].index("merge_start")
    # 断供后第 7 拍才起意 = 攒满 6 分 + 请求延迟一拍消费（恢复首拍消费的是断供
    # 拍存下的"无请求"）。不清零的话 pre 那 2 分会一路带过来，第 5 拍就起意。
    assert j + 1 == n_confirm + 1
    assert hop2[j][2] == pytest.approx(2.0)  # 绝对 +2，不是 1 更不是 0
