# -*- coding: utf-8 -*-
"""离线回放：用 trace 的场景快照重算轨迹计划，对照记录的计划杆值。

用途（2026-10-27）：验证「安全区间 + 擦边横向门 + 走廊硬软两层 + 计划门
即时让位」四件改动对满杆/候选翻转的影响。每拍用记录下来的 scene/plan_ego
重算 plan_motion——闭环保真（车模状态是实拍的），计划输入也保真；差异只
来自被改的决策规则。

用法：
    .venv/Scripts/python.exe tools/experiments/merge_contract/probe_interval_plan.py \
        --session 20261027_134222_1
"""
from __future__ import annotations

import argparse
import json
import math
import os

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.motion_plan import (
    EgoState, PlanGate, Scene, SceneObj, plan_motion)
from maaracing_master.plugins.speedrush.selection import RoadBand, RoadFrame

DEFAULT_DEBUG = os.path.join(
    os.environ.get("APPDATA", ""), "MaaRacingMaster", "logs")
LW = 3.38


def _road(d: dict) -> RoadFrame:
    r = d["road"]
    bands = tuple(
        RoadBand(float(z0), float(z1),
                 None if l is None else float(l),
                 None if rr is None else float(rr),
                 src=str(src), age_ms=None if age is None else float(age),
                 conf=float(conf))
        for z0, z1, l, rr, src, age, conf in r["bands"])
    return RoadFrame(bands,
                     None if r["w"] is None else float(r["w"]),
                     None if r["l"] is None else float(r["l"]),
                     None if r["r"] is None else float(r["r"]),
                     True)


def _scene(d: dict) -> Scene | None:
    s = d.get("scene")
    if not s:
        return None
    objs = tuple(SceneObj(
        str(o[0]), float(o[1]),
        None if o[2] is None else float(o[2]),
        float(o[3]), float(o[4]), float(o[5]), str(o[6]),
        None if o[7] is None else float(o[7]),
        bool(o[8]), bool(o[9]), bool(o[10]), bool(o[11]),
        None if o[12] is None else float(o[12]), bool(o[13]))
        for o in (s.get("objs") or ()))
    return Scene(int(s.get("schema", 3)), int(s["fid"]),
                 float(s["ego_m"]), _road(s), objs, float(s["lane_w"]),
                 float(s["frame_age_ms"]),
                 None if s.get("corridor_age_ms") is None
                 else float(s["corridor_age_ms"]),
                 bool(s.get("stale", False)))


def _coarse(d: dict) -> float | None:
    route = d.get("route") or {}
    if route.get("vd") == "needs_init":
        return None
    ref = route.get("ref")
    if ref is not None:
        return float(ref)
    c = d.get("coarse") or {}
    return None if c.get("lane") is None else float(c["lane"])


def _nearest_timed_car(d: dict) -> tuple[float, float] | None:
    """返回 (横向差, 到站时间)，取最近到站的定速车；无则 None。"""
    s = d.get("scene") or {}
    ego = float((d.get("plan_ego") or [0.0])[0]) * LW
    best = None
    for o in (s.get("objs") or ()):
        if o[0] != "car" or not o[8] or o[7] is None:
            continue
        c = (abs(float(o[1]) - ego), float(o[2]))
        if best is None or c[1] < best[1]:
            best = c
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--debug", default=DEFAULT_DEBUG)
    ap.add_argument("--stage", type=int, default=0)
    args = ap.parse_args()
    path = os.path.join(args.debug, args.session, "trace", "speedrush.jsonl")
    cfg = _read_decision(DECISION_FILE)

    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type"):
                continue
            if args.stage and d.get("stage_id") != args.stage:
                continue
            rows.append(d)
    if not rows:
        print("no ticks")
        return 1
    print("session %s ticks=%d stage=%s" % (
        args.session, len(rows), args.stage or "all"))

    gate = PlanGate(cfg.motion.switch_margin, cfg.motion.switch_dwell_s,
                    cfg.motion.score_epsilon,
                    cfg.motion.switch_geom_margin,
                    cfg.motion.switch_immediate_margin,
                    cfg.motion.switch_infeasible_grace_s)
    full_old = full_new = 0
    inf_old = inf_new = 0
    far_pass_old = far_pass_new = 0
    changed = 0
    tot_du = 0.0
    rev_old = rev_new = 0
    rev_both = 0
    n_both = 0
    hold_new = 0
    prev_old_u = prev_new_u = prev_old_b = prev_new_b = None
    samples = []
    for d in rows:
        scene = _scene(d)
        oldp = d.get("plan") or {}
        ego = d.get("plan_ego")
        if scene is None or ego is None:
            continue
        o = EgoState(float(ego[0]), float(ego[1]), float(ego[2]))
        p = plan_motion(scene, o, cfg.motion, cfg.selection, cfg.planner,
                        gate, intent_lane=float(d.get("x_target") or 0.0))
        uo = float(oldp.get("u") or 0.0)
        un = float(p.u_feedforward)
        full_old += abs(uo) >= 0.8
        full_new += abs(un) >= 0.8
        ro = oldp.get("reason") or ""
        inf_old += ro == "plan:infeasible_corridor"
        inf_new += p.reason == "plan:infeasible_corridor"
        near = _nearest_timed_car(d)
        if oldp.get("layer") == 2 and near is not None and near[0] > 1.25 * LW:
            far_pass_old += 1
        if p.layer == 2 and near is not None and near[0] > 1.25 * LW:
            far_pass_new += 1
        hold_new += p.reason == "plan:frame_shift"
        both_valid = bool(oldp.get("valid")) and p.valid
        if both_valid:
            if prev_old_b is not None and (uo > 0.05) != (prev_old_b > 0.05):
                rev_both += 1
            prev_old_b, prev_new_b = uo, un
            n_both += 1
        else:
            prev_old_b = prev_new_b = None
        if abs(uo - un) > 1e-6:
            changed += 1
            tot_du += abs(uo - un)
            samples.append((float(d.get("ts_wall_ns") or 0) / 1e9,
                            uo, un, ro, p.reason,
                            oldp.get("layer"), p.layer,
                            oldp.get("chosen"), p.chosen))
        if prev_old_u is not None and (uo > 0.05) != (prev_old_u > 0.05):
            rev_old += 1
        if prev_new_u is not None and (un > 0.05) != (prev_new_u > 0.05):
            rev_new += 1
        prev_old_u, prev_new_u = uo, un

    print("满杆拍 |u|>=0.8: 旧 %d -> 新 %d" % (full_old, full_new))
    print("plan:infeasible_corridor: 旧 %d -> 新 %d" % (inf_old, inf_new))
    print("2 道外擦边拍（layer2）: 旧 %d -> 新 %d" % (far_pass_old, far_pass_new))
    print("plan u 变更拍: %d  平均 |Δu|(变更拍): %.3f" % (
        changed, tot_du / max(changed, 1)))
    print("u 符号翻转（全序列）: 旧 %d -> 新 %d" % (rev_old, rev_new))
    print("u 符号翻转（双方都有效的相邻拍）: %d / %d 拍" % (rev_both, n_both))
    print("新计划因参考系分歧退回 PD: %d 拍" % hold_new)
    print("\n前 20 个变更样本（t=相对会话墙钟）：")
    for s in samples[:20]:
        print("  t=%s 旧u=%.2f 新u=%.2f 旧:%s L%s c%s 新:%s L%s c%s" % (
            ("%.2f" % (s[0] - rows[0].get("ts_wall_ns", 0) / 1e9)),
            s[1], s[2], s[3], s[5], s[7], s[4], s[6], s[8]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
