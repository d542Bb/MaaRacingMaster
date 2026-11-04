# -*- coding: utf-8 -*-
"""单权威影子重放（C 类实验，只读既有 trace，不碰真机/生产）。

回答一个问题：把决策收成单一权威 + 满权重用到感知边缘（3.5s）+ 秒级承诺，
能恢复 BEHAVIOR_GAPS_20261020 清单里哪几条能力。

重放口径：逐拍吃 trace `sel_dbg`（ego/c0/走廊缘/车/币到站时间）+ 产线
`law`/`step`（跟踪律与横向运动学单一真源，参数真源 decision.json）。
三个变体只差决策层，执行层（杆幅上限/运动学）与产线一致：

  V1 基线   = trace 实际 executed（不仿）
  V2 满权重 = 3.5s 内事件全权重取最高值目标，无承诺（每拍重选）
  V3 承诺   = V2 + 秒级承诺（目标保持到完成/消失/更高值出现）

公平性边界：对象流取自实际运行轨迹的录写——V2/V3 自车横位不同，币到站
时间按恒油门近似有效；本探针只回答「同一段感知喂进来，决策层变化恢复
什么行为」，不回答真机闭环得分。
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.config import load_decision
from maaracing_master.plugins.speedrush.planner import law, step

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = [
    ("P1", DBG / "20261020_202758_p1" / "trace_20261020_202758_p1.jsonl"),
    ("P2", DBG / "20261020_202843_p2" / "trace_20261020_202843_p2.jsonl"),
]

HORIZON_S = 3.5          # 满权重边界（感知 p90 到站 2.6~3.2s）
VAL_PASS, VAL_COIN = 120.0, 90.0
GRAZE_GAP = 0.75         # 擦车目标距车心（道）
CAP = 0.45               # 执行层杆幅上限（与产线机动态一致，隔离决策变量）
BAND = 0.30              # 归中带（道）
COMMIT_MARGIN = 100.0    # 承诺被更高值打破的阈值（奖励车级）
LOSE_S = 0.6             # 目标对象从感知消失多久放弃


def load_rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def objects_at(row, t_now):
    """trace 行 → (cars, coins)：(x 道, t 秒)；t 缺失=不参与。"""
    sd = row.get("sel_dbg") or {}
    cars = [(x, t) for x, t in (sd.get("car") or []) if t is not None and -0.2 <= t <= HORIZON_S]
    coins = [(x, t) for x, t in (sd.get("coin") or []) if t is not None and -0.2 <= t <= HORIZON_S]
    return cars, coins


def pick_target(cars, coins, ego, c0, commit, wall):
    """单权威目标选择。承诺内只跟随当前对象（按邻近匹配），对象消失才重选。"""
    cands = []
    for x, t in cars:
        room_l = x - wall
        room_r = wall - x
        side = 1.0 if room_r >= room_l else -1.0
        tx = max(-wall + 0.35, min(wall - 0.35, x + side * GRAZE_GAP))
        cands.append((VAL_PASS, t, tx, "pass"))
    for x, t in coins:
        cands.append((VAL_COIN, t, max(-wall + 0.35, min(wall - 0.35, x)), "coin"))
    if commit is not None:
        kind0, x0 = commit
        same = [c for c in cands if c[3] == kind0 and abs(c[2] - x0) < 0.6]
        if same:
            c = min(same, key=lambda c: c[1])      # 承诺内：跟随同一目标
            return c[2], c[0], c[3], True
    if not cands:
        return None
    best = max(cands, key=lambda c: (c[0], -c[1]))
    return best[2], best[0], best[3], False


def replay(rows, planner_p, committed: bool):
    dt = 0.05
    wall = 2.0
    x = rows[0].get("executed_lane") or 0.0
    v = rows[0].get("v_lat_est") or 0.0
    steer = 0.0
    commit = None
    lose_t = 0.0
    out = []
    for row in rows:
        sd = row.get("sel_dbg") or {}
        if not sd or sd.get("c0") is None:
            out.append({"x": x, "v": v, "u": None, "has_obj": False})
            x, v, steer = step(x, v, steer, 0.0, dt, planner_p)
            continue
        c0 = sd["c0"]
        cars, coins = objects_at(row, 0.0)
        has_event = bool(cars or coins)
        target = None
        switched = False
        if has_event:
            got = pick_target(cars, coins, x, c0, commit if committed else None, wall)
            if got is not None:
                target, value, kind, held = got
                if committed:
                    if held:
                        lose_t = 0.0
                    else:
                        # 对象消失超过 LOSE_S 才换新目标；期间沿旧目标走
                        if commit is not None and lose_t <= LOSE_S:
                            target = commit[1]
                            held = True
                        else:
                            commit = (kind, target)
                            lose_t = 0.0
                            switched = True
                else:
                    switched = commit is None or abs(target - commit[1]) > 0.6
                    commit = (kind, target)
                lose_t = 0.0 if held else lose_t
        elif committed and commit is not None:
            lose_t += dt
            if lose_t <= LOSE_S:
                target = commit[1]
            else:
                commit = None
        mode = "event" if target is not None and has_event else (
            "hold" if target is not None else "center")
        if target is None:
            # 无机会：回正中（带外直奔中心，带内保持）
            target = c0 if abs(x - c0) > BAND else x
            if committed:
                commit = None
        target = max(-wall + 0.35, min(wall - 0.35, target))
        u = law(target - x, v, planner_p, gentle=False, cap=CAP)
        x, v, steer = step(x, v, steer, u, dt, planner_p)
        out.append({"x": x, "v": v, "u": u, "has_obj": has_event,
                    "target": target, "n_obj": len(cars) + len(coins),
                    "c0": c0, "cars": cars, "coins": coins,
                    "switched": switched, "mode": mode})
    return out


def _pct(a, q):
    a = sorted(a)
    return a[min(len(a) - 1, int(q * len(a)))] if a else float("nan")


def displacement(series, w):
    out = []
    n = len(series)
    for i in range(n):
        j = i
        while j < n - 1 and series[j + 1][0] - series[i][0] <= w:
            j += 1
        out.append(abs(series[j][1] - series[i][1]))
    out.sort()
    return out


def metrics(label, xs, steers, times, rows):
    dur = times[-1] - times[0]
    # 舵角突变（>=4/s）与方向翻转
    jumps = 0
    flips = 0
    onset = []
    prev = None
    for i, (t, u) in enumerate(zip(times, steers)):
        if u is None:
            prev = None
            continue
        if prev is not None:
            dt = t - prev[0]
            if dt > 0 and abs(u - prev[1]) / dt >= 4.0:
                jumps += 1
                cars = rows[i].get("_cars") or []
                ts_ = [c[1] for c in cars if c[1] > 0]
                if ts_:
                    onset.append(min(ts_))
            if i + 1 < len(times) and steers[i + 1] is not None:
                nxt = steers[i + 1]
                if abs(nxt - u) >= 0.05 and (nxt - u) * (u - prev[1]) < 0:
                    flips += 1
        prev = (t, u)
    d05 = displacement(list(zip(times, xs)), 0.5)
    d20 = displacement(list(zip(times, xs)), 2.0)
    return {
        "label": label, "dur": dur,
        "jumps_10s": jumps / dur * 10,
        "flips_10s": flips / dur * 10,
        "onset_p50": _pct(onset, 0.5) if onset else None,
        "onset_p90": _pct(onset, 0.9) if onset else None,
        "d05_p50": _pct(d05, 0.5), "d05_p90": _pct(d05, 0.9),
        "d20_p50": _pct(d20, 0.5), "d20_p99": _pct(d20, 0.99),
    }


def event_capture(rows, xs):
    """事件捕获代理：币/车到站拍（t 最小）自车是否贴住。"""
    coins = passes = 0
    for row, x in zip(rows, xs):
        coins_list = row.get("_coins") or []
        cars_list = row.get("_cars") or []
        for cx, t in coins_list:
            if t is not None and t <= 0.05 and abs(x - cx) <= 0.35:
                coins += 1
                break
        for cxx, t in cars_list:
            if t is not None and t <= 0.05 and abs(x - cxx) <= 0.75:
                passes += 1
                break
    return coins, passes


def main():
    cfg = load_decision()
    p = cfg.planner
    for tag, path in TRACES:
        rows = load_rows(path)
        t0 = rows[0]["ts_ns"]
        times = [(r["ts_ns"] - t0) / 1e9 for r in rows]
        for r in rows:
            sd = r.get("sel_dbg") or {}
            r["_cars"] = [(x, t) for x, t in (sd.get("car") or []) if t is not None]
            r["_coins"] = [(x, t) for x, t in (sd.get("coin") or []) if t is not None]
        base_x = [r.get("executed_lane") or 0.0 for r in rows]
        base_u = [r.get("steer_norm") for r in rows]
        print(f"\n===== {tag}  n={len(rows)}  {times[-1]:.1f}s =====")
        table = [metrics("V1 基线", base_x, base_u, times, rows)]
        for label, committed in (("V2 满权重3.5s", False), ("V3 满权重+承诺", True)):
            sim = replay(rows, p, committed)
            xs = [s["x"] for s in sim]
            us = [s["u"] for s in sim]
            m = metrics(label, xs, us, times, rows)
            coins, passes = event_capture(rows, xs)
            m["coins_10s"] = coins / times[-1] * 10
            m["passes_10s"] = passes / times[-1] * 10
            m["switches_10s"] = sum(1 for s in sim if s.get("switched")) / times[-1] * 10
            # 回中拍的在带内占比（占位/归中；承诺尾拍不算回中）
            inband = [abs(s["x"] - s["c0"]) <= BAND + 0.05 for s in sim
                      if s.get("mode") == "center" and s.get("c0") is not None]
            m["noevent_inband"] = sum(inband) / len(inband) if inband else None
            m["center_frac"] = (sum(1 for s in sim if s.get("mode") == "center")
                                / len(sim) if sim else None)
            table.append(m)
        bcoins, bpasses = event_capture(rows, base_x)
        table[0]["coins_10s"] = bcoins / times[-1] * 10
        table[0]["passes_10s"] = bpasses / times[-1] * 10
        hdr = f"{'变体':<14} {'突变/10s':>8} {'翻转/10s':>8} {'换目标/10s':>9} {'起步p50':>7} {'0.5s位移p50':>10} {'2s位移p99':>9} {'币/10s':>6} {'车贴/10s':>7} {'无事件在带内':>10}"
        print(hdr)
        for m in table:
            o50 = "—" if m["onset_p50"] is None else f"{m['onset_p50']:.2f}s"
            nib = "—" if m.get("noevent_inband") is None else f"{m['noevent_inband']:.0%}"
            sw = m.get("switches_10s")
            row = (f"{m['label']:<14} {m['jumps_10s']:>8.1f} {m['flips_10s']:>8.1f} "
                   f"{('—' if sw is None else f'{sw:.1f}'):>9} {o50:>7} "
                   f"{m['d05_p50']:>10.2f} {m['d20_p99']:>9.2f} "
                   f"{m.get('coins_10s', float('nan')):>6.1f} {m.get('passes_10s', float('nan')):>7.1f} "
                   f"{nib:>10}")
            print(row)


if __name__ == "__main__":
    main()
