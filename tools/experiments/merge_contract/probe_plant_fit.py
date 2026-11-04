# -*- coding: utf-8 -*-
"""横向运动学灰盒辨识（工作台；被 `probe_plant_cv` 当模块复用）。

只读会话流 trace。用记录的低通杆值 ``steer_norm`` 与线观测横位，拟合
``v' = a*steer - b*v, x' = v`` 的 a、b，判断产线运动学（a=6, tau=0.5s）
与实车差多少。每个连续锁线段独立拟合 x0/v0，再汇总 a、b；拟合用 soft_l1
抗深度锚小跳。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_plant_fit.py \
        --session 20261025_221703_1
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys

import numpy as np
from scipy.optimize import least_squares

import probe_lateral_state as P

MIN_SEG_S = 1.5
MIN_RANGE_LANE = 0.5
CONFIG_A = 6.0
CONFIG_B = 2.0      # 1/tau_align_s, tau=0.5s


def segments(ticks):
    """连续锁线样本切段，同时给出每个区间的平均低通杆值。"""
    samples = P.smooth_and_derive(P.line_samples(ticks))
    segs, cur, prev = [], [], None
    for s in samples:
        if prev is not None and s["t"] - prev > P.MAX_SAMPLE_GAP_S:
            if len(cur) >= 5:
                segs.append(cur)
            cur = []
        cur.append(s)
        prev = s["t"]
    if len(cur) >= 5:
        segs.append(cur)
    out = []
    for seg in segs:
        dur = seg[-1]["t"] - seg[0]["t"]
        rng = max(s["x"] for s in seg) - min(s["x"] for s in seg)
        if dur < MIN_SEG_S or rng < MIN_RANGE_LANE:
            continue
        us = []
        ok = True
        for i in range(len(seg) - 1):
            t0, t1 = seg[i]["t"], seg[i + 1]["t"]
            vals = [float(k["steer"]) for k in ticks
                    if k["steer"] is not None and t0 <= k["t"] <= t1]
            if not vals:
                ok = False
                break
            us.append(sum(vals) / len(vals))
        if ok:
            out.append((seg, us))
    return out


def simulate(seg, us, a, b, dx0, v0, d0, d1):
    """``x' = v + d0 + d1*t``：d0 吸收弯道/锚漂的常速项，d1 吸收其变化率。"""
    t0 = seg[0]["t"]
    xs = [seg[0]["x"] + dx0]
    v = v0
    x = xs[0]
    for i, u in enumerate(us):
        h = seg[i + 1]["t"] - seg[i]["t"]
        e = math.exp(-b * h)
        v_ss = a * u / b
        x = (x + (v - v_ss) * (1.0 - e) / b + v_ss * h
             + (d0 + d1 * (seg[i]["t"] - t0)) * h)
        v = v * e + v_ss * (1.0 - e)
        xs.append(x)
    return np.array(xs)


def fit_segment(seg, us, drift: int):
    obs = np.array([s["x"] for s in seg])
    n = 4 + drift

    def resid(p):
        d0 = p[4] if drift >= 1 else 0.0
        d1 = p[5] if drift >= 2 else 0.0
        return simulate(seg, us, p[0], p[1], p[2], p[3], d0, d1) - obs

    lo = [0.5, 0.15, -2.5, -3.0, -2.0, -2.0]
    hi = [80.0, 12.0, 2.5, 3.0, 2.0, 2.0]
    best = None
    for a0, b0 in ((CONFIG_A, CONFIG_B), (12.0, 1.0), (3.0, 3.0)):
        p0 = [a0, b0, 0.0, 0.0, 0.0, 0.0][:n]
        r = least_squares(
            resid, p0, bounds=(lo[:n], hi[:n]),
            loss="soft_l1", f_scale=0.08, max_nfev=300)
        if best is None or r.cost < best.cost:
            best = r
    a, b = best.x[0], best.x[1]
    d0 = best.x[4] if drift >= 1 else 0.0
    d1 = best.x[5] if drift >= 2 else 0.0
    pred = simulate(seg, us, a, b, best.x[2], best.x[3], d0, d1)
    rms_cfg = float(np.sqrt(np.mean(
        (simulate(seg, us, CONFIG_A, CONFIG_B, 0.0, 0.0, 0.0, 0.0)
         - obs) ** 2)))
    return {"a": float(a), "b": float(b),
            "dur": seg[-1]["t"] - seg[0]["t"],
            "rms_fit": float(np.sqrt(np.mean((pred - obs) ** 2))),
            "rms_cfg": rms_cfg}


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=P.DEFAULT_LOGS)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"# 横向运动学辨识：{args.session}\n")
    games = P.load_session(args.logs, args.session)
    modes = ((0, "无漂移项"), (1, "+常速漂移"), (2, "+线性漂移"))
    for drift, name in modes:
        print(f"## {name}\n")
        print("| 局 | 段数 | a p10/50/90 | tau=1/b p10/50/90(s) | "
              "拟合RMS | 产线a=6,tau=0.5 RMS |")
        print("|---|---|---|---|---|---|")
        all_a, all_b, all_fit, all_cfg, all_dur = [], [], [], [], []
        for label, rows in games:
            segs = segments(P.ticks_of(rows))
            fits = [fit_segment(seg, us, drift) for seg, us in segs]
            a = [f["a"] for f in fits]
            b = [f["b"] for f in fits]
            tau = [1.0 / f["b"] for f in fits]
            rf = [f["rms_fit"] for f in fits]
            rc = [f["rms_cfg"] for f in fits]
            if not fits:
                print(f"| {label} | 0 | - | - | - | - |")
                continue
            all_a += a
            all_b += b
            all_fit += rf
            all_cfg += rc
            all_dur += [f["dur"] for f in fits]
            print(f"| {label} | {len(fits)} | "
                  f"{pct(a, .1):.1f}/{pct(a, .5):.1f}/{pct(a, .9):.1f} | "
                  f"{pct(tau, .1):.2f}/{pct(tau, .5):.2f}/{pct(tau, .9):.2f} | "
                  f"{statistics.median(rf):.3f} | {statistics.median(rc):.3f} |")
        if all_a:
            print(f"\n- 汇总 {len(all_a)} 段（{sum(all_dur):.0f}s）："
                  f"a 中位 {statistics.median(all_a):.1f}（产线 6.0），"
                  f"tau 中位 {1/statistics.median(all_b):.2f}s（产线 0.50s）")
            print(f"- 开环 RMS：拟合 {statistics.median(all_fit):.3f} 道，"
                  f"产线参数 {statistics.median(all_cfg):.3f} 道")
            z_cfg = (CONFIG_A * 0.2 + CONFIG_B) / (
                2 * math.sqrt(0.7 * CONFIG_A))
            a_med = statistics.median(all_a)
            z_fit = (a_med * 0.2 + statistics.median(all_b)) / (
                2 * math.sqrt(0.7 * a_med))
            print(f"- 闭环阻尼（含自回正）：产线 ζ={z_cfg:.2f}，"
                  f"按辨识中位 ζ={z_fit:.2f}（kp=0.7, kd=0.2）\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
