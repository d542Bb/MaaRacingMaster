# -*- coding: utf-8 -*-
"""短窗线位移横速观测：离线 A/B 回放（工作台，不入库）。

**问题**：控制器横速（模型积分 + 航向各半融合）与线横位 0.25s 位移差
0.6 道/s 中位（大于信号本身 0.3~0.45）。线位移的测量噪声只有 0.02~0.05
道/0.1s（probe_lane_flow 实测，比深度路心细 3~10 倍）。76db699 拆掉的
"线速"用的是 0.6s 因果斜率窗——反打处结构性滞后/反号，不是线测量不可用。

**本探针**：用 0.15~0.3s 窗口的线位移造横速观测，按锁线/重锁/断链/跳变
门控，**只覆盖控制律的横速（不改模型状态 v_lat_est）**，回放 A/B：

  A  = 现行（模型 + 航向融合）
  B1 = 有观测时纯用线位移速度
  B2 = 与现行各半

指标（A/B 相对，回放贴合实录是前提）：
  - 贴合：回放 exec 与实录 executed_lane 的偏差 p50/p90（发散大时只看相对）；
  - 深度残差：新鲜观测拍 |obs−exec|；瞬移 >0.6 道拍数；
  - 杆值总变差 Σ|Δu| 与反向次数（打方向是否更稳）；
  - 贴边僵直拍数、刹停预算触发拍数；
  - 控制横速 vs 0.25~0.4s 线位移（参考尺，与观测窗重叠，只看相对）。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_lane_velocity.py \
        --session 20261027_110753_1
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from probe_anchor_replay import (  # noqa: E402
    DEFAULT_LOGS, HOLD_SRC, TICK_HZ, _decision, _plan, load_games)
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.planner import LateralPlanner  # noqa: E402

W_MIN, W_MAX = 0.15, 0.30      # 观测窗（秒）
W_REF = (0.25, 0.40)           # 参考尺窗（与观测窗部分重叠，只看相对）
MAX_GAP = 0.16                 # 窗内相邻样本间隔上限（无检测空洞）
MAX_AGE_MS = 300.0             # 线检测龄上限
V_HOP = 3.2                    # 物理横向速度上限（道/s），跳变门
HOP_NOISE = 0.25               # 位移噪声地板（道）
V_CLAMP = 5.0                  # 与 _HEAD_V_MAX_FACTOR·v_lat_max 同口径


def _fit_velocity(samples, wmin, wmax):
    """窗内线位移的加权最小二乘斜率（道/s）；不满足连续性/跳变门返回 None。"""
    t_last = samples[-1][0]
    sel = [s for s in samples if wmin <= t_last - s[0] <= wmax]
    if len(sel) < 2:
        return None
    span = t_last - sel[0][0]
    if span < wmin:
        return None
    for a, b in zip(sel, sel[1:]):
        if b[0] - a[0] > MAX_GAP:
            return None
    dx = sel[-1][1] - sel[0][1]
    if abs(dx) > V_HOP * span + HOP_NOISE:
        return None
    ts = [s[0] for s in sel]
    xs = [s[1] for s in sel]
    mt = sum(ts) / len(ts)
    mx = sum(xs) / len(xs)
    vt = sum((a - mt) ** 2 for a in ts)
    if vt < 1e-9:
        return None
    v = sum((a - mt) * (b - mx) for a, b in zip(ts, xs)) / vt
    return max(-V_CLAMP, min(V_CLAMP, v))


def lane_velocities(g):
    """逐拍（线位移观测, 参考尺）；只吃新鲜 TRACK 检测帧，RELOCK 清窗。"""
    samples = []
    wit = [None] * len(g)
    ref = [None] * len(g)
    t = 0.0
    for i, row in enumerate(g):
        dt = float(row.get("dt") or 1 / TICK_HZ)
        t += dt
        ln = row.get("lane_obs") or {}
        st = ln.get("state")
        x = ln.get("x")
        fid = ln.get("det_fid")
        age = ln.get("det_age_ms")
        lane_ok = (
            st == "TRACK" and int(ln.get("n_locked") or 0) >= 1
            and isinstance(x, (int, float)) and abs(x) <= 2.6
            and (age is None or float(age) <= MAX_AGE_MS))
        if st == "RELOCK":
            samples.clear()
        if lane_ok and (not samples or samples[-1][2] != fid):
            samples.append((t, float(x), fid))
        while samples and t - samples[0][0] > 1.2:
            samples.pop(0)
        if lane_ok and samples and t - samples[-1][0] <= 0.15:
            wit[i] = _fit_velocity(samples, W_MIN, W_MAX)
            ref[i] = _fit_velocity(samples, *W_REF)
    return wit, ref


def replay(g, cfg, wit, ref, mode, window=None) -> dict:
    pl = LateralPlanner(cfg.planner)
    orig_fused = pl.head.fused
    cur = [None]

    def fused(v_model, gain):
        v0 = orig_fused(v_model, gain)
        w = cur[0]
        if w is None or mode == "A":
            return v0
        if mode == "B1":
            return w
        return max(-V_CLAMP, min(V_CLAMP, 0.5 * v0 + 0.5 * w))

    pl.head.fused = fused
    lane_w = 3.38
    out = dict(ticks=0, fidelity=[], residual=[], teleports=0, max_dt=0.0,
               blind=0, brake=0, tv=0.0, rev=0, verr=[], vref=[])
    prev_exec = None
    prev_u = None
    for i, row in enumerate(g):
        cur[0] = wit[i]
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
        u = pl.last_steer_raw
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
            obs = ro - (pl._road_anchor or 0.0)
            out["residual"].append(abs(obs - ex))
        xt = row.get("x_target")
        if isinstance(xt, (int, float)) and abs(xt - ex) > 1.0 and abs(u) < 0.2:
            out["blind"] += 1
        if isinstance(xt, (int, float)) and pl._brake_overshoot(xt):
            out["brake"] += 1
        if prev_u is not None:
            out["tv"] += abs(u - prev_u)
            if u * prev_u < 0 and abs(u - prev_u) > 0.15:
                out["rev"] += 1
        prev_u = u
        r = ref[i] if i < len(ref) else None
        if r is not None and abs(r) > 0.35:
            out["verr"].append(abs(pl._v_ctrl - r))
            out["vref"].append(r)
    return out


def stats(xs):
    xs = sorted(xs)
    if not xs:
        return (0.0, 0.0, 0)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    return (q(0.5), q(0.9), len(xs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--window", default=None, help="逐拍对照窗 t0:t1（秒）")
    args = ap.parse_args()

    base = load_decision()
    games = load_games(args.logs, args.session)
    print("session %s games=%d" % (args.session, len(games)))

    agg = {m: dict(fid=[], res=[], tv=0.0, rev=0, blind=0, brake=0,
                   tele=0, verr=[]) for m in ("A", "B1", "B2")}
    avail = dict(ticks=0, lane_ok=0, wit=0, wit_exc=0, abs=[], exc=0)
    for gi, g in enumerate(games):
        wit, ref = lane_velocities(g)
        for i, row in enumerate(g):
            avail["ticks"] += 1
            ln = row.get("lane_obs") or {}
            if ln.get("state") == "TRACK" and isinstance(ln.get("x"), (int, float)):
                avail["lane_ok"] += 1
            if wit[i] is not None:
                avail["wit"] += 1
                avail["abs"].append(abs(wit[i]))
                vm = abs(float((row.get("lat_ctrl") or {}).get("v_model") or 0.0))
                if vm > 0.35:
                    avail["wit_exc"] += 1
            if abs(float((row.get("lat_ctrl") or {}).get("v_model") or 0.0)) > 0.35:
                avail["exc"] += 1
        print("\n-- game%d (%d 拍) 观测可用=%d --" % (
            gi + 1, len(g), sum(1 for w in wit if w is not None)))
        for mode in ("A", "B1", "B2"):
            r = replay(g, base, wit, ref, mode)
            s = stats(r["fidelity"])
            q = stats(r["residual"])
            e = stats(r["verr"])
            agg[mode]["fid"].extend(r["fidelity"])
            agg[mode]["res"].extend(r["residual"])
            agg[mode]["tv"] += r["tv"]
            agg[mode]["rev"] += r["rev"]
            agg[mode]["blind"] += r["blind"]
            agg[mode]["brake"] += r["brake"]
            agg[mode]["tele"] += r["teleports"]
            agg[mode]["verr"].extend(r["verr"])
            print("  %-2s 贴合 %.2f/%.2f | 残差 %.2f/%.2f | Σ|Δu| %5.1f "
                  "反 %3d 僵 %2d 刹 %3d 瞬移 %2d | v误差 %.2f/%.2f" % (
                      mode, s[0], s[1], q[0], q[1], r["tv"], r["rev"],
                      r["blind"], r["brake"], r["teleports"], e[0], e[1]))
    print("\n== 汇总 %d 拍，线 TRACK=%d，短窗观测可用=%d (%.0f%%，激励段 %d/%d) ==" % (
        avail["ticks"], avail["lane_ok"], avail["wit"],
        100.0 * avail["wit"] / max(avail["ticks"], 1),
        avail["wit_exc"], avail["exc"]))
    if avail["abs"]:
        a = sorted(avail["abs"])
        print("   |观测| p50/p90/max = %.2f / %.2f / %.2f 道/s" % (
            a[len(a) // 2], a[9 * len(a) // 10], a[-1]))
    for mode in ("A", "B1", "B2"):
        s = stats(agg[mode]["fid"])
        q = stats(agg[mode]["res"])
        e = stats(agg[mode]["verr"])
        print("  %-2s 贴合 %.2f/%.2f | 残差 %.2f/%.2f | Σ|Δu| %6.1f "
              "反 %4d 僵 %3d 刹 %3d 瞬移 %3d | v误差 %.2f/%.2f" % (
                  mode, s[0], s[1], q[0], q[1], agg[mode]["tv"],
                  agg[mode]["rev"], agg[mode]["blind"], agg[mode]["brake"],
                  agg[mode]["tele"], e[0], e[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
