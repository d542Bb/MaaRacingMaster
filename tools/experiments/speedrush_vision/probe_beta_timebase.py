# -*- coding: utf-8 -*-
"""⑥b β 速度修正的时间基×增益重标定探针（P1 授权序列第 4 步，2026-10-08）。

背景（台账 §4A 判据冻结）：planner ⑥b 的 β·r 分母曾是控制拍 dt（33~66ms），
而新息 r 是跨新证据间隔（驻留协议 ~200ms）积累的——速度修正被放大 4~6×，
v_lat_est 拍级振荡 ±1 道/s 的机制根因。产线已改累计帧钟 T_e=Σdt（逐拍新证据
时 T_e=dt，旧行为逐位保留）。本探针在存量 trace 上离线回放，回答：
旧时间基下"调出来"的 β=0.2 换到正确时间基后该取多少——按抖动-响应权衡
重定，不机械沿用也不机械换算。

方法（自包含，不 import 产线代码；⑥+⑥b 数学按 planner.py 判据冻结后的
语义复刻，参数读 resources/policy/decision.json 数据文件）：
  回放输入 = trace 逐拍的 dt / steer_norm（recorded，隔离控制律）/
  road_offset / road_offset_new（=dgeo_new）/ reanchor_lane。
  自验证 = 旧时间基×β0.2 回放应对账 recorded v_lat_est（残差仅 4 位舍入）。
  变体 = 时间基{old, accum} × β{0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2}。
指标（对 recorded executed_lane 的 0.6s 低通导数 v_ref——粗真值，口径见下）：
  抖动 = 新证据拍 |Δv| P50/P95；响应 = 机动段 v~v_ref 互相关滞后与相关系数；
  偏置 = 巡航段 mean(v−v_ref)；恢复 = 机动段结束后 |v−v_ref|<0.3 首达延迟。
stdout-only，不改任何文件。
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CFG_PATH = REPO / "maaracing_master" / "plugins" / "speedrush" / "resources" / \
    "policy" / "decision.json"
TRACE_ROOT = Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "data" / \
    "speedrush" / "control_traces"

V_REF_TAU_S = 0.6      # 粗真值低通：压掉 α/β 修正跳变（0.2s 尺度），留机动包络
MANEUVER_V = 0.3       # 机动段判据：|v_ref| 超此值（道/s）
MANEUVER_MIN_TICKS = 5
RECOVER_BAND = 0.3     # 恢复判据：|v−v_ref| 回到带内
BETA_GRID = (0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2)


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))] if xs else float("nan")


def _load_cfg():
    d = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    p = d["planner"]
    return {k: p[k] for k in ("a_lat_gain", "tau_align_s", "v_lat_max",
                              "obs_alpha", "obs_beta", "anchor_max_off",
                              "anchor_stale_s", "obs_jump_max_lane")}


def _load_ticks(path):
    """trace → 回放输入序列；剔除缺列/首拍。"""
    ticks = []
    prev_fid = None
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("dt") is None or r.get("steer_norm") is None:
            continue
        ticks.append({
            "dt": r["dt"],
            "steer": r["steer_norm"],
            "ro": r.get("road_offset"),
            # 产线语义：β 修正只认 dgeo_new 且 ro 非 None 的拍
            "new": bool(r.get("dgeo_new")) and r.get("road_offset") is not None,
            "reanchor": r.get("reanchor_lane"),
            "exec_rec": r.get("executed_lane"),
            "v_rec": r.get("v_lat_est"),
            "_fid": r.get("fid"),
        })
        prev_fid = r.get("fid")
    return [t for t in ticks if t["exec_rec"] is not None and t["v_rec"] is not None]


def _replay(ticks, cfg, time_base, beta):
    """复刻 ⑤之后的 ⑥+⑥b（顺序与 planner.py 一致：⑥ 先、⑥b 后）。

    time_base: "old"=β·r/dt（改前口径）；"accum"=β·r/T_e（判据冻结口径）。
    返回逐拍 (v, exec) 与新证据拍索引。"""
    v = 0.0
    exec_l = 0.0
    anchor = None
    stale_t = 0.0
    t_vcorr = 0.0
    out_v, out_exec, new_idx = [], [], []
    for t in ticks:
        dt = t["dt"]
        t_vcorr += dt
        if t["reanchor"] is not None:
            exec_l = t["reanchor"]
            v = 0.0
            anchor = None
            t_vcorr = 0.0
        # ⑥ 横向运动学（双积分；recorded steer_norm 已含 ⑤ 低通）
        v = v + (cfg["a_lat_gain"] * t["steer"] - v / cfg["tau_align_s"]) * dt
        v = max(-cfg["v_lat_max"], min(cfg["v_lat_max"], v))
        exec_l += v * dt
        # ⑥b 路观测修正
        ro = t["ro"]
        if ro is not None:
            if anchor is None:
                if abs(ro) <= cfg["anchor_max_off"]:
                    anchor = 0.0
                    stale_t = 0.0
                    t_vcorr = 0.0
                else:
                    stale_t += dt
                    if stale_t >= cfg["anchor_stale_s"]:
                        exec_l, v, anchor = ro, 0.0, 0.0
                        stale_t = t_vcorr = 0.0
            else:
                r = (ro - anchor) - exec_l
                if abs(r) <= cfg["obs_jump_max_lane"]:
                    exec_l += cfg["obs_alpha"] * r
                    if t["new"]:
                        denom = dt if time_base == "old" else max(t_vcorr, dt)
                        v = max(-cfg["v_lat_max"], min(
                            cfg["v_lat_max"], v + beta * r / denom))
                        t_vcorr = 0.0
                        new_idx.append(len(out_v))
                    stale_t = 0.0
                else:
                    stale_t += dt
                    if stale_t >= cfg["anchor_stale_s"]:
                        exec_l, v, anchor = ro, 0.0, 0.0
                        stale_t = t_vcorr = 0.0
        out_v.append(v)
        out_exec.append(exec_l)
    return out_v, out_exec, new_idx


def _v_ref(ticks):
    """粗真值：recorded executed_lane 的 0.6s 一阶低通导数（道/s）。"""
    ts, acc = [], 0.0
    for t in ticks:
        acc += t["dt"]
        ts.append(acc)
    exec_r = [t["exec_rec"] for t in ticks]
    deriv = [0.0]
    for i in range(1, len(ts)):
        deriv.append((exec_r[i] - exec_r[i - 1]) / max(ts[i] - ts[i - 1], 1e-6))
    out, vlp = [], deriv[0]
    for i in range(len(ts)):
        a = 1.0 - math.exp(-((ts[i] - ts[i - 1]) if i else 0.0) / V_REF_TAU_S)
        vlp += a * (deriv[i] - vlp) if i else 0.0
        out.append(vlp)
    return out


def _segments(v_ref):
    """机动段 [i0,i1)（|v_ref|>阈 的连续区间）与巡航拍集合。"""
    segs, i = [], 0
    while i < len(v_ref):
        if abs(v_ref[i]) > MANEUVER_V:
            j = i
            while j < len(v_ref) and abs(v_ref[j]) > MANEUVER_V:
                j += 1
            if j - i >= MANEUVER_MIN_TICKS:
                segs.append((i, j))
            i = j
        else:
            i += 1
    cruise = [k for k in range(len(v_ref))
              if abs(v_ref[k]) <= MANEUVER_V
              and not any(a <= k < b for a, b in segs)]
    return segs, cruise


def _metrics(v, exec_l, new_idx, v_ref, ticks):
    """四指标（全 trace 汇总的面）。"""
    # 抖动：新证据拍注入前后的速度跳变（旧口径的振荡直接显形处）
    jumps = []
    for k in new_idx:
        if 0 < k < len(v):
            jumps.append(abs(v[k] - v[k - 1]))
    segs, cruise = _segments(v_ref)
    # 偏置：巡航段 mean(v − v_ref)
    bias = [v[k] - v_ref[k] for k in cruise]
    # 响应/恢复：逐机动段
    lags, corrs, recov = [], [], []
    for a, b in segs:
        w_v = v[max(0, a - 10):min(len(v), b + 10)]
        w_r = v_ref[max(0, a - 10):min(len(v), b + 10)]
        if len(w_v) < 10:
            continue
        mv, mr = sum(w_v) / len(w_v), sum(w_r) / len(w_r)
        num = sum((x - mv) * (y - mr) for x, y in zip(w_v, w_r))
        d1 = math.sqrt(sum((x - mv) ** 2 for x in w_v))
        d2 = math.sqrt(sum((y - mr) ** 2 for y in w_r))
        if d1 > 1e-9 and d2 > 1e-9:
            best, best_k = -2.0, 0
            for lag in range(-10, 11):     # ±0.5s @20Hz
                n = len(w_v)
                if lag >= 0:
                    xs, ys = w_v[lag:], w_r[:n - lag]
                else:
                    xs, ys = w_v[:n + lag], w_r[-lag:]
                if len(xs) < 5:
                    continue
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                c = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
                dy = math.sqrt(sum((y - my) ** 2 for y in ys))
                if dx > 1e-9 and dy > 1e-9:
                    cc = c / dx / dy
                    if cc > best:
                        best, best_k = cc, lag
            corrs.append(best)
            lags.append(best_k * (ticks[1]["dt"] if len(ticks) > 1 else 0.05))
        # 恢复：段结束后 |v−v_ref|<带 的首拍延迟
        for k in range(b, min(len(v), b + 40)):
            if abs(v[k] - v_ref[k]) < RECOVER_BAND:
                recov.append(sum(t["dt"] for t in ticks[b:k + 1]))
                break
    return {"jump_p50": _pct(jumps, .5), "jump_p95": _pct(jumps, .95),
            "bias_mean": sum(bias) / len(bias) if bias else float("nan"),
            "bias_p95": _pct([abs(x) for x in bias], .95),
            "corr_med": _pct(corrs, .5), "lag_med_s": _pct(lags, .5),
            "recov_p50_s": _pct(recov, .5), "n_seg": len(segs)}


def main():
    cfg = _load_cfg()
    paths = sorted(TRACE_ROOT.glob("trace_*.jsonl"))
    if not paths:
        print(f"NO TRACES under {TRACE_ROOT}")
        return 1
    print(f"cfg: beta={cfg['obs_beta']} alpha={cfg['obs_alpha']} "
          f"v_lat_max={cfg['v_lat_max']} a_lat_gain={cfg['a_lat_gain']}")
    print(f"traces: {len(paths)} files")

    all_ticks = []
    for p in paths:
        ts = _load_ticks(p)
        if len(ts) >= 50:
            all_ticks.append((p.name, ts))
    n_ticks = sum(len(ts) for _, ts in all_ticks)
    print(f"usable: {len(all_ticks)} traces, {n_ticks} ticks "
          f"(executed/v_lat 列齐备)")

    # ── 自验证：旧时间基×产线 β 应逐位复现 recorded v_lat_est ──
    dev_all = []
    for _, ts in all_ticks:
        v, _, _ = _replay(ts, cfg, "old", cfg["obs_beta"])
        dev_all += [abs(a - b["v_rec"]) for a, b in zip(v, ts)]
    print(f"\n[自验证] old×β{cfg['obs_beta']} vs recorded v_lat_est: "
          f"|Δ| P50={_pct(dev_all, .5):.4f} P95={_pct(dev_all, .95):.4f} "
          f"max={max(dev_all):.4f} 道/s")

    # ── 变体矩阵 ──
    print(f"\n{'variant':<16}{'jumpP50':>8}{'jumpP95':>8}{'biasP95':>8}"
          f"{'corr':>7}{'lag_s':>7}{'recov_s':>8}{'seg':>5}")
    rows = {}
    for tb in ("old", "accum"):
        for b in BETA_GRID:
            jps, j95, bp, cs, ls, rs = [], [], [], [], [], []
            nseg = 0
            for _, ts in all_ticks:
                v, ex, ni = _replay(ts, cfg, tb, b)
                vr = _v_ref(ts)
                m = _metrics(v, ex, ni, vr, ts)
                if not math.isnan(m["jump_p50"]):
                    jps.append(m["jump_p50"]); j95.append(m["jump_p95"])
                if not math.isnan(m["bias_p95"]):
                    bp.append(m["bias_p95"])
                if not math.isnan(m["corr_med"]):
                    cs.append(m["corr_med"]); ls.append(m["lag_med_s"])
                if not math.isnan(m["recov_p50_s"]):
                    rs.append(m["recov_p50_s"])
                nseg += m["n_seg"]
            # 跨 trace 取池化分位（样本面）
            pool = {"jump_p50": _pct(jps, .5), "jump_p95": _pct(j95, .5),
                    "bias_p95": _pct(bp, .5), "corr": _pct(cs, .5),
                    "lag": _pct(ls, .5), "recov": _pct(rs, .5)}
            rows[(tb, b)] = pool
            print(f"{tb}×β{b:<6.1f}{pool['jump_p50']:>8.3f}{pool['jump_p95']:>8.3f}"
                  f"{pool['bias_p95']:>8.3f}{pool['corr']:>7.3f}{pool['lag']:>7.2f}"
                  f"{pool['recov']:>8.2f}{nseg:>5d}")

    # ── 读数指引 ──
    print("\n读数指引：jump=新证据拍|Δv|（振荡直接显形处，越小越稳）；"
          "corr/lag=机动段 v 对粗真值的跟随（越接近 1 / 越小越跟手）；"
          "bias P95=巡航段 |v−v_ref|（过大=跟不上或常偏）；"
          "recov=机动段结束回带时间。β 选择=corr/lag 不劣化前提下 jump 最小。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
