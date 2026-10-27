# -*- coding: utf-8 -*-
"""⑥b 新息门 A/B 离线回放（工作台，不入库）。

同一份 trace 喂两套参数（A=旧固定门 0.8 道；B=时基物理包络），逐拍重放
LateralPlanner，回答三件事：

  1. 位置滞后：新鲜观测拍的新息 |obs−exec|（控制器实际吃到的偏差）；
  2. 单拍瞬移：|Δexec| > 0.6 道的拍数（跳变确认后的 exec:=obs）；
  3. 刹停时刻：--window 内首次满足刹停预算条件的时间（对照墙击时刻）。

另报"贴边僵直"判据：|target−exec| > 1 道且 |u| < 0.2 的拍数。回放首段
先对实录 executed_lane 做贴合度检查（p90 偏差），偏差大时数字只看相对。

输入重建与 module.py 同口径：
  road_offset_hold = ro_source ∈ {pair_slot, wall_occl, memory}
  road_offset_new  = dgeo_new and not hold
  lane_ok = lane_obs.state=="TRACK" and n_locked≥1 and x 非空
  lane_relock = lane_obs.state=="RELOCK"
plan 预览位按 x_preview_lane = x_preview_m / lane_w 近似（trace 只存米制
场景帧；两臂同一近似，不影响相对比较）。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_anchor_replay.py \
        --session 20261027_110753_1 --window 25.0:26.8
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import types

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.planner import LateralPlanner  # noqa: E402
from maaracing_master.plugins.speedrush.tracking import (  # noqa: E402
    DecisionOutput, DecisionState)

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
    if not isinstance(p, dict):
        return None
    if not p.get("valid") or p.get("u") is None:
        return types.SimpleNamespace(
            valid=False, reason=p.get("reason") or "", u_feedforward=0.0,
            x_preview_lane=0.0, v_preview_lane_s=0.0)
    xm = p.get("x")
    return types.SimpleNamespace(
        valid=True, reason=p.get("reason") or "",
        u_feedforward=float(p["u"]),
        x_preview_lane=(float(xm) / lane_w if isinstance(xm, (int, float))
                        and lane_w else 0.0),
        v_preview_lane_s=float(p.get("v") or 0.0))


def replay(g: list, cfg, window=None) -> dict:
    pl = LateralPlanner(cfg.planner)
    lane_w = 3.38
    out = dict(
        ticks=0, fresh=0, innovation=[], residual=[], teleports=0, max_dt=0.0,
        blind=0, brake_t=None, brake_x=None, brake_v=None, fidelity=[],
        gate_rej=0)
    prev_exec = None
    wall_t = (window[1] if window else None)
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
        out["ticks"] += 1
        if prev_exec is not None:
            d = abs(ex - prev_exec)
            out["max_dt"] = max(out["max_dt"], d)
            if d > 0.6:
                out["teleports"] += 1
        prev_exec = ex
        rec = row.get("executed_lane")
        if isinstance(rec, (int, float)):
            out["fidelity"].append(abs(ex - rec))
        if ro is not None and not hold:
            out["fresh"] += 1
            obs = ro - (pl._road_anchor or 0.0)
            # 新息用更新前状态重算：update 已消费，取 post-correction 残差
            out["residual"].append(abs(obs - ex))
        xt = row.get("x_target")
        u = pl.last_steer_raw
        if isinstance(xt, (int, float)) and abs(xt - ex) > 1.0 and abs(u) < 0.2:
            out["blind"] += 1
        if window and out["brake_t"] is None and window[0] <= t <= window[1] \
                and isinstance(xt, (int, float)):
            v = pl.state.v_lat_est
            if abs(v) > 1e-3 and cfg.planner.brake_decel_lane_s2 > 0.0:
                sgn = 1.0 if v > 0 else -1.0
                stop = ex + sgn * v * v / (2.0 * cfg.planner.brake_decel_lane_s2)
                edge = xt + sgn * cfg.planner.hold_deadband_lane
                if (stop - edge) * sgn > 0.0:
                    out["brake_t"] = t
                    out["brake_x"] = ex
                    out["brake_v"] = v
    return out


def replay_trace(g: list, cfg) -> list:
    """逐拍 (exec, v) 轨迹（dump 用）。"""
    pl = LateralPlanner(cfg.planner)
    lane_w = 3.38
    out = []
    for i, row in enumerate(g):
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
        out.append((pl.state.executed_lane, pl.state.v_lat_est))
    return out


def _stats(xs):
    if not xs:
        return (0.0, 0.0, 0.0)
    xs = sorted(xs)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    return (q(0.5), q(0.9), xs[-1])


def arm(cfg, **changes):
    return dataclasses.replace(cfg, planner=dataclasses.replace(
        cfg.planner, **changes))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--window", default=None,
                    help="刹停对照窗 mm:ss 形式 t0:t1（秒）")
    ap.add_argument("--dump", default=None,
                    help="逐拍对照窗 t0:t1（秒）：打印两臂 exec/v")
    args = ap.parse_args()
    win = None
    if args.window:
        a, b = args.window.split(":")
        win = (float(a), float(b))

    base = load_decision()
    old = arm(base, obs_jump_envelope_v_lane_s=0.0,
              obs_jump_envelope_a_lane_s2=0.0,
              obs_jump_envelope_cap_lane=base.planner.obs_jump_max_lane)
    new = base

    games = load_games(args.logs, args.session)
    print("session %s games=%d" % (args.session, len(games)))
    if args.dump:
        a, b = args.dump.split(":")
        dump = (float(a), float(b))
    else:
        dump = None
    for gi, g in enumerate(games):
        ra, rb = replay(g, old, win), replay(g, new, win)
        if dump:
            pa, pb = replay_trace(g, old), replay_trace(g, new)
            print("\n-- game%d 逐拍 (t | exec | 录exec | exec | v)：A 旧门 / B 包络"
                  % (gi + 1))
            for i in range(len(g)):
                t = i / TICK_HZ
                if not (dump[0] <= t <= dump[1]):
                    continue
                ro = g[i].get("road_offset")
                print("  %5.2f  obs=%6s rec=%6s | A %+5.2f v%+5.2f | B %+5.2f v%+5.2f" % (
                    t, ("%+.2f" % ro) if isinstance(ro, (int, float)) else "NA",
                    ("%+.2f" % g[i]["executed_lane"])
                    if isinstance(g[i].get("executed_lane"), (int, float))
                    else "NA",
                    pa[i][0], pa[i][1], pb[i][0], pb[i][1]))
        fa = _stats(ra["fidelity"]); fb = _stats(rb["fidelity"])
        ia = _stats(ra["residual"]); ib = _stats(rb["residual"])
        print("\n-- game%d (%d ticks) --" % (gi + 1, len(g)))
        print("  回放贴合实录 exec p50/p90: A=%.2f/%.2f  B=%.2f/%.2f" % (
            fa[0], fa[1], fb[0], fb[1]))
        print("  新鲜观测残差 p50/p90/max:  A=%.2f/%.2f/%.2f  B=%.2f/%.2f/%.2f"
              % (ia + ib))
        print("  单拍瞬移>0.6道:            A=%d  B=%d" % (
            ra["teleports"], rb["teleports"]))
        print("  贴边僵直(>1道且|u|<0.2):   A=%d  B=%d" % (
            ra["blind"], rb["blind"]))
        if win:
            def fmt(r):
                if r["brake_t"] is None:
                    return "未触发"
                return "t=%.2fs exec=%+.2f v=%+.2f" % (
                    r["brake_t"], r["brake_x"], r["brake_v"])
            print("  刹停窗 %s 首次触发:  A=%s  B=%s" % (
                args.window, fmt(ra), fmt(rb)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
