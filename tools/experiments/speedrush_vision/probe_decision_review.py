# -*- coding: utf-8 -*-
"""决策行为对比（代理 vs 人工局影子，同口径一屏）。

背景：感知线收口后重开决策诊断。分数 = 里程 41/s 底盘 + 事件单位（金币 30/
超车 30/退场 180，score_account.py 模型）——里程底盘两边相同，分差全在事件
单位。本探针对比代理局与人工局（trace 为影子决策，executed/x_target 是人工
操作）的决策行为指纹：机会可见量、意图生命周期、动作churn、CHANGE 系节奏。

只读 debug 产物，不改生产。
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
EXP = Path(__file__).parent
BATCHES = {
    "代理": [(DBG / "20261017_170531_p1" / "trace_20261017_170531_p1.jsonl", 2276, 38.9),
             (DBG / "20261017_170616_p2" / "trace_20261017_170616_p2.jsonl", 4831, 40.2)],
    "人工": [(EXP / "manual_score_20261016" / "p1_trace.jsonl", None, 39.0),
             (EXP / "manual_score_20261016" / "p2_trace.jsonl", None, 42.9)],
}


def _p(xs, q):
    xs = sorted(xs)
    return float("nan") if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def report(label, games):
    for path, score, dur in games:
        rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        n = len(rows)

        state = collections.Counter(r.get("state") for r in rows)
        # CHANGE 系 episode（CHANGE/ABORT 连续段）
        eps, cur = [], 0
        for r in rows:
            if r.get("state") in ("CHANGE", "ABORT"):
                cur += 1
            elif cur:
                eps.append(cur)
                cur = 0
        if cur:
            eps.append(cur)

        reason = collections.Counter((r.get("reason") or "").split(":")[0]
                                     for r in rows)
        # 意图生命周期
        basis_alive = sum(1 for r in rows
                          if (r.get("intent") or {}).get("basis_ids"))
        gaps = sum(1 for r in rows
                   if (r.get("intent") or {}).get("basis") == "gap")
        d0s = [(r.get("intent") or {}).get("d0") for r in rows
               if (r.get("intent") or {}).get("d0") is not None]
        d0_move = [abs(b - a) for a, b in zip(d0s, d0s[1:])]
        d0_flips = sum(1 for a, b, c in zip(d0s, d0s[1:], d0s[2:])
                       if (b - a) * (c - b) < 0)

        # 机会可见量
        groups = [r.get("n_groups") or 0 for r in rows]
        with_groups = sum(1 for g in groups if g > 0)
        coins_t = [t for r in rows for t in (r.get("cr_coins") or [])]

        # passes
        passes = [p for r in rows for p in (r.get("passes") or [])]
        dmins = [p["d_min"] for p in passes if p.get("d_min") is not None]

        # 动作 churn
        ex = [r.get("executed_lane") for r in rows]
        ex_move = [abs(b - a) for a, b in zip(ex, ex[1:])
                   if a is not None and b is not None and a != b]
        xt = [r.get("x_target") for r in rows]
        xt_move = [abs(b - a) for a, b in zip(xt, xt[1:])
                   if a is not None and b is not None]

        rate = f"{score / dur:.0f}" if score else "?"
        print(f"\n=== {label} {path.parent.name[-2:]} === 拍={n} "
              f"时长={dur}s 分={score} 积分率={rate}/s")
        print(f"  state: {dict(state.most_common(5))}")
        print(f"  CHANGE系: {sum(eps)}拍/{len(eps)}次 episode "
              f"时长 P50={_p(eps, .5):.0f}拍 P90={_p(eps, .9):.0f}拍")
        print(f"  reason: {dict(reason.most_common(7))}")
        print(f"  意图: 带内依据活 {100 * basis_alive / n:.0f}% gap {100 * gaps / n:.0f}% | "
              f"d0挪动 P50={_p(d0_move, .5):.2f} 方向翻 {d0_flips}次")
        print(f"  币组: 可见拍 {100 * with_groups / n:.0f}% "
              f"组数 P50={_p(groups, .5):.0f} P90={_p(groups, .9):.0f} | "
              f"cr_coins 非空 {len(coins_t)}个 t P50={_p(coins_t, .5):.2f}")
        print(f"  passes: {len(passes)}次 d_min P50={_p(dmins, .5):.2f} "
              f"P10={_p(dmins, .1):.2f}")
        print(f"  动作: executed挪动 {len(ex_move)}次 |Δ| P50={_p(ex_move, .5):.3f} | "
              f"x_target挪动 P50={_p(xt_move, .5):.2f}")


for label, games in BATCHES.items():
    report(label, games)
