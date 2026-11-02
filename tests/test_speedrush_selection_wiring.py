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

LW = 3.5
_CFG_BASE = _read_decision(DECISION_FILE)
CFG = _CFG_BASE


def _corridor(left=-7.0, right=7.0, *, executed=0.0):
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


def test_preposition_params_flow_from_config():
    """接线锁：预排参数从决策配置进选择器（防回归成死配置）——构造处漏传
    时选择器拿到的是 dataclass 默认值，与配置真源脱钩（只改配置不生效）。"""
    eng = DecisionEngine(CFG)
    p = eng.selector.p
    s = CFG.selection
    assert p.preposition_enabled == s.preposition_enabled
    assert p.preposition_horizon_s == s.preposition_horizon_s == 10.0
    assert p.preposition_max_lane == s.preposition_max_lane
    assert p.preposition_bias_lane == s.preposition_bias_lane
    # 影子选择器与生产共用同一份参数（预排行为逐位一致）
    assert eng._selector_shadow.p.preposition_horizon_s == 10.0


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
    # 币组心 0.4 道：目标按车身带收到 0.4−0.27=0.13 道（不对币心）；
    # 出跨度的目标才转路线请求，同道偏一侧吃币不受闸门影响
    assert eng.selection_report.target_x_m / LW == pytest.approx(0.13, abs=0.1)


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
    # 币线心在右邻道（1.0 道）；闭接近速度 1.5 → 到站约 2.6s。旧夹具用
    # 2.5（到站 1.55s）：按执行层真实横移能力，1.0 道此刻已来不及（需
    # 1.58s+反应），请求不该发——夹具本身要改成「够得着」的到达时间。
    g, ms = _group(1, x=1.0, rel=1.5)
    seen = []
    for fid in range(1, 40):
        # 币组本拍可见（seen 判据是 last_seen_fid == frame_id）：读数位先于
        # 决策更新；请求保持打开后，过期的 last_seen_fid 会让 req_live 恒 False。
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
        ms = tuple(replace(m, first_seen_fid=1, last_seen_fid=fid,
                           validity_until_fid=fid + 8) for m in ms)
        out = eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                         executed_lane=0.0, corridor=_corridor())
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
    assert fired[3] + 1e-9 >= CFG.route.opportunity_confirm_s
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
    n_confirm = round(CFG.route.opportunity_confirm_s / 0.05)
    ego = [0.0]
    monkeypatch.setattr(dmod, "choose_lane",
                        lambda *a, **k: _flat_coarse(ego[0]))
    eng = DecisionEngine(CFG)
    g, ms = _group(1, x=1.0, rel=1.5)        # 右邻道币线，到站约 2.6s（够得着）
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


def test_selection_debug_carries_identity_and_cone_slack():
    """观测面接线：机会的组号与光锥余量随 selection_debug 落列，场景对象
    同时带组号与「本拍见过」；这些列不参与任何判定。"""
    eng = DecisionEngine(CFG)
    g, ms = _group(1, x=0.4, rel=2.5)
    for fid in (1, 2, 3):
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
        ms = tuple(replace(m, first_seen_fid=1, last_seen_fid=fid,
                           validity_until_fid=fid + 8) for m in ms)
        eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                   executed_lane=0.0, corridor=_corridor())
    dbg = eng.selection_debug
    assert dbg is not None and dbg["coin"]
    oid, slack, slack_sm = dbg["opp"]["coin"][0]  # 平滑列与原始列同值（未平滑对象/无身份回退）
    assert oid == 1
    assert slack is not None and slack > 0.0    # 良候选：到站预算为正
    assert slack_sm == slack or slack_sm is not None
    coin = [o for o in eng.scene.objs if o.kind == "coin"][0]
    assert coin.id == 1 and coin.seen is True


def test_engine_exposes_openness_and_anchor():
    """第 4 期观测面接线：开度与 60m 锚随决策拍落列，不参与选择。"""
    eng = DecisionEngine(CFG)
    g, ms = _group(1, x=0.4, rel=2.5)
    trig = None
    for fid in (1, 2, 3, 4, 5):
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
        ms = tuple(replace(m, first_seen_fid=1, last_seen_fid=fid,
                           validity_until_fid=fid + 8) for m in ms)
        eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                   executed_lane=0.0, corridor=_corridor())
        trig = eng.trigger_trace()
    op = eng.openness_trace()
    assert op is not None and op["lvl"] == "open"
    an = eng.anchor_trace()
    assert an is not None and an["k"] == "coin" and an["id"] == 1
    assert eng.selection_report.layer == 3
    hs = eng.horizon_trace()
    assert hs is not None and hs["h"] == 3.0
    assert trig is None                    # 状态稳定后不再触发"



def test_dynamic_horizon_switch_controls_selector_override():
    """4b 开关关闭时传 None（固定视界），打开时传影子视界；默认关。"""
    def _spy(cfg):
        eng = DecisionEngine(cfg)
        seen = []
        orig = eng.selector.update

        def spy(*a, **k):
            seen.append(k.get("horizon_s"))
            return orig(*a, **k)

        eng.selector.update = spy
        eng.update(_obs(1), 0.05, executed_lane=0.0, corridor=_corridor())
        return seen[-1]

    assert _spy(CFG) is None
    on = replace(CFG, selection=replace(CFG.selection, dynamic_horizon=True))
    assert _spy(on) == pytest.approx(3.0)


def test_new_trace_columns_are_json_serializable():
    """第 1/2/4 期新增列全部可 JSON 序列化，且两个新开关默认关。"""
    import json
    eng = DecisionEngine(CFG)
    g, ms = _group(1, x=0.4, rel=2.5)
    for fid in (1, 2, 3):
        g = replace(g, first_seen_fid=1, last_seen_fid=fid)
        ms = tuple(replace(m, first_seen_fid=1, last_seen_fid=fid,
                           validity_until_fid=fid + 8) for m in ms)
        eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                   executed_lane=0.0, corridor=_corridor(),
                   ego_speed_mps=20.0)
    for v in (eng.object_table_trace(), eng.lane_map_trace(),
              eng.openness_trace(), eng.anchor_trace(),
              eng.horizon_trace(), eng.trigger_trace()):
        json.dumps(v, ensure_ascii=False)
    assert CFG.selection.dynamic_horizon is False
    assert CFG.selection.layer_dwell is False
    assert CFG.selection.event_replan is False


def test_event_replan_engine_small_step_on_target_jump():
    """引擎级：事件重算开时同层币目标从 0.13 跳到 0.33 道只走 0.1 道/拍。"""
    def run(event):
        cfg = replace(CFG, selection=replace(CFG.selection,
                                             event_replan=event))
        eng = DecisionEngine(cfg)
        g, ms = _group(1, x=0.4, rel=2.5)
        for fid in range(1, 6):
            x = 0.6 if fid >= 5 else 0.4
            g = replace(g, x_center=x, first_seen_fid=1, last_seen_fid=fid)
            ms = tuple(replace(m, x_lane=x - 0.15 * i,
                               first_seen_fid=1, last_seen_fid=fid,
                               validity_until_fid=fid + 8)
                       for i, m in enumerate(ms))
            eng.update(_obs(fid, groups=(g,), targets=ms), 0.05,
                       executed_lane=0.0, corridor=_corridor())
        return eng.selection_report.target_x_m / LW

    off = run(False)
    on = run(True)
    assert off == pytest.approx(0.33, abs=0.03)
    assert on == pytest.approx(0.23, abs=0.03)


# ---------- 6b 资格闭锁 / 6c 请求读数位 ----------

def _bonus(fid_seen, *, x=1.4, oid=900):
    return TrackedTarget(
        id=oid, kind="bonus", x_lane=x, x_sigma=0.1, cy=500, w=30, h=30,
        conf=0.99, rel_approach=1.5, first_seen_fid=1,
        last_seen_fid=fid_seen, validity_until_fid=99)


def test_eligibility_latch_holds_a_dropped_reward_for_selection_only():
    """闭锁只把"暂时没有读数"的奖励车留给选择层，不给场景/计划。"""
    cfg = replace(CFG, selection=replace(CFG.selection,
                                         eligibility_latch=True))
    eng = DecisionEngine(cfg)
    for fid in range(1, 5):                 # 过 min_obs_ticks=3
        eng.update(_obs(fid, targets=(_bonus(fid),)), 0.05,
                   executed_lane=0.0, corridor=_corridor())
    assert eng.selection_debug["src_elig"]["rw_sel"] == [[900, True, "pixel"]]
    # 丢检：横位没有读数（no_x），闭锁保留上一份
    eng.update(_obs(5, targets=(_bonus(5, x=None),)), 0.05,
               executed_lane=0.0, corridor=_corridor())
    elig = eng.selection_debug["src_elig"]
    assert [900, False, "held"] in elig["rw_sel"]
    assert elig["rw_rej"]["900"] == "no_x"
    # 保留源不进计划场景（场景只吃当拍源）
    assert all(o.id != 900 for o in eng.scene.objs)
    # 超过 0.3 s 释放
    for fid in range(6, 13):
        eng.update(_obs(fid, targets=(_bonus(5, x=None),)), 0.05,
                   executed_lane=0.0, corridor=_corridor())
    assert not [x for x in eng.selection_debug["src_elig"]["rw_sel"]
                if x[0] == 900]


def test_request_reports_whether_the_source_is_live():
    """请求带当拍读数位：未见本人的奖励车仍可发请求，但 live=False。"""
    eng = DecisionEngine(CFG)
    eng.update(_obs(4, targets=(_bonus(1),)), 0.05,
               executed_lane=0.0, corridor=_corridor())
    rep = eng.selection_report
    assert rep.lane_request == 1.0
    assert rep.req_live is False and rep.req_oid == 900
    eng.update(_obs(5, targets=(_bonus(5),)), 0.05,
               executed_lane=0.0, corridor=_corridor())
    assert eng.selection_report.req_live is True


def test_latch_and_request_hold_confirm_through_dropouts():
    """每 3 拍丢检的奖励车：闭锁保住证据、请求保持冻结，最终在可达拍起意。"""
    cfg = replace(CFG,
                  selection=replace(CFG.selection, eligibility_latch=True),
                  route=replace(CFG.route, request_hold=True))
    eng = DecisionEngine(cfg)
    started = False
    paused_seen = False
    for fid in range(1, 31):
        drop = fid % 3 == 0
        b = _bonus(fid - 1 if drop else fid, x=None if drop else 1.4)
        req_live = eng._route_req_live       # 本拍路线将消费的请求读数位
        eng.update(_obs(fid, targets=(b,)), 0.05,
                   executed_lane=0.0, corridor=_corridor())
        rt = eng.route_verdict
        # 请求延迟一拍消费：丢检拍本身不新增，下一拍看到 paused
        if rt.req_state == "paused":
            paused_seen = True
        if drop and rt.credit:
            assert [900, False, "held"] in \
                eng.selection_debug["src_elig"]["rw_sel"]
        if rt.verdict == "merge_start":
            assert rt.why == "opportunity"
            assert req_live is True         # 起意只认当拍有效来源
            started = True
            break
    assert started and paused_seen
