# -*- coding: utf-8 -*-
"""step 4 决策层的回归锁：配置 fail-loud、校验合取、评分偏序、FSM 全转移矩阵。

矩阵按设计稿 v2 §二逐格上锁（含取消/超时/改判/恢复/升级五类收尾路径）；
评分只锁**偏序**不锁绝对值（验收 3）；数值场景以 decision.json 起值反推
（helper 里注释了各场景过/不过哪道门的算式），改参数会红——这是刻意的：
参数与测试场景是一对契约面。
"""

from __future__ import annotations

import json
import math

import pytest

from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import (
    DecisionEngine, Scorer, ValidationWatch)
from maaracing_master.plugins.speedrush.traffic import CarView
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    _read_gate0, load_calib, t_meet_car_s)
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH, GATE0_FILE as GATE0_PATH

DT = 0.05          # 20Hz 名义 tick
CFG = _read_decision(
    __import__("maaracing_master.plugins.speedrush", fromlist=["DECISION_FILE"])
    .DECISION_FILE)


# ---------- 观测构造 helper（场景算式见各测试注释） ----------

def _obs(*, fid=1, groups=(), targets=(), presence=None, alive=True, fresh=True,
         geometry=True, transition=False, boundary=None, fid_hz=None, raw=None):
    if presence is None:
        presence = bool(groups or targets)
    if raw is None:
        raw = presence
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(alive, fresh, geometry, presence, transition,
                                raw_target_presence=raw),
        boundary=boundary, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=fid_hz)


def _members(fid, gid, n, x, cy, rel, conf=0.85):
    return [TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n)]


def _group(gid, fid, n, x, cy, rel, conf=0.85):
    ms = _members(fid, gid, n, x, cy, rel, conf)
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, tuple(ms)


# 场景 A（默认"良候选"）：n=3 conf=0.85 x=0.5 cy=500 rel=5
#   流口径（全场 k 中位 = rel·hz/(cy−y_h)²）：k=100/178²≈3.16e-3；
#   t_miss=∫=(1/k)·(1/181.8−1/391.8)≈0.93s；life=e^−0.93/5≈0.83
#   value=30·3·0.6=54 → 44.8 −shift 5 ≈ 39.8 ≥ min 20 ✓；
#   need=0.3+0.4·0.5+0.13+0.15=0.78 < 0.93 ✓可行
GOOD = dict(gid=1, n=3, x=0.5, cy=500, rel=5)


def _good_obs(fid=1, **kw):
    p = {**GOOD, **kw}
    gid = p.pop("gid")
    g, ms = _group(gid, fid, **p)
    return _obs(fid=fid, groups=(g,), targets=ms)


# ---------- 配置 fail-loud ----------

def _decision_dict():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("schema_version"), "schema_version"),
    (lambda d: d["validate"].update(t_recover_s=99.0), "t_conserve_max"),
    (lambda d: d["mode"].update(allow_car_graze=True), "allow_car_graze"),
    (lambda d: d["scoring"].update(conf_floor=1.5), "越界"),
    (lambda d: d["scoring"].update(conf_floor=0.9), "折扣区倒置"),
    (lambda d: d["control"].update(frame_rate_hz=0), "越界"),
    (lambda d: d.pop("planner"), "planner"),
    (lambda d: d["planner"].update(lookahead_tau_s=0.05), "lookahead"),
    (lambda d: d["planner"].update(rate_limit_raw=99999.0), "限幅"),
    (lambda d: d["control"].pop("abort_settle_v_lane_s"), "abort_settle"),
    (lambda d: d["planner"].update(maneuver_stick_max=0.0), "maneuver_stick_max"),
    (lambda d: d["planner"].update(obs_beta_dv_rate_lane_s2=99.0),
     "obs_beta_dv_rate"),
    (lambda d: d["traffic"].pop("v_lat_ema_alpha"), "v_lat_ema"),
    (lambda d: d["motion"].update(preview_s=3.0), "preview_s"),
    (lambda d: d["motion"].update(mode="live", calibrated=False),
     "calibrated"),
])
def test_decision_fail_loud(tmp_path, mutate, frag):
    d = _decision_dict()
    mutate(d)
    p = tmp_path / "decision.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        _read_decision(p)
    assert frag in str(e.value)


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("a_x"), "a_x"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.update(v_ego=300.0), "分母退化"),        # v_ego < y_h
    (lambda d: d.update(y_h="地平线"), "非数值"),
])
def test_gate0_fail_loud(tmp_path, mutate, frag):
    d = json.loads(GATE0_PATH.read_text(encoding="utf-8"))
    mutate(d)
    p = tmp_path / "gate0.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        _read_gate0(p)
    assert frag in str(e.value)


def test_real_sources_load():
    assert CFG.validate.t_recover_s < CFG.validate.t_conserve_max_s
    cal = load_calib()
    assert cal.v_ego - cal.y_h > 0 and cal.min_denom > 0


# ---------- 校验合取（§四：五信号合取，任何单项不作数） ----------

def test_watch_frame_stale_fatal():
    w = ValidationWatch(CFG)
    r = w.check(_obs(fresh=False), DT)
    assert r.fatal == "frame_stale"


def test_watch_stale_during_transition_not_fatal():
    w = ValidationWatch(CFG)
    assert w.check(_obs(fresh=False, transition=True), DT).fatal is None


def test_watch_empty_needs_conjunction():
    """枯竭计时要求 alive∧fresh∧geometry∧¬transition 全立——空旷/过渡/半死不计时。"""
    w = ValidationWatch(CFG)
    for _ in range(int(CFG.validate.t_empty_s / DT)):
        assert w.check(_obs(presence=False, geometry=False), DT).fatal is None
    # 合取恢复后从头计时（旧账不追溯）
    for i in range(int(CFG.validate.t_empty_s / DT) - 1):
        assert w.check(_obs(presence=False, raw=True), DT).fatal is None
    assert w.check(_obs(presence=False, raw=True), DT).fatal == "target_empty"


def test_watch_target_presence_resets_timer():
    w = ValidationWatch(CFG)
    for _ in range(10):
        w.check(_obs(presence=False, raw=True), DT)
    assert w.check(_good_obs(), DT).fatal is None      # 有目标：清零
    for _ in range(int(CFG.validate.t_empty_s / DT) - 1):
        assert w.check(_obs(presence=False, raw=True), DT).fatal is None


def test_watch_cy_jump_reversal():
    w = ValidationWatch(CFG)
    g, ms = _group(1, 1, 3, 0.5, 500, 10)
    w.check(_obs(fid=1, groups=(g,), targets=ms), DT)
    g2, ms2 = _group(1, 2, 3, 0.5, 700, 10)            # 同 id 全组 +200px ≫ 90
    assert w.check(_obs(fid=2, groups=(g2,), targets=ms2), DT).fatal == "cy_jump"


# ---------- 评分偏序（验收 3：锁排序语义不锁绝对值） ----------

def _score(**kw):
    p = {**GOOD, **kw}
    gid = p.pop("gid")
    g, ms = _group(gid, 1, **p)
    s = Scorer(CFG, load_calib())
    return s.score(g, _obs(groups=(g,), targets=ms))


def test_score_monotone_in_count():
    assert _score(n=4).score > _score(n=3).score


def test_score_group_floor_partial_view():
    """组下限（RULES 2026-10-14 定案）：观测 1~2 枚是残缺视图（距离/遮挡/置信
    门削剩的头部），价值按整组 ≥3 枚计，不按观测枚数折算——n=1 组按 30 分
    基值再被车道惩罚一压连 min_score 都过不了（20261014 实机 score 拒
    106~119 次/局、金币候选 ok 每局 1 拍）。锁：同几何下 n=1 与 n=3 同值
    （残缺视图按整组计）且过 min_score；n=4 仍严格更高（test_monotone 已锁）。"""
    def _g_with(n):
        ms = _members(1, 1, n, 0.5, 500, 5)
        g = CoinGroup(group_id=1, member_ids=tuple(m.id for m in ms),
                      observed_count=n, estimated_count=n, x_center=0.5,
                      x_span=0.0, cy_min=500, cy_max=500, conf_min=0.85,
                      partial_observation=False,
                      first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
        return g, ms

    s = Scorer(CFG, load_calib())
    g1, ms1 = _g_with(1)
    g3, ms3 = _g_with(3)
    s1 = s.score(g1, _obs(groups=(g1,), targets=ms1))
    s3 = s.score(g3, _obs(groups=(g3,), targets=ms3))
    assert s1 is not None and s3 is not None
    # 组值项逐值同（max(1,3)=3）；t_miss 的全场 k 随组员集中位略差，放宽到 2%
    assert s1.score == pytest.approx(s3.score, rel=0.02)
    assert s1.score > 0                          # 完整整组值，未被折扣归零


def test_score_monotone_in_distance():
    # 流口径下速率随行号二次增长，远行目标必须配按场一致的 rel（同一 k）——
    # rel(cy) ∝ (cy−y_h)²，锚定 GOOD(cy=500, rel=5)；锁「越远时间越多」的序
    def _rel(cy):
        return 5.0 * (cy - load_calib().y_h) ** 2 / (500 - load_calib().y_h) ** 2
    assert _score(cy=620, rel=_rel(620)).score \
        > _score(cy=500).score > _score(cy=420, rel=_rel(420)).score


def test_score_penalizes_lateral_shift():
    assert _score(x=0.3).score > _score(x=1.5).score


def test_score_conf_ramp_and_floor():
    assert _score(conf=0.99).score > _score(conf=0.7).score
    # 20261017 教训：地板降到检测门槛 0.35（日落逆光场景 conf 帧间 0.42~0.83
    # 剧烈跳，0.5 切在区间正中把肉眼可见的币整组拒掉）——0.4 进低权坡度不拒
    assert _score(conf=0.4) is not None      # 低权计入（折扣代替拒收）
    assert _score(conf=0.4).score < _score(conf=0.7).score
    assert _score(conf=0.34) is None         # 地板（=检测门槛）下不低信决策
    held = CoinGroup(group_id=9, member_ids=(1,), observed_count=1, estimated_count=1,
                     x_center=0.5, x_span=0, cy_min=500, cy_max=500, conf_min=0.0,
                     partial_observation=True, first_seen_fid=0, last_seen_fid=0,
                     validity_until_fid=9)
    assert Scorer(CFG, load_calib()).score(held, _obs(groups=(held,))) is None


def test_score_rejects_receding_target():
    """rel≤0（远离）：机会视为无限远 → 分数被折扣压到不可选（−cost）。"""
    s = _score(rel=-5)
    assert s is None or s.score < 1.0            # 折扣归零（−位移代价）


def test_score_survives_member_id_churn_via_field_flow():
    """换基主锁（2026-10-02）：组员 id 全断（不在 targets 里，逐轨账本旧法
    必然饿死成 life=0）但场内另有健康 track——地面流是全场属性，k 从横截面
    拟合出来，金币照常可选。旧逐轨实现下此锁必红。"""
    g, _ = _group(7, 1, 3, x=0.5, cy=500, rel=0.0)     # 组员 rel=0 且不在 targets
    # 场内两个健康 track（行号不同、速率按同一 k=100/178² 流场自洽）
    flow = [TrackedTarget(
        id=90 + i, kind="coin", x_lane=-0.8 + 1.6 * i, x_sigma=0.1,
        cy=cy, w=20, h=20, conf=0.9,
        rel_approach=100.0 * (cy - load_calib().y_h) ** 2 / (500 - load_calib().y_h) ** 2
        / CFG.control.frame_rate_hz,
        first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
        for i, cy in enumerate((600, 450))]
    s = Scorer(CFG, load_calib()).score(g, _obs(targets=flow))
    assert s is not None and s.score > 0


def test_score_blackout_keeps_failsafe_discount():
    """全场无任何速率测量（断供黑视）：无限远折扣 → 不可选——安全默认与
    逐轨断供时代同形，不许在无证据时臆造可达性。"""
    g, _ = _group(7, 1, 3, x=0.5, cy=500, rel=0.0)
    s = Scorer(CFG, load_calib()).score(g, _obs(targets=()))
    assert s is not None and s.score < 1.0


# ---------- FSM 全转移矩阵（§二优先级逐格） ----------

def _eng(**over):
    return DecisionEngine(CFG)


def test_conserve_output_holds_and_forbids_moves():
    e = _eng()
    e._enter_conserve()                            # 直接进态（矩阵其余路径另有锁）
    out = e.update(_obs(fid=5, presence=True), DT)
    assert out.state is DecisionState.CONSERVE
    assert out.x_target is None                    # 保持≠回中（§二）


def test_conserve_recovers_with_full_reset():
    e = _eng()
    e._enter_conserve()                            # fatal 触发保守态（直接进入）
    e.update(_obs(fid=2, fresh=False), DT)         # 过旧拍：保守态续留
    e.update(_obs(fid=3, presence=True), DT)
    f = 4
    out = e.update(_obs(fid=f, presence=True, geometry=True), DT)
    while out.reason != "conserve_recovered" and f < 200:
        f += 1
        out = e.update(_obs(fid=f, presence=True, geometry=True), DT)
    assert out.state is DecisionState.CRUISE
    # 首个非致命拍=fid 3：恢复计数从这拍起，不许秒回
    assert f - 2 >= int(CFG.validate.t_recover_s / DT)
    e.reset()
    assert e._target_prev is None                       # 重置面


def test_conserve_escalates_to_fault_and_back():
    """升级要"降级但不致命"的观测（过旧+过渡=非致命但 degraded），否则满 T_recover
    就恢复、到不了 T_conserve_max 的 FAULT 升级；恢复再切回健康帧走双 T_recover。"""
    e = _eng()
    e._enter_conserve()
    f = 10
    out = e.update(_obs(fid=f, presence=True, fresh=False, transition=True), DT)
    while out.reason != "fault:conserve_max" and f < 400:
        f += 1
        out = e.update(_obs(fid=f, presence=True, fresh=False, transition=True), DT)
    assert out.state is DecisionState.FAULT
    while out.reason != "fault_recovered" and f < 600:
        f += 1
        out = e.update(_obs(fid=f, presence=True), DT)   # 健康帧：双 T_recover 降回
    assert out.state is DecisionState.CRUISE
    e.reset()
    assert e._target_prev is None                       # 重置面（恢复后冷启动）


def test_output_schema_and_validity_window():
    e = _eng()
    out = e.update(_good_obs(), DT)
    assert out.schema_version == 5                 # v5：删除 move_allowed
    assert out.valid_until_fid > out.emitted_fid
    assert isinstance(out.reason, str) and out.reason


def test_engine_reset_clears_everything():
    e = _eng()
    e.update(_good_obs(), DT)
    e.reset()
    assert e._state is DecisionState.CRUISE and e._target_prev is None

def _cv(tid=5, x=1.1, cy=500, rel=10.0):
    return CarView(id=tid, x_lane=x, cy=cy, rel_approach=rel,
                   d_min=abs(x), age_ticks=10)


def _traffic(views=(), events=()):
    return (tuple(views), tuple(events))


# ---------- 对称性原则（维护者裁定 2026-09-22：决策默认左右对称，禁用方向需显式证据） ----------

def _corridor(left_m=None, right_m=None):
    """单带走廊（_side_has_space 消费面）：米制入参，obs.boundary 通道
    v4 恒 None 已退役（第 6 步侧别检查改吃走廊带）。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand, UNKNOWN)
    band = CorridorBand(
        z0=3.0, z1=6.0,
        left=BandBoundary(left_m, "measured") if left_m is not None
        else BandBoundary(None, UNKNOWN),
        right=BandBoundary(right_m, "measured") if right_m is not None
        else BandBoundary(None, UNKNOWN))
    return Corridor((band,), None, None, "pair", "ok")




def test_abort_settles_on_velocity_zero():
    """有反馈 ABORT：executed 停住（速率≤界）即落稳，不再一律冻满 2s。"""
    e2 = _eng()
    e2._enter_abort("test")                                # fatal 路径外直接进入
    out = e2.update(_obs(fid=10, presence=True), DT, executed_lane=0.2)  # 首拍立参考
    assert out.state is DecisionState.ABORT
    out = e2.update(_obs(fid=11, presence=True), DT, executed_lane=0.2)  # v=0 → 落稳
    assert out.state is DecisionState.CRUISE and "abort:settled" in out.reason
    # 仍在横移：不落稳
    e3 = _eng()
    e3._enter_abort("test")
    out = e3.update(_obs(fid=10, presence=True), DT, executed_lane=0.2)  # 立参考
    assert out.state is DecisionState.ABORT
    out = e3.update(_obs(fid=11, presence=True), DT, executed_lane=0.35)   # v=3>0.3
    assert out.state is DecisionState.ABORT





# ---------- 前置三件套①：车流 t_meet 地面流化（2026-10-11 V0 归因） ----------

def test_car_t_meet_ground_flow_and_fallback():
    """fid 率有供=地面流积分式（对 1/r 沿行积分的闭式对账）；缺供=退回旧
    行率式（已知 TTC 高估方向的旧消费口径，行为不静默换式）。"""
    cal = load_calib()
    d, D, rel, f = 200.0, cal.v_ego - cal.y_h, 4.0, 30.0
    assert t_meet_car_s(rel, cal.y_h + d, cal, f) == \
        pytest.approx(d * (D - d) / (D * rel) / f)
    assert math.isinf(t_meet_car_s(-1.0, 500.0, cal, f))   # 不接近→永不到站
    assert t_meet_car_s(4.0, cal.v_ego + 5, cal, f) == 0.0  # 已在行上
    with pytest.raises(ValueError):
        t_meet_car_s(4.0, 500.0, cal, 0.0)
    e = _eng()
    v = _cv(tid=1, cy=cal.y_h + d, rel=rel)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([v]))
    # 缺供退路=旧线性式（rel 按 px/tick 消费），与三件套①前的行为逐位同
    assert e.scorer.car_t_meet_s(rel, cal.y_h + d) == \
        pytest.approx((cal.v_ego - cal.y_h - d) / (rel * 20.0))
    # 引擎逐拍喂 fid 率后换积分式
    e.update(_obs(fid=2, presence=True, fid_hz=f), DT, traffic=_traffic([v]))
    assert e.scorer.fid_hz == f
    assert e.scorer.car_t_meet_s(rel, cal.y_h + d) == \
        pytest.approx(d * (D - d) / (D * rel) / f)




# ---------- 目标流枯竭：空街不是故障 ----------

def test_empty_road_does_not_trip_target_empty():
    w = ValidationWatch(CFG)
    for fid in range(1, 12):
        rep = w.check(_obs(fid=fid, presence=False, raw=False), 0.5)
        assert rep.fatal is None


def test_raw_detections_without_tracks_trip_target_empty():
    w = ValidationWatch(CFG)
    fatal = None
    for fid in range(1, 12):
        fatal = w.check(_obs(fid=fid, presence=False, raw=True), 0.5).fatal
    assert fatal == "target_empty"


def _car_t(tid, fid, cy, box):
    return TrackedTarget(
        id=tid, kind="car", x_lane=0.0, x_sigma=0.1, cy=cy, w=100, h=100,
        conf=0.9, rel_approach=0.0, first_seen_fid=1, last_seen_fid=fid,
        validity_until_fid=fid + 8, box=box)


def test_fast_approach_cy_jump_with_continuous_box_is_not_fatal():
    """大车逼近 1 拍跨 150px 是物理的；2D 框仍重叠就不是身份互换。"""
    w = ValidationWatch(CFG)
    a = _car_t(1, 1, 500, (100, 400, 220, 560))
    b = _car_t(1, 2, 650, (100, 460, 220, 700))
    assert w.check(_obs(fid=1, targets=(a,)), 0.05).fatal is None
    assert w.check(_obs(fid=2, targets=(b,)), 0.05).fatal is None


def test_cy_jump_without_box_overlap_is_fatal():
    w = ValidationWatch(CFG)
    a = _car_t(1, 1, 500, (100, 400, 200, 500))
    b = _car_t(1, 2, 650, (700, 600, 800, 700))
    assert w.check(_obs(fid=1, targets=(a,)), 0.05).fatal is None
    assert w.check(_obs(fid=2, targets=(b,)), 0.05).fatal == "cy_jump"
