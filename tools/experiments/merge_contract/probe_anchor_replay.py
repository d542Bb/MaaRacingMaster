# -*- coding: utf-8 -*-
"""车模/⑥b A/B 离线回放（工作台，不入库）。

同一份 trace 喂两套参数，逐拍重放 LateralPlanner，回答：

  1. 位置：新鲜观测残差 |obs−exec|；单拍大跳（>0.6 道）拍数；
  2. 速度：模型横速 vs 观测反推横速（相邻新鲜观测差分）的偏差——
     分全部拍 / 反打拍（|u|>0.6 且 u·v_obs<0）两组；
  3. 刹停预算：条件成立（预测停止点越过目标带远侧缘）的拍数与
     --window 内的首个成立时刻；贴边僵直（|target−exec|>1 且 |u|<0.2）拍数。

A 臂=counter_gain_lane_s2=0（旧对称车模），B 臂=--counter（默认 1.3）。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_anchor_replay.py \
        --session 20261027_110753_1 --counter 1.3 --window 25.0:26.8 --dump 25.2:26.8

输入重建与 module.py 同口径：
  road_offset_hold = ro_source ∈ {pair_slot, wall_occl, memory}
  road_offset_new  = dgeo_new and not hold
  lane_ok = lane_obs.state=="TRACK" and n_locked≥1 and x 非空
  lane_relock = lane_obs.state=="RELOCK"
plan 预览位按 x_preview_lane = x_preview_m / lane_w 近似（trace 只存米制
场景帧；两臂同一近似，不影响相对比较）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import types

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
import maaracing_master.plugins.speedrush.planner as _planner_mod  # noqa: E402
from maaracing_master.plugins.speedrush.planner import (  # noqa: E402
    LateralPlanner, step as _step_sym)
from maaracing_master.plugins.speedrush.tracking import (  # noqa: E402
    DecisionOutput, DecisionState)

import math  # noqa: E402


def make_step(counter: float):
    """返回横向积分函数：counter=0 用产线对称式；>0 反打时用该增益。"""
    if counter <= 0.0:
        return _step_sym

    def _step(x, v, steer, u, dt, p):
        steer += (1.0 - math.exp(-dt / p.tau_steer_s)) * (u - steer)
        gain = p.a_lat_gain
        if steer * v < 0.0 and abs(v) > 0.05 and abs(steer) > 1e-6:
            gain = counter
        v += (gain * steer - v / p.tau_align_s) * dt
        v = max(-p.v_lat_max, min(p.v_lat_max, v))
        x += v * dt
        return x, v, steer

    return _step

DEFAULT_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.environ.get(
        "SPEEDRUSH_DEBUG",
        r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/speedrush"))),
    "logs")
HOLD_SRC = {"pair_slot", "wall_occl", "memory"}
TICK_HZ = 18.0


def load_games(logs_dir: str, token: str):
    path = os.path.join(logs_dir, token, "trace", "speedrush.jsonl")
    games, cur = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            kind = d.get("type")
            if kind == "stage_begin":
                if cur:
                    games.append(cur)
                cur = []
                continue
            if kind in ("stage_end", "session_begin"):
                continue
            cur.append(d)
    if cur:
        games.append(cur)
    return games


def _decision(row, fid: int) -> DecisionOutput:
    st = row.get("state") or "CRUISE"
    try:
        state = DecisionState(st)
    except ValueError:
        state = DecisionState.CRUISE
    return DecisionOutput(
        schema_version=5, state=state,
        x_target=row.get("x_target"), reason=row.get("reason") or "",
        emitted_fid=fid, valid_until_fid=fid + 2,
        maneuver=bool(row.get("maneuver")))


def _plan(row, lane_w: float):
    p = row.get("plan")
    if not isinstance(p, dict) or not p.get("valid") or p.get("u") is None:
        return None
    xm = p.get("x")
    return types.SimpleNamespace(
        valid=True, reason=p.get("reason") or "",
        u_feedforward=float(p["u"]),
        x_preview_lane=(float(xm) / lane_w if isinstance(xm, (int, float))
                        and lane_w else 0.0),
        v_preview_lane_s=float(p.get("v") or 0.0))


def replay(g: list, cfg, window=None) -> dict:
    """逐拍重放，返回指标与逐拍轨迹。积分函数取模块级 step（可用
    ``_planner_mod.step`` 临时替换做车模 A/B）。"""
    pl = LateralPlanner(cfg)
    lane_w = 3.38
    out = dict(ticks=0, fresh=0, residual=[], teleports=0, blind=0,
               fidelity=[], trace=[], v_err=[], v_err_brake=[],
               budget_on=0, budget_first=None)
    prev_exec = None
    prev_fresh = None
    for i, row in enumerate(g):
        t = i / TICK_HZ
        sc = row.get("scene") or {}
        lane_w = float(sc.get("lane_w") or lane_w)
        dt = float(row.get("dt") or (1.0 / TICK_HZ))
        fid = int(row.get("fid") or (i + 1))
        ro = row.get("road_offset")
        src = row.get("ro_source") or "none"
        hold = src in HOLD_SRC
        new = bool(row.get("dgeo_new")) and not hold and ro is not None
        ln = row.get("lane_obs") or {}
        lane_x = ln.get("x") if isinstance(ln, dict) else None
        lane_ok = (isinstance(ln, dict) and ln.get("state") == "TRACK"
                   and int(ln.get("n_locked") or 0) >= 1
                   and lane_x is not None)
        pl.update(_decision(row, fid), dt, fid, road_offset=ro,
                  road_offset_new=new, road_offset_hold=hold,
                  lane_x=lane_x, lane_ok=lane_ok,
                  lane_relock=(ln.get("state") == "RELOCK"
                               if isinstance(ln, dict) else False),
                  lane_theta=ln.get("theta") if isinstance(ln, dict) else None,
                  lane_theta_ok=lane_ok,
                  lane_fid=ln.get("det_fid") if isinstance(ln, dict) else None,
                  lane_age_ms=(ln.get("det_age_ms")
                               if isinstance(ln, dict) else None),
                  plan=_plan(row, lane_w))
        ex = pl.state.executed_lane
        v = pl.state.v_lat_est
        u = pl.last_steer_raw
        out["ticks"] += 1
        if prev_exec is not None and abs(ex - prev_exec) > 0.6:
            out["teleports"] += 1
        prev_exec = ex
        rec = row.get("executed_lane")
        if isinstance(rec, (int, float)):
            out["fidelity"].append(abs(ex - rec))
        obs = None if ro is None else ro - (pl._road_anchor or 0.0)
        v_obs = None
        if ro is not None and not hold:
            out["fresh"] += 1
            out["residual"].append(abs(obs - ex))
            if prev_fresh is not None:
                t0, o0 = prev_fresh
                gap = t - t0
                if 0.1 <= gap <= 0.6:
                    v_obs = (obs - o0) / gap
                    out["v_err"].append(abs(v - v_obs))
                    if abs(u) > 0.6 and u * v_obs < 0:
                        out["v_err_brake"].append(abs(v - v_obs))
            prev_fresh = (t, obs)
        elif prev_fresh is not None and t - prev_fresh[0] > 0.6:
            prev_fresh = None
        xt = row.get("x_target")
        if isinstance(xt, (int, float)) and abs(xt - ex) > 1.0 and abs(u) < 0.2:
            out["blind"] += 1
        budget = False
        if isinstance(xt, (int, float)) and abs(v) > 1e-3 \
                and cfg.brake_decel_lane_s2 > 0.0:
            sgn = 1.0 if v > 0 else -1.0
            stop = ex + sgn * v * v / (2.0 * cfg.brake_decel_lane_s2)
            edge = xt + sgn * cfg.hold_deadband_lane
            budget = (stop - edge) * sgn > 0.0
        if budget:
            out["budget_on"] += 1
            if window and out["budget_first"] is None \
                    and window[0] <= t <= window[1]:
                out["budget_first"] = (t, ex, v)
        out["trace"].append(dict(t=t, exec=ex, v=v, u=u, obs=obs,
                                 v_obs=v_obs, fresh=bool(ro is not None
                                                         and not hold),
                                 budget=budget, xt=xt))
    return out


def _stats(xs):
    if not xs:
        return (0.0, 0.0, 0.0)
    xs = sorted(xs)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    return (q(0.5), q(0.9), xs[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--counter", type=float, default=1.3,
                    help="B 臂反打增益（0=旧对称车模）")
    ap.add_argument("--window", default=None, help="对照窗 t0:t1（秒）")
    ap.add_argument("--dump", default=None, help="逐拍对照窗 t0:t1（秒）")
    args = ap.parse_args()

    def span(s):
        a, b = s.split(":")
        return (float(a), float(b))

    win = span(args.window) if args.window else None
    dump = span(args.dump) if args.dump else None

    base = load_decision()
    old_cfg, new_cfg = base.planner, base.planner
    old_step, new_step = make_step(0.0), make_step(args.counter)

    games = load_games(args.logs, args.session)
    print("session %s games=%d  B.counter=%.2f" % (
        args.session, len(games), args.counter))
    for gi, g in enumerate(games):
        _planner_mod.step = old_step
        try:
            ra = replay(g, old_cfg, win)
        finally:
            _planner_mod.step = _step_sym
        _planner_mod.step = new_step
        try:
            rb = replay(g, new_cfg, win)
        finally:
            _planner_mod.step = _step_sym
        print("\n-- game%d (%d ticks) --" % (gi + 1, len(g)))
        fa, fb = _stats(ra["fidelity"]), _stats(rb["fidelity"])
        print("  贴合实录 exec p50/p90:      A=%.2f/%.2f  B=%.2f/%.2f" % (
            fa[0], fa[1], fb[0], fb[1]))
        ia, ib = _stats(ra["residual"]), _stats(rb["residual"])
        print("  新鲜观测残差 p50/p90/max:   A=%.2f/%.2f/%.2f  B=%.2f/%.2f/%.2f"
              % (ia + ib))
        va, vb = _stats(ra["v_err"]), _stats(rb["v_err"])
        vba, vbb = _stats(ra["v_err_brake"]), _stats(rb["v_err_brake"])
        print("  横速误差 全部 p50/p90:      A=%.2f/%.2f  B=%.2f/%.2f" % (
            va[0], va[1], vb[0], vb[1]))
        print("  横速误差 反打拍 p50/p90:    A=%.2f/%.2f(n=%d)  B=%.2f/%.2f(n=%d)"
              % (vba[0], vba[1], len(ra["v_err_brake"]),
                 vbb[0], vbb[1], len(rb["v_err_brake"])))
        print("  单拍大跳>0.6道:             A=%d  B=%d" % (
            ra["teleports"], rb["teleports"]))
        print("  贴边僵直(>1道且|u|<0.2):    A=%d  B=%d" % (
            ra["blind"], rb["blind"]))
        print("  刹停预算成立拍:             A=%d  B=%d" % (
            ra["budget_on"], rb["budget_on"]))
        if win:
            def fmt(r):
                if r["budget_first"] is None:
                    return "未触发"
                t, x, v = r["budget_first"]
                return "t=%.2fs exec=%+.2f v=%+.2f" % (t, x, v)
            print("  窗 %s 首个预算成立:  A=%s  B=%s" % (
                args.window, fmt(ra), fmt(rb)))
        if dump:
            print("  逐拍 (t | obs | 录exec | A exec/v | B exec/v | u | 预算)：")
            for i in range(len(g)):
                t = i / TICK_HZ
                if not (dump[0] <= t <= dump[1]):
                    continue
                ta, tb = ra["trace"][i], rb["trace"][i]
                ro = g[i].get("road_offset")
                rec = g[i].get("executed_lane")
                print("  %5.2f obs=%6s rec=%6s | %+5.2f v%+5.2f | %+5.2f v%+5.2f | u%+5.2f %s" % (
                    t,
                    ("%+.2f" % ro) if isinstance(ro, (int, float)) else "NA",
                    ("%+.2f" % rec) if isinstance(rec, (int, float)) else "NA",
                    ta["exec"], ta["v"], tb["exec"], tb["v"], ta["u"],
                    "B" if ta["budget"] or tb["budget"] else " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
