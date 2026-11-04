# -*- coding: utf-8 -*-
"""v_close_mps 运动观测判决探针（stdout-only 零文件写，trace-only）。

问题（刀二闸开判据，2026-10-08 预登记，见设计稿 §4 边界注记③）：
v_close_mps 目前是「已保存的运动观测」，不是「已验证的运动状态」。
本探针用带 v 的实机 trace 回答两件事，答案决定刀二形态：

  Q1 v(t) 稳定性——同一锚窗内逐拍评估的 v̂ 序列有多抖？
     指标：窗内 |v - 窗中位| 的 p50/p90，及相邻评估拍 |Δv|。
     对照：v̂ 本就是区间估计（锚窗累积），理论应比逐拍差分稳——实测检验。

  Q2 投影地平线——z(t+Δ) = z(t) − v(t)·Δ 在多大地平线内赢过持锚基线
     （z(t+Δ) ≈ z(t) 不动）？
     指标：按 Δt 分箱的投影误差 p50/p90 vs 持锚误差 p50/p90；投影在
     某箱内 p50 不胜过持锚即到达地平线。
     分层：按 src（ANCHOR/RESCALED_ANCHOR）与 v 强度分桶。

判据（预登记）：
  - 投影在所有 ≥100ms 的箱都不胜持锚基线 → v 不是可外推量，刀二不做
    速度投影（下一步要么更好的 tracking 状态估计，要么放弃该路径）；
  - 投影胜出到地平线 Δ* → 刀二阈值以 Δ* 收敛（留安全余量），政策按
    Δ* 设计，仍走 config 闸 + 实机 A/B（表现性质变更通道）。
  - 锚窗边界（age 回落=新锚重置）内的配对全部剔除——跨窗投影无效。

用法：.venv python probe_v_projection.py <trace.jsonl> [...]
（v 列 25ac9e1 起才有：旧 trace 报"无 v 数据"属预期。）
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

HORIZON_BINS = ((50, 100), (100, 200), (200, 300), (300, 400), (400, 500))
RESET_EPS_MS = 5.0   # age 回落超过此值 = 新锚重置（跨窗配对剔除）


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        beats = [json.loads(l) for l in f if l.strip()]
    beats.sort(key=lambda b: b["fid"])
    return beats


def track_series(beats: list[dict]) -> dict[int, list[dict]]:
    """track_id → 按拍序的读数序列（仅 dist 里有锚龄的车）。"""
    out: dict[int, list[dict]] = defaultdict(list)
    for b in beats:
        for e in b.get("dist") or []:
            if e.get("age") is None:
                continue
            out[e["id"]].append({
                "ts": b["ts_ns"], "fid": b["fid"],
                "m": e.get("m"), "v": e.get("v"),
                "age": e["age"], "src": e.get("src")})
    return out


def split_windows(seq: list[dict]) -> list[list[dict]]:
    """按锚窗切分：age 相对前拍回落 > eps = 新锚重置。"""
    wins: list[list[dict]] = []
    cur: list[dict] = []
    prev_age = None
    for r in seq:
        if prev_age is not None and r["age"] < prev_age - RESET_EPS_MS:
            wins.append(cur)
            cur = []
        cur.append(r)
        prev_age = r["age"]
    if cur:
        wins.append(cur)
    return wins


def p50(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def p90(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(int(len(xs) * 0.9), len(xs) - 1)] if xs else float("nan")


def q1_stability(all_wins: list[list[dict]]) -> None:
    """Q1：窗内 v̂ 抖动（对窗中位的偏差）与相邻评估拍 Δv。"""
    dev: list[float] = []
    dv: list[float] = []
    for w in all_wins:
        vs = [r["v"] for r in w if r["v"] is not None]
        if len(vs) < 3:
            continue
        med = sorted(vs)[len(vs) // 2]
        dev.extend(abs(v - med) for v in vs)
        dv.extend(abs(b - a) for a, b in zip(vs, vs[1:]))
    print(f"[Q1 v 稳定性] 窗数={sum(1 for w in all_wins if w)} "
          f"有效评估拍窗={sum(1 for w in all_wins if len([r for r in w if r['v'] is not None]) >= 3)}")
    if dev:
        print(f"  窗内偏差 |v−med|: p50={p50(dev):.2f} p90={p90(dev):.2f} m/s (n={len(dev)})")
        print(f"  相邻评估拍 |Δv| : p50={p50(dv):.2f} p90={p90(dv):.2f} m/s (n={len(dv)})")
    else:
        print("  无足够窗内评估序列（需带 v trace）")


def q2_projection(all_wins: list[list[dict]]) -> None:
    """Q2：分地平线箱的投影误差 vs 持锚基线误差。"""
    proj: dict[tuple, list[float]] = defaultdict(list)
    hold: dict[tuple, list[float]] = defaultdict(list)
    for w in all_wins:
        for i, ri in enumerate(w):
            if ri["v"] is None or ri["m"] is None:
                continue
            for rj in w[i + 1:]:
                if rj["m"] is None:
                    continue
                dt_ms = (rj["ts"] - ri["ts"]) / 1e6
                if dt_ms >= HORIZON_BINS[-1][1]:
                    break
                key = (ri["src"], next((b for b in HORIZON_BINS if b[0] <= dt_ms < b[1]), None))
                if key[1] is None:
                    continue
                dt_s = dt_ms / 1000.0
                proj[key].append(abs(rj["m"] - (ri["m"] - ri["v"] * dt_s)))
                hold[key].append(abs(rj["m"] - ri["m"]))
    print(f"[Q2 投影地平线] 配对分箱（投影误差 vs 持锚误差，同箱小者胜）")
    for src in sorted({k[0] for k in proj}):
        for bin_ in HORIZON_BINS:
            p, h = proj.get((src, bin_)), hold.get((src, bin_))
            if not p or not h:
                continue
            win = sum(1 for a, b in zip(p, h) if a < b) / len(p)
            print(f"  [{src:16s}] Δ∈[{bin_[0]:3d},{bin_[1]:3d})ms n={len(p):4d} "
                  f"投影 p50={p50(p):6.2f} p90={p90(p):6.2f}  "
                  f"持锚 p50={p50(h):6.2f} p90={p90(h):6.2f}  投影胜率={win:.2f}")


def main() -> None:
    all_wins: list[list[dict]] = []
    for path in sys.argv[1:]:
        beats = load(path)
        series = track_series(beats)
        wins = [w for seq in series.values() for w in split_windows(seq)]
        n_v = sum(1 for w in wins for r in w if r["v"] is not None)
        print(f"=== {os.path.basename(path)} === 轨数={len(series)} 锚窗={len(wins)} 带v拍={n_v}")
        if n_v == 0:
            print("  （无 v 数据：v 列 25ac9e1 起才有，旧 trace 属预期）")
            continue
        all_wins.extend(wins)
    if not all_wins:
        print("\n无可分析的带 v 数据——跑一局新 trace 后再执行本探针。")
        return
    print()
    q1_stability(all_wins)
    print()
    q2_projection(all_wins)


if __name__ == "__main__":
    main()
