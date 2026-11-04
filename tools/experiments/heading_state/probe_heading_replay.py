#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读回放：把真实对局的横向观测喂给「朝向优先」观测器，量它的 1s 预测。

三个数，全部与 1s 后的实际横移（planner 帧 executed_lane）比：

1. **朝向预测**：当前朝向保持 1s（``x' = v·tanψ`` 的同一套 step）。这是本架构
   对「车会到哪」的答案，也是计划候选滚动的核心。
2. **旧模型预测**：trace 里录的 ``lat_ctrl.p1m``（保持当前杆值的旧双积分）。
3. **线上计划自己的 1s 预测**：计划的被选候选在 1s 处的横位增量。

回放只读 trace，不进生产链；通过 ``--root`` 或 APPDATA 找 logs/<run>/trace。

用法：
    python probe_heading_replay.py
    python probe_heading_replay.py --root D:/data/MaaRacingMaster --runs 3
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path

LANE_W = 3.5


def rows_of(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "fid" in r and "dt" in r:
                out.append(r)
    return out


def corr(a: list[float], b: list[float]) -> float:
    if len(a) < 10:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def stat(vals: list[float]) -> str:
    if not vals:
        return "—"
    vals = sorted(vals)
    return "中位 %.2f p90 %.2f" % (vals[len(vals) // 2],
                                   vals[min(len(vals) - 1, int(len(vals) * 0.9))])


def replay(rows: list[dict], planner):
    """按 trace 顺序喂观测器，返回逐拍（预测 1s 位移, 实际 1s 位移）对。

    真实值取 planner 帧 executed_lane 的 1s 差——它是这一层自己的状态，和预测
    同在道单位、同号。"""
    from maaracing_master.plugins.speedrush.planner import HeadingObserver

    obs = HeadingObserver(planner)
    t = [0.0]
    for r in rows:
        t.append(t[-1] + float(r.get("dt") or 0.05))
    pairs = []
    idx_by_time = []
    for i, r in enumerate(rows):
        lo = r.get("lane_obs") or {}
        th = lo.get("theta")
        n_locked = lo.get("n_locked")
        lane_x = lo.get("x")
        state = lo.get("state")
        obs.update(float(r.get("dt") or 0.05), lane_x,
                   state == "TRACK",
                   th, th is not None, lo.get("fid"),
                   lo.get("det_age_ms"), n_locked)
        idx_by_time.append(i)
        if obs.psi is None or obs.v_scale is None or obs.v_line is None:
            continue
        # 1s 后的那一拍
        j = i
        while j + 1 < len(rows) and t[j + 1] < t[i] + 1.0:
            j += 1
        if t[j] < t[i] + 0.9:
            continue
        x0 = float(r.get("executed_lane") or 0.0)
        x1 = float(rows[j].get("executed_lane") or 0.0)
        act = x1 - x0
        v = obs.v_scale
        # 朝向保持 1s：x += v·tanψ·dt，逐拍小步（与 step 同式）
        pred = 0.0
        n = int(round(1.0 / 0.05))
        for _ in range(n):
            pred += v * math.tan(obs.psi) / LANE_W * 0.05
        pairs.append({"act": act, "pred": pred,
                      "p1m": (r.get("lat_ctrl") or {}).get("p1m"),
                      "plan": _plan_1s(r), "t": t[i]})
    return pairs, obs


def _plan_1s(r: dict) -> float | None:
    """线上计划被选候选的 1s 横位增量（道）。"""
    pl = r.get("plan") or {}
    if not pl.get("valid") or not pl.get("cand"):
        return None
    ch = pl.get("chosen", -1)
    xs = pl["cand"]
    if ch is None or ch < 0 or ch >= len(xs):
        return None
    c = xs[ch]
    if len(c) < 21:
        return None
    lane_w = (r.get("scene") or {}).get("lane_w") or LANE_W
    return (c[20] - c[0]) / lane_w


def summarize(name: str, pairs: list[dict]) -> None:
    if len(pairs) < 30:
        print("  %s: 样本不足（%d）" % (name, len(pairs)))
        return
    # 只认「真的在横移」的窗：|实际位移| ≥ 0.1 道，否则噪声占比过高
    mv = [p for p in pairs if abs(p["act"]) >= 0.1]
    print("  %s: 1s 窗 %d（在横移 %d）" % (name, len(pairs), len(mv)))
    for key, kn in (("pred", "朝向保持"), ("p1m", "旧模型"), ("plan", "线上计划")):
        sub = [p for p in mv if isinstance(p.get(key), (int, float))]
        if len(sub) < 20:
            print("    %-6s: 样本不足（%d）" % (kn, len(sub)))
            continue
        err = [abs(p[key] - p["act"]) for p in sub]
        c = corr([p[key] for p in sub], [p["act"] for p in sub])
        print("    %-6s: 误差 %s 道 | 与实际相关 %.2f | n=%d"
              % (kn, stat(err), c, len(sub)))


def replay_control(rows: list[dict], cfg) -> dict:
    """把真实观测 + 决策目标喂进**新控制律**，与录制的旧杆值流对比。

    这不是闭环仿真（车不会按新杆值动），只回答两个问题：新律在真实观测上
    的**命令连续性**（抖不抖、有没有打满）与**状态跟随性**（executed 是否
    与录制值同量级）。"""
    from maaracing_master.plugins.speedrush.planner import LateralPlanner
    from maaracing_master.plugins.speedrush.tracking import (
        DecisionOutput, DecisionState)

    pl = LateralPlanner(cfg.planner)
    states = {s.name: s for s in DecisionState}
    new_cmds, old_cmds, ex_err, lat = [], [], [], []
    for r in rows:
        lo = r.get("lane_obs") or {}
        st = states.get(str(r.get("state")), DecisionState.CRUISE)
        x_t = r.get("x_target")
        out = DecisionOutput(
            schema_version=5, state=st, x_target=x_t,
            reason=str(r.get("reason") or ""), emitted_fid=int(r.get("fid") or 0),
            valid_until_fid=int(r.get("fid") or 0) + 3,
            maneuver=bool(r.get("maneuver")))
        th = lo.get("theta")
        cmd = pl.update(
            out, float(r.get("dt") or 0.05), int(r.get("fid") or 0),
            road_offset=r.get("road_offset"),
            road_offset_new=not bool(r.get("ro_hold")),
            road_offset_hold=bool(r.get("ro_hold")),
            lane_x=lo.get("x"), lane_ok=(lo.get("state") == "TRACK"),
            lane_relock=(lo.get("state") == "RELOCK"),
            lane_theta=th, lane_theta_ok=th is not None,
            lane_fid=lo.get("fid"), lane_age_ms=lo.get("det_age_ms"),
            lane_n_locked=lo.get("n_locked"))
        new_cmds.append(cmd.steer_x)
        old = r.get("steer_x")
        if isinstance(old, (int, float)):
            old_cmds.append(old)
        lat.append(pl.state.executed_lane)
        ex = r.get("executed_lane")
        if isinstance(ex, (int, float)):
            ex_err.append(abs(pl.state.executed_lane - ex))
    jit = [abs(new_cmds[i] - new_cmds[i - 1]) for i in range(1, len(new_cmds))]
    old_jit = [abs(old_cmds[i] - old_cmds[i - 1]) for i in range(1, len(old_cmds))]
    sat = sum(1 for c in new_cmds if abs(c) >= 32000) / max(len(new_cmds), 1)
    return {"jit": jit, "old_jit": old_jit, "sat": sat,
            "ex_err": ex_err,
            "same_dir": sum(1 for a, b in zip(new_cmds, old_cmds)
                            if (a > 0) == (b > 0) and a != 0 and b != 0),
            "n": min(len(new_cmds), len(old_cmds))}


def summarize_control(name: str, m: dict) -> None:
    if m["n"] < 100:
        print("  %s: 控制回放样本不足" % name)
        return
    nz = max(1, sum(1 for x in m["ex_err"] if x is not None))
    print("  控制回放：命令抖动 |Δu| %s（录制 %s）| 满杆拍 %.1f%%"
          " | executed 与录制差 %s 道 | 同号拍 %d/%d"
          % (stat(m["jit"]), stat(m["old_jit"]), 100.0 * m["sat"],
             stat(m["ex_err"]), m["same_dir"], m["n"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    root = Path(args.root)
    from maaracing_master.plugins.speedrush.config import (
        _read_decision)
    from maaracing_master.plugins.speedrush import DECISION_FILE
    cfg = _read_decision(DECISION_FILE)

    runs = sorted((root / "logs").glob("*/trace/speedrush.jsonl"),
                  key=lambda p: p.stat().st_mtime)[-args.runs:]
    if not runs:
        print("没有找到 logs/<run>/trace/speedrush.jsonl")
        return
    for p in runs:
        rows = rows_of(p)
        if not rows:
            continue
        pairs, obs = replay(rows, cfg.planner)
        print("=" * 78)
        print("[%s] ticks=%d" % (p.parent.parent.name, len(rows)))
        summarize(p.parent.parent.name, pairs)
        summarize_control(p.parent.parent.name, replay_control(rows, cfg))


if __name__ == "__main__":
    main()
