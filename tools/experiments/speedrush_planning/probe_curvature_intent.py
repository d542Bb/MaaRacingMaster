# -*- coding: utf-8 -*-
"""走廊横位口径 vs 曲率意图口径的闭环对照（C 类实验，不改生产）。

两套口径共用同一批人工局机会表、同一执行器（PD + 横移能力上限），
差异只在决策：
  A 现状：目标横位 + 换道时间 0.3+2.0·|dx|/道 + 0.3 道死区；
  B 曲率意图：横移意图（相对走廊中心）+ 运动学可达性（a_lat/v_lat_max）
    + 无死区，平滑放在横向加速度指令的变化率上。

评分来自机会表（未来帧可见的币/奖励），决策只看当前帧可见机会。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_curvature_intent.py \
        [--sess manual_20261019_173132_p2 ...]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "experiments" / "speedrush_planning"))

from probe_route_oracle import (  # noqa: E402
    DATA, GRID, COIN_W, REWARD_W, HORIZON_S, load_clock, future_points)

CAPTURE_LANE = 1.2
A_LAT = 6.0          # 道/s²，仓库拟合值
V_LAT_MAX = 2.5      # 道/s
KP = 4.0
KD = 2.0
JERK = 12.0          # 道/s³，B 口径的指令变化率上限
DEADBAND_LANE = 0.3
T_MOVE_BASE = 0.3
T_MOVE_PER_LANE = 2.0
CENTER_W = 0.02
CAR_W = 0.8
WALL_MARGIN = 0.35


def visible_opportunities(r):
    out = []
    for o in r["objs"]:
        if o[0] not in ("coin", "reward") or not o[6] or o[2] is None:
            continue
        if 0.0 <= o[2] <= HORIZON_S:
            out.append((float(o[2]), float(o[1]),
                        REWARD_W if o[0] == "reward" else COIN_W))
    return out


def schedule(rows, clk):
    """机会时间表：(绝对时钟, x, w)；同一机会多帧观测去重。"""
    merged = {}
    for i, r in enumerate(rows):
        if r["ego_m"] is None or clk[i] is None:
            continue
        base = -clk[i][1]
        for t_rel, x, w in future_points(rows, clk, i, HORIZON_S):
            key = (int((base + t_rel) / 0.5), int(np.floor(x / 0.5)))
            merged[key] = (base + t_rel, x, w)
    return sorted(merged.values())


def cars_at(r):
    out = []
    for o in r["objs"]:
        if o[0] != "car" or not o[6]:
            continue
        t = o[2] if o[2] is not None else (
            o[5] / 40.0 if o[5] is not None else None)
        if t is not None and 0.0 <= t <= HORIZON_S:
            out.append((float(t), float(o[1])))
    return out


def target_A(opps, cars, r, x, v, deadband=True):
    """现状口径：横位网格 + 换道时间可达 + 中心带死区。"""
    lane = r["lane_w"]
    best, best_x = -1e9, x
    for gx in GRID:
        tm = T_MOVE_BASE + T_MOVE_PER_LANE * abs(gx - x) / lane
        val = 0.0
        for t, ox, w in opps:
            if t >= tm and abs(gx - ox) <= CAPTURE_LANE * lane:
                val += w / (1.0 + 0.2 * t)
        for t, cx in cars:
            sig = 0.5 * lane
            val -= CAR_W * np.exp(-0.5 * ((gx - cx) / sig) ** 2)
        val -= CENTER_W * (gx / lane) ** 2
        if r["cor"]:
            half = max(0.0, r["cor"][1] / 2 - WALL_MARGIN) * lane
            if abs(gx) > half:
                continue
        if val > best:
            best, best_x = val, gx
    if deadband and abs(best_x - x) <= DEADBAND_LANE * lane:
        best_x = x
    return best_x


def reachable(dx, v, t):
    """从横向速度 v 出发，在 t 秒内能否覆盖 dx（加速度/速度双上限）。"""
    d = abs(dx)
    if d < 1e-6:
        return True
    s = 1.0 if dx > 0 else -1.0
    v0 = max(0.0, v * s)          # 同向初速；反向时忽略刹车时间（略乐观）
    t_acc = max(0.0, (V_LAT_MAX - v0) / A_LAT)
    room = v0 * t_acc + 0.5 * A_LAT * t_acc ** 2
    if room >= d:
        need = (-v0 + np.sqrt(v0 * v0 + 2 * A_LAT * d)) / A_LAT
    else:
        need = t_acc + (d - room) / V_LAT_MAX
    return t >= need


def target_B(opps, cars, r, x, v):
    """曲率意图口径：运动学可达 + 无死区。"""
    lane = r["lane_w"]
    best, best_x = -1e9, x
    for gx in GRID:
        val = 0.0
        for t, ox, w in opps:
            if reachable(gx - x, v / lane, t) and abs(gx - ox) <= CAPTURE_LANE * lane:
                val += w / (1.0 + 0.2 * t)
        for t, cx in cars:
            sig = 0.5 * lane
            val -= CAR_W * np.exp(-0.5 * ((gx - cx) / sig) ** 2)
        val -= CENTER_W * (gx / lane) ** 2
        if r["cor"]:
            half = max(0.0, r["cor"][1] / 2 - WALL_MARGIN) * lane
            if abs(gx) > half:
                continue
        if val > best:
            best, best_x = val, gx
    return best_x


def simulate(rows, clk, sched, mode, *, deadband=True, jerk=None,
             hidden=False):
    lane = None
    x = v = 0.0
    a_prev = 0.0
    score = 0.0
    eaten = set()
    jumps_a, jumps_x, xs = [], [], []
    prev_abs = None
    for i, r in enumerate(rows):
        if r["ego_m"] is None or r["lane_w"] is None or clk[i] is None:
            continue
        lane = r["lane_w"]
        abs_now = -clk[i][1]
        if prev_abs is None:
            prev_abs = abs_now - 0.05
        dt = max(0.02, min(0.1, abs_now - prev_abs))
        # 捕获：这段时间内到站的机会
        for k, (ta, ox, w) in enumerate(sched):
            if k in eaten or not (prev_abs < ta <= abs_now):
                continue
            if abs(x - ox) <= CAPTURE_LANE * lane:
                eaten.add(k)
                score += w
        if hidden:
            opps = [(ta - abs_now, ox, w) for ta, ox, w in sched
                    if 0.0 <= ta - abs_now <= HORIZON_S]
        else:
            opps = visible_opportunities(r)
        cars = cars_at(r)
        if mode == "A":
            xt = target_A(opps, cars, r, x, v, deadband=False)
        else:
            xt = target_B(opps, cars, r, x, v)
        if deadband and abs(xt - x) <= DEADBAND_LANE * lane:
            xt = x
        a = KP * (xt - x) - KD * v
        a = max(-A_LAT, min(A_LAT, a))
        if jerk:
            lim = jerk * dt
            a = max(a_prev - lim, min(a_prev + lim, a))
        jumps_a.append(abs(a - a_prev))
        jumps_x.append(abs(v * dt))
        a_prev = a
        v = max(-V_LAT_MAX, min(V_LAT_MAX, v + a * dt))
        x += v * dt
        xs.append(x)
        prev_abs = abs_now
    return {
        "score": score,
        "a_jump_med": statistics.median(jumps_a),
        "a_jump_p90": statistics.quantiles(jumps_a, n=10)[8],
        "x_jump_med": statistics.median(jumps_x),
        "abs_x_med": statistics.median(abs(q) for q in xs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="走廊横位 vs 曲率意图 闭环对照")
    ap.add_argument("--sess", action="append")
    args = ap.parse_args(argv)
    sessions = args.sess or sorted(
        p.name[len("potential_shadow_"):-len(".jsonl")]
        for p in DATA.glob("potential_shadow_*.jsonl"))
    tot = {"A": 0.0, "B": 0.0}
    print(f"{'局':>28} {'机会上限':>7} {'A 得分':>7} {'B 得分':>7}"
          f" {'A 指令跳':>8} {'B 指令跳':>8}")
    for s in sessions:
        p = DATA / f"potential_shadow_{s}.jsonl"
        if not p.exists():
            continue
        rows = [json.loads(l) for l in p.open(encoding="utf-8")]
        clk = [load_clock(s)(r["ts"]) for r in rows]
        sched = schedule(rows, clk)
        total = sum(w for _, _, w in sched)
        res = {m: simulate(rows, clk, sched, m) for m in ("A", "B")}
        tot["A"] += res["A"]["score"]
        tot["B"] += res["B"]["score"]
        print(f"{s:>28} {total:>7.0f} {res['A']['score']:>7.0f}"
              f" {res['B']['score']:>7.0f} {res['A']['a_jump_med']:>8.2f}"
              f" {res['B']['a_jump_med']:>8.2f}")
    print(f"\n合计 A={tot['A']:.0f}  B={tot['B']:.0f}"
          f"  提升 {tot['B'] / max(1e-9, tot['A']) - 1:+.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
