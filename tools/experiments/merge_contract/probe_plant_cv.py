# -*- coding: utf-8 -*-
"""横向运动学参数留一局交叉验证（工作台，不入库）。

``probe_plant_fit`` 每段自由拟合 6 个参数，容易把漂移吸进 a/tau。这里
只在全局共享 (a, tau)，每段的 x0/v0/漂移用线性最小二乘闭式解出，再做
留一局验证：3 局拟合、第 4 局只解段内初值，报测试 RMS。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_plant_cv.py \
        --session 20261025_221703_1
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys

import numpy as np
from scipy.optimize import minimize

import probe_lateral_state as P
import probe_plant_fit as F

CONFIG_A = 6.0
CONFIG_TAU = 0.5


def steer_response(seg, us, a, b):
    """零初值下杆值产生的横位（精确分段积分）。"""
    xs = [0.0]
    v = 0.0
    x = 0.0
    for i, u in enumerate(us):
        h = seg[i + 1]["t"] - seg[i]["t"]
        e = math.exp(-b * h)
        v_ss = a * u / b
        x = x + (v - v_ss) * (1.0 - e) / b + v_ss * h
        v = v * e + v_ss * (1.0 - e)
        xs.append(x)
    return np.array(xs)


def seg_solve(seg, us, a, b):
    """固定 (a, b)，线性最小二乘解 x0/v0/漂移；返回残差平方和。"""
    t0 = seg[0]["t"]
    ts = np.array([s["t"] - t0 for s in seg])
    obs = np.array([s["x"] for s in seg])
    e = np.exp(-b * ts)
    A = np.stack([np.ones_like(ts), (1.0 - e) / b, ts, 0.5 * ts * ts], axis=1)
    y = obs - steer_response(seg, us, a, b)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    r = A @ coef - y
    return float(r @ r), len(seg)


def total_sse(segs, a, tau, indices):
    b = 1.0 / tau
    sse, n = 0.0, 0
    for i in indices:
        seg, us = segs[i]
        s, m = seg_solve(seg, us, a, b)
        sse += s
        n += m
    return sse, n


def fit_global(segs, indices):
    def obj(p):
        a, tau = p
        if not (0.5 < a < 60.0 and 0.15 < tau < 6.0):
            return 1e12
        return total_sse(segs, a, tau, indices)[0]

    best = None
    for a0 in (4.0, 6.0, 10.0):
        for tau0 in (0.5, 1.5, 3.0):
            r = minimize(obj, [a0, tau0], method="Nelder-Mead",
                         options={"xatol": 0.02, "fatol": 0.05,
                                  "maxiter": 400})
            if best is None or r.fun < best.fun:
                best = r
    return float(best.x[0]), float(best.x[1])


def rms(sse, n):
    return math.sqrt(sse / max(1, n))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=P.DEFAULT_LOGS)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    games = P.load_session(args.logs, args.session)
    per_game = []
    for label, rows in games:
        segs = F.segments(P.ticks_of(rows))
        per_game.append((label, segs))
    print(f"# 运动学参数留一局交叉验证：{args.session}\n")
    print("| 测试局 | 段数 | 训练 a | 训练 tau(s) | 测试RMS 拟合 | "
          "测试RMS 产线 | 改善 |")
    print("|---|---|---|---|---|---|---|")
    for gi, (label, segs) in enumerate(per_game):
        train = [i for j, (_l, s) in enumerate(per_game) if j != gi
                 for i in range(len(s))]
        # 训练集索引是拼接后的局部索引，需要按局重新算
        train_segs, test_segs = [], []
        for j, (_l, s) in enumerate(per_game):
            if j == gi:
                test_segs = s
            else:
                train_segs += s
        a, tau = fit_global(train_segs, list(range(len(train_segs))))
        _ = train
        b = 1.0 / tau
        sse_f = n_f = sse_c = n_c = 0.0
        for seg, us in test_segs:
            s, m = seg_solve(seg, us, a, b)
            sse_f += s
            n_f += m
            s, m = seg_solve(seg, us, CONFIG_A, 1.0 / CONFIG_TAU)
            sse_c += s
            n_c += m
        rf, rc = rms(sse_f, n_f), rms(sse_c, n_c)
        print(f"| {label} | {len(test_segs)} | {a:.1f} | {tau:.2f} | "
              f"{rf:.3f} | {rc:.3f} | {rc / rf:.2f}× |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
