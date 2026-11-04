# -*- coding: utf-8 -*-
"""决策响应性对比（人工 vs 程序，executed 同口径：深度路心重锚实际横位）。

背景：hold 系占 80~82% 游戏时间、moving:reach 仅 2~5%——决策被事件化+冷却
闸住，横切车来了不响应。本探针对比人工局（executed=人工实际横位，零下发）
与程序局的响应指纹：转向吞吐量、钉死段（|Δex|<ε 连续）、方向翻转、
车近（near_dx）时的横向响应量。

只读 debug 产物，不改生产。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
EXP = Path(__file__).parent
BATCHES = {
    "程序": [DBG / "20261017_170531_p1" / "trace_20261017_170531_p1.jsonl",
             DBG / "20261017_170616_p2" / "trace_20261017_170616_p2.jsonl"],
    "人工": [EXP / "manual_score_20261016" / "p1_trace.jsonl",
             EXP / "manual_score_20261016" / "p2_trace.jsonl"],
}
EPS = 0.005          # 钉死阈值：|Δexecuted| < 0.005 道/拍
NEAR = 1.2           # 车近阈值（near_dx，道）


def _p(xs, q):
    xs = sorted(xs)
    return float("nan") if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def report(label, paths):
    for path in paths:
        rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        n = len(rows)
        dur = (rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9
        ex = [r.get("executed_lane") for r in rows]
        dts = [(b["ts_ns"] - a["ts_ns"]) / 1e9 for a, b in zip(rows, rows[1:])]

        # 转向吞吐量：|Δex| 总量 / 时长（道/秒）
        moves = [abs(b - a) for a, b in zip(ex, ex[1:])
                 if a is not None and b is not None]
        thr = sum(moves) / max(dur, 1e-6)

        # 钉死段：|Δex| < EPS 连续 run
        runs, cur = [], 0
        for a, b in zip(ex, ex[1:]):
            if a is not None and b is not None and abs(b - a) < EPS:
                cur += 1
            elif cur:
                runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        run_s = [c * 0.05 for c in runs]   # 拍间隔 ~50ms 近似

        # 方向翻转（拍间）
        signs = [(b - a) for a, b in zip(ex, ex[1:])
                 if a is not None and b is not None and abs(b - a) >= EPS]
        flips = sum(1 for a, b in zip(signs, signs[1:]) if a * b < 0)

        # 车近时的响应：near_dx < NEAR 的拍及其后 10 拍的 |Δex| 均值
        ndx = [r.get("near_dx") for r in rows]
        near_move, far_move = [], []
        for i, d in enumerate(ndx):
            win = [abs(ex[j + 1] - ex[j]) for j in range(i, min(i + 10, n - 1))
                   if ex[j] is not None and ex[j + 1] is not None]
            if win:
                (near_move if d is not None and d < NEAR else far_move).extend(win)

        print(f"\n=== {label} {path.parent.name if path.parent.name else path.name} === "
              f"拍={n} 时长={dur:.1f}s")
        print(f"  转向吞吐量={thr:.3f} 道/s | 方向翻转={flips}次 "
              f"({flips / max(dur, 1e-6):.1f}/s)")
        print(f"  钉死段(|Δex|<{EPS}): {len(runs)}段 长度 P50={_p(run_s, .5):.2f}s "
              f"P90={_p(run_s, .9):.2f} max={max(run_s) if run_s else 0:.2f}s")
        print(f"  车近(<{NEAR}道)响应 |Δex| P50={_p(near_move, .5):.3f} "
              f"P90={_p(near_move, .9):.3f} (n={len(near_move)}) | "
              f"无车拍 P50={_p(far_move, .5):.3f} (n={len(far_move)})")


for label, paths in BATCHES.items():
    report(label, paths)
