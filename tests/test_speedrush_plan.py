# -*- coding: utf-8 -*-
"""计划层锁（三类字典序 + 持续性反抖，设计稿 v2）：配置闸、字典序、
CHANGE 改判持续性门、bonus 接触兑现、V0 直行门不旁路。

兼容红线：plan_layer 闸关（部署默认）时 decision 旧路径逐位不变——
该性质由 test_speedrush_decision_traj.py 全套 + 本文件闸关断言共同锁。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)

DT = 0.05
_CFG_BASE = _read_decision(DECISION_FILE)
CFG_PLAN = replace(_CFG_BASE,
                   plan_layer=replace(_CFG_BASE.plan_layer, enabled=True),
                   mode=replace(_CFG_BASE.mode, trajectory_sampling=False),
                   grid_veto=replace(_CFG_BASE.grid_veto, enabled=False))


def _obs(*, fid=1, groups=(), targets=(), presence=None):
    if presence is None:
        presence = bool(groups or targets)
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(True, True, True, presence, False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups)


def _group(gid, fid, x, cy=500, rel=5.0, conf=0.85, n=3):
    ms = tuple(TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 40)
        for i in range(n))
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid,
                  validity_until_fid=fid + 40)
    return g, ms


def _bonus(bid, fid, x=1.0, cy=300, rel=10.0, *, age=10):
    """bonus 目标（TrackedTarget 鸭子 CarView）：age 拍前已见=过年龄门槛；
    cy=300/rel=10 → t_meet≈2.1s > 变道耗时+响应+余量≈0.9s（可行性门内）。"""
    return TrackedTarget(
        id=bid, kind="bonus", x_lane=x, x_sigma=0.1, cy=cy, w=40, h=40,
        conf=0.9, rel_approach=rel, first_seen_fid=fid - age,
        last_seen_fid=fid, validity_until_fid=fid + 40)


# ---------- 配置闸 ----------

def test_plan_layer_config_default_off_and_roundtrip(tmp_path):
    """老 json 缺段=全默认=关；部署文件当前=关（回放三检验前不许开）。"""
    d = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    assert _CFG_BASE.plan_layer.enabled is False          # 部署文件闸关
    d.pop("plan_layer", None)
    old = tmp_path / "decision_old.json"
    old.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    cfg = _read_decision(old)
    assert cfg.plan_layer.enabled is False
    assert cfg.plan_layer.switch_streak == 2


def test_plan_layer_streak_bounds():
    d = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    d.setdefault("plan_layer", {})["switch_streak"] = 0   # K<1 越界 fail-loud
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.json"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError):
            _read_decision(p)


# ---------- 字典序 ----------

def test_bonus_wins_over_coin():
    """bonus 与金币组同场：字典序最高类胜出，目标=bonus 本体。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,), targets=ms + (_bonus(555, 1),)),
                   DT, executed_lane=0.2)
    assert out.state is DecisionState.CHANGE
    assert e.target_kind == "bonus"
    assert out.target_id == 555


def test_coin_fallback_when_no_bonus_or_overtake():
    """无 bonus/街车候选：coin 作为兜底目标（第三类语义）。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,), targets=ms), DT, executed_lane=0.2)
    assert out.state is DecisionState.CHANGE
    assert e.target_kind == "coin"
    assert out.target_id == g.group_id


def test_bonus_goal_is_car_body():
    """bonus 需求=车体位（撞上去）：demand/x 目标朝 bonus 的 x_lane 推进，
    不做 d_hold 贴邻偏移。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,),
                        targets=ms + (_bonus(555, 1, x=1.0),)),
                   DT, executed_lane=0.2)
    assert out.x_target is not None
    assert out.x_target > 0.2             # 朝 1.0（车体位）方向推进


# ---------- 持续性门槛 ----------

def test_persistence_gate_delays_upgrade():
    """CHANGE 中 bonus 出现：第一次慢拍重评不换（streak=1 举证中），
    连续第二次才换——帧级闪现不撕计划（P3）。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,), targets=ms), DT, executed_lane=0.2)
    assert e.target_kind == "coin"
    # 慢拍重评一次（dt=0.6 > _REEVAL_S）：bonus 在场但仅 1 次 → 维持 coin
    out = e.update(_obs(fid=2, groups=(g,),
                        targets=ms + (_bonus(555, 2),)),
                   0.6, executed_lane=0.3)
    assert e.target_kind == "coin"
    assert out.target_id == g.group_id
    # 第二次慢拍重评：持续性达标 → 换计划
    out = e.update(_obs(fid=3, groups=(g,),
                        targets=ms + (_bonus(555, 3),)),
                   0.6, executed_lane=0.3)
    assert e.target_kind == "bonus"
    assert out.reason == "switch:score_win"


def test_bonus_flash_does_not_flip_plan():
    """bonus 单帧闪现后消失（未过年龄门槛/不再出现）：计划保持 coin 不撕。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    e.update(_obs(fid=1, groups=(g,), targets=ms), DT, executed_lane=0.2)
    # 闪现一帧（age=1 < min_obs_ticks=3：门口就挡）→ 慢拍重评无候选 → 维持
    e.update(_obs(fid=2, groups=(g,),
                  targets=ms + (_bonus(555, 2, age=1),)),
             0.6, executed_lane=0.3)
    out = e.update(_obs(fid=3, groups=(g,), targets=ms),
                   0.6, executed_lane=0.3)
    assert e.target_kind == "coin"


# ---------- 兑现 ----------

def test_bonus_contact_completes_on_arrival():
    """bonus 兑现=几何接触判定：目标 cy 渐进逼近到自车行 → bonus_contact 收尾
    回 CRUISE（出画不作成功证明，设计稿 §四.3）。步长 85px<cy_jump_max_px=90
    ——瞬移会被校验闸一票 ABORT（那是另一条正确语义，不在这里触发）。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    e.update(_obs(fid=1, groups=(g,),
                  targets=ms + (_bonus(555, 1),)), DT, executed_lane=0.2)
    assert e.target_kind == "bonus"
    v_ego = int(e.cal.v_ego)
    fid = 1
    for cy in range(300, v_ego + 10, 85):     # 逼近到跨过自车行
        fid += 1
        out = e.update(_obs(fid=fid, groups=(g,),
                            targets=ms + (_bonus(555, fid, cy=cy),)),
                       DT, executed_lane=0.9)
        if out.state is DecisionState.CRUISE:
            break
    assert out.state is DecisionState.CRUISE
    assert "bonus_contact" in out.reason


# ---------- 解约与重选（V0：目标消失→解约重选）----------

def test_target_lost_aborts_then_reselects():
    """bonus 计划中目标消失（宽限耗尽）：ABORT 有界回稳 → 出口重选在场候选
    （解约免费，P3 失效语义）。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    e.update(_obs(fid=1, groups=(g,),
                  targets=ms + (_bonus(555, 1),)), DT, executed_lane=0.2)
    assert e.target_kind == "bonus"
    # 目标消失（组仍在场）：lost → ABORT
    out = e.update(_obs(fid=2, groups=(g,), targets=ms), DT, executed_lane=0.5)
    assert out.state is DecisionState.ABORT_CHANGE
    assert "overtake_lost" in out.reason
    # 有界回稳落定 → CRUISE 重选在场金币组（解约免费：无冷却罚加）
    out = e.update(_obs(fid=3, groups=(g,), targets=ms), DT, executed_lane=0.5)
    assert out.state in (DecisionState.ABORT_CHANGE, DecisionState.CRUISE)
    for fid in range(4, 40):
        out = e.update(_obs(fid=fid, groups=(g,), targets=ms), DT,
                       executed_lane=0.5)
        if out.state is DecisionState.CHANGE:
            break
    assert out.state is DecisionState.CHANGE
    assert e.target_kind == "coin"


# ---------- 资格判例（V0：候选资格）----------

def test_bonus_not_approaching_rejected():
    """bonus 不接近（rel≤0）＝永远撞不上 → 不作候选；coin 兜底。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,),
                        targets=ms + (_bonus(555, 1, rel=0.0),)),
                   DT, executed_lane=0.2)
    assert e.target_kind == "coin"


def test_bonus_unreachable_rejected():
    """bonus 到站时间 < 变道耗时+响应+余量（贴脸快车）＝撞不上 → 不作候选。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,),
                        targets=ms + (_bonus(555, 1, cy=700, rel=60.0),)),
                   DT, executed_lane=0.2)
    assert e.target_kind == "coin"


def test_coin_conf_floor_still_enforced():
    """计划层不废质量门：组置信低于地板 → 不作候选（分数退出≠门槛全拆）。"""
    g, ms = _group(3, 1, x=0.5, conf=0.2)   # 低于 conf_floor=0.5
    e = DecisionEngine(CFG_PLAN)
    out = e.update(_obs(fid=1, groups=(g,), targets=ms), DT, executed_lane=0.2)
    assert out.state is DecisionState.CRUISE
    assert e.target_kind is None


# ---------- 帧抖动注入（V0：抖动不撕计划）----------

def test_bonus_flicker_across_slow_evals_never_switches():
    """慢拍级抖动：合格 bonus 在第 1 次重评在场、第 2 次消失——streak 归零，
    永远到不了 K=2，计划不撕（比单帧 age 门槛更强的抖动注入）。"""
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(CFG_PLAN)
    e.update(_obs(fid=1, groups=(g,), targets=ms), DT, executed_lane=0.2)
    assert e.target_kind == "coin"
    fid = 1
    for i in range(6):                        # 在场/消失交替 × 3 轮慢拍
        fid += 1
        tgt = ms + (_bonus(555, fid),) if i % 2 == 0 else ms
        out = e.update(_obs(fid=fid, groups=(g,), targets=tgt),
                       0.6, executed_lane=0.3)
        assert e.target_kind == "coin"
    assert out.state is DecisionState.CHANGE


# ---------- V0 红线 ----------

def test_allow_all_moves_gate_not_bypassed():
    """V0 直行门（allow_all_moves=false）在计划层闸开时依旧一票拦——
    红线不因分流旁路。"""
    cfg = replace(CFG_PLAN, mode=replace(CFG_PLAN.mode, allow_all_moves=False))
    g, ms = _group(3, 1, x=0.5)
    e = DecisionEngine(cfg)
    out = e.update(_obs(fid=1, groups=(g,),
                        targets=ms + (_bonus(555, 1),)), DT, executed_lane=0.2)
    assert out.state is DecisionState.CRUISE
    assert out.move_allowed is False
