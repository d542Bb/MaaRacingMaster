# -*- coding: utf-8 -*-
"""横向状态观测离线评估（工作台，不入库）。

只读会话流 trace，不跑车。回答三件事：
  1. 控制环现在用的 ``v_lat_est`` 与线观测推出的横速差多少（相关、比值、MAE）；
  2. 车道线横速、线角（航向）各自能否提前预测 0.25s 后的横位；
  3. 线角能否反推等效前向速度（``v = v_lat*LANE_W/tan(theta_rel)``），
     用来替换固定 45 m/s 的米秒代理。

口径：
- 线横位 ``lane_obs.x`` 只取 ``state=TRACK`` 且非预测拍；重锁/单拍跳变后
  重新起算。真值横速用线横位 3 点中心差分（±0.1s），不拿模型当真值。
- 评估只在“当前线观测与未来 0.25s 线观测都有效”的拍上做，比较的是
  同一线帧内的横位增量，避免深度锚偏置混进来。
- ``v_heading = speed_ref * tan(theta_rel) / LANE_W``；speed_ref 由本局
  ``v_line*LANE_W/tan(theta_rel)`` 的稳健中位给出，并同时报告它的分布。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_lateral_state.py \
        --session 20261025_221703_1
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

LANE_W = 3.38
TAU_S = 0.25
SMOOTH_HALF_S = 0.3
MAX_SAMPLE_GAP_S = 0.45
MAX_STEP_LANE = 0.8
MIN_SPEED_SAMPLE = 0.15
MIN_TAN = 0.01

DEFAULT_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(
        os.environ.get(
            "SPEEDRUSH_DEBUG",
            r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/speedrush"))),
    "logs")


def load_session(logs_dir: str, token: str):
    path = Path(logs_dir) / token / "trace" / "speedrush.jsonl"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    games, buf = [], None
    for line in path.open(encoding="utf-8"):
        d = json.loads(line)
        kind = d.get("type")
        if kind == "stage_begin":
            buf = []
        elif kind == "stage_end":
            if buf is not None:
                games.append((f"{token}:s{d.get('stage_id')}"
                              f"#{len(games) + 1}", buf))
            buf = None
        elif kind is None and buf is not None:
            buf.append(d)
    return games


def ticks_of(rows):
    out = []
    for r in rows:
        lo = r.get("lane_obs") or {}
        lf = r.get("lateral_frame") or {}
        fuse = r.get("lane_fuse") or {}
        out.append({
            "t": r["ts_ns"] / 1e9, "fid": r.get("fid"),
            "dt": r.get("dt") or 0.05,
            "x": lo.get("x"), "theta": lo.get("theta"),
            "state": lo.get("state"), "pred": bool(lo.get("pred")),
            "n_locked": lo.get("n_locked") or 0,
            "lo_age_ms": lo.get("det_age_ms"),
            "lf_x": lf.get("x"), "lf_v": lf.get("v"),
            "rel": lf.get("theta_rel"), "th_ok": bool(lf.get("theta_ok")),
            "v_theta": lf.get("v_theta"),
            "exec": r.get("executed_lane"), "v": r.get("v_lat_est"),
            "ro": r.get("road_offset"), "ro_new": r.get("dgeo_new"),
            "ro_hold": r.get("ro_hold"), "bias": fuse.get("bias"),
            "steer": r.get("steer_norm"), "xt": r.get("x_target"),
            "phase": (r.get("route") or {}).get("ph"),
        })
    return out


def line_samples(ticks):
    """线横位测量序列：只取锁线、非预测拍；重锁/跳变重新起算。"""
    out = []
    prev = None
    for k in ticks:
        if k["state"] != "TRACK" or k["pred"] or k["x"] is None:
            prev = None
            continue
        x = float(k["x"])
        if prev is not None:
            dt = k["t"] - prev["t"]
            if dt <= 0.0 or dt > MAX_SAMPLE_GAP_S \
                    or abs(x - prev["x"]) > MAX_STEP_LANE:
                prev = None
                continue
        prev = {"t": k["t"], "x": x}
        out.append({"t": k["t"], "x": x, "theta": k["theta"],
                    "rel": k["rel"], "th_ok": k["th_ok"]})
    return out


def smooth_and_derive(samples):
    """线横位用 ±0.3s 窗口线性拟合求斜率：中心差分会被 ±0.1 道的测量噪声
    放大成 0.7 道/s 的假速度；7 点拟合把噪声压到 ~0.1 道/s。"""
    n = len(samples)
    for s in samples:
        s["xs"] = s["x"]
        s["v"] = None
    for i, s in enumerate(samples):
        a = i
        while a > 0 and s["t"] - samples[a - 1]["t"] <= SMOOTH_HALF_S:
            a -= 1
        c = i
        while c < n - 1 and samples[c + 1]["t"] - s["t"] <= SMOOTH_HALF_S:
            c += 1
        if c - a < 4:
            continue
        ts = [samples[j]["t"] - s["t"] for j in range(a, c + 1)]
        xs = [samples[j]["x"] for j in range(a, c + 1)]
        mt = sum(ts) / len(ts)
        mx = sum(xs) / len(xs)
        vt = sum((t - mt) ** 2 for t in ts)
        if vt <= 1e-9:
            continue
        s["v"] = sum((t - mt) * (x - mx) for t, x in zip(ts, xs)) / vt
        s["xs"] = mx
    return samples


def noise_lane(samples):
    """二阶差分估线横位测量噪声：平滑运动下 d2≈0，噪声方差=6σ²。"""
    d2 = []
    for i in range(1, len(samples) - 1):
        a, b, c = samples[i - 1], samples[i], samples[i + 1]
        if b["t"] - a["t"] <= 0.25 and c["t"] - b["t"] <= 0.25:
            d2.append(a["x"] - 2.0 * b["x"] + c["x"])
    if len(d2) < 20:
        return None
    return statistics.median([abs(v) for v in d2]) * 1.4826 / math.sqrt(6.0)


def nearest(samples, t, tol):
    best, bd = None, tol
    for s in samples:
        d = abs(s["t"] - t)
        if d <= bd:
            best, bd = s, d
    return best


def add_causal(valid):
    """因果横位/横速：只用 [t-0.6s, t] 的样本拟合，供预测与命令对比。"""
    n = len(valid)
    for i, s in enumerate(valid):
        a = i
        while a > 0 and s["t"] - valid[a - 1]["t"] <= 0.6:
            a -= 1
        if i - a < 3 or s["t"] - valid[a]["t"] < 0.3:
            s["xc"], s["vc"] = None, None
            continue
        ts = [valid[j]["t"] - s["t"] for j in range(a, i + 1)]
        xs = [valid[j]["x"] for j in range(a, i + 1)]
        mt = sum(ts) / len(ts)
        mx = sum(xs) / len(xs)
        vt = sum((t - mt) ** 2 for t in ts)
        if vt <= 1e-9:
            s["xc"], s["vc"] = None, None
            continue
        v = sum((t - mt) * (x - mx) for t, x in zip(ts, xs)) / vt
        s["vc"] = v
        s["xc"] = mx + v * (0.0 - mt)
    return valid


def speed_ref(samples):
    """线角反推等效前向速度：v_line*LANE_W/tan(theta_rel) 的稳健中位。"""
    vals = []
    for s in samples:
        if s["v"] is None or not s["th_ok"] or s["rel"] is None:
            continue
        if abs(s["v"]) < MIN_SPEED_SAMPLE:
            continue
        tan = math.tan(float(s["rel"]))
        if abs(tan) < MIN_TAN:
            continue
        if (s["v"] > 0) != (tan > 0):
            continue
        vals.append(s["v"] * LANE_W / tan)
    if len(vals) < 8:
        return None, vals
    return statistics.median(vals), vals


def corr(a, b):
    if len(a) < 3:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 1e-12 or vb <= 1e-12:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def mae(errs):
    return statistics.median([abs(e) for e in errs]) if errs else None


def p90(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * 0.9))]


def evaluate_game(label, rows):
    ticks = ticks_of(rows)
    samples = smooth_and_derive(line_samples(ticks))
    valid = add_causal([s for s in samples if s["v"] is not None])
    sp_ref, sp_vals = speed_ref(samples)

    # ① 当前模型横速 vs 线横速（同一拍最近线样本 ±0.1s）
    pairs = []
    for k in ticks:
        if k["v"] is None:
            continue
        s = nearest(valid, k["t"], 0.12)
        if s is not None:
            pairs.append((float(k["v"]), s["v"]))
    mv = [p[0] for p in pairs]
    tv = [p[1] for p in pairs]
    ratios = [a / b for a, b in pairs if abs(b) >= 0.1]
    ratio_pos = [r for r in ratios if r > 0]

    # ② 前向预测：因果估计（截至 t）出发，0.25s 后与线横位真值比
    pred_err = {"model": [], "line": [], "heading": []}
    pred_sgn = {"model": [], "line": [], "heading": []}
    for s0 in valid:
        if s0["vc"] is None:
            continue
        s1 = nearest(valid, s0["t"] + TAU_S, 0.08)
        if s1 is None or abs(s1["t"] - s0["t"] - TAU_S) > 0.08:
            continue
        k = nearest(ticks, s0["t"], 0.03)
        if k is None or k["v"] is None:
            continue
        vs = {"model": float(k["v"]), "line": s0["vc"]}
        if sp_ref is not None and s0["th_ok"] and s0["rel"] is not None:
            vs["heading"] = sp_ref * math.tan(float(s0["rel"])) / LANE_W
        for name, v in vs.items():
            e = (s0["xc"] + v * TAU_S) - s1["xs"]
            pred_err[name].append(e)
            if abs(v) >= 0.1 and abs(s1["xs"] - s0["xc"]) >= 0.1:
                pred_sgn[name].append((v > 0) == (s1["xs"] > s0["xc"]))

    # ③ 控制命令：同一 PD 律，只换横速；方向不一致与幅值差
    cmd = {"n": 0, "diff_sign": 0, "abs_delta": [], "model_zero": 0}
    p, kp, kd = 0.7, 0.2, 0.7
    for k in ticks:
        if k["v"] is None or k["exec"] is None or k["xt"] is None:
            continue
        s = nearest(valid, k["t"], 0.12)
        if s is None or s["vc"] is None:
            continue
        e = k["xt"] - (k["exec"] + float(k["v"]) * TAU_S)
        u0 = kp * e - kd * float(k["v"])
        e1 = k["xt"] - (k["exec"] + s["vc"] * TAU_S)
        u1 = kp * e1 - kd * s["vc"]
        cmd["n"] += 1
        if abs(u0) > 0.05 and abs(u1) > 0.05 \
                and (u0 > 0) != (u1 > 0):
            cmd["diff_sign"] += 1
        cmd["abs_delta"].append(abs(u0 - u1))
        if abs(u0) <= 0.05 and abs(u1) > 0.05:
            cmd["model_zero"] += 1
    _ = p

    def rate(s):
        return (sum(s) / len(s)) if s else None

    return {
        "label": label,
        "ticks": len(ticks),
        "line_ticks": len(valid),
        "line_cov": len(valid) / max(1, len(ticks)),
        "noise": noise_lane(samples),
        "speed_ref": sp_ref,
        "speed_n": len(sp_vals),
        "speed_p10": (sorted(sp_vals)[int(len(sp_vals) * 0.1)]
                      if len(sp_vals) >= 8 else None),
        "speed_p90": (sorted(sp_vals)[int(len(sp_vals) * 0.9)]
                      if len(sp_vals) >= 8 else None),
        "v_corr": corr(mv, tv),
        "v_ratio_p50": statistics.median(ratios) if ratios else None,
        "v_ratio_pos": (sum(1 for r in ratio_pos if r > 0) / len(ratios)
                        if ratios else None),
        "v_mae": mae([a - b for a, b in pairs]),
        "pred": {n: (mae(v), p90([abs(x) for x in v]), len(v))
                 for n, v in pred_err.items()},
        "pred_sgn": {n: rate(v) for n, v in pred_sgn.items()},
        "cmd": {"n": cmd["n"],
                "diff_sign": cmd["diff_sign"],
                "abs_delta_p50": statistics.median(cmd["abs_delta"])
                if cmd["abs_delta"] else None,
                "model_zero": cmd["model_zero"]},
    }


def fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def pct(x):
    return "-" if x is None else f"{x:.0%}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    games = load_session(args.logs, args.session)
    print(f"# 横向状态观测离线评估：{args.session}（{len(games)} 局）\n")
    print("| 局 | ticks | 线样本 | 覆盖率 | 噪声(道) | speed_ref(m/s) | v相关 | "
          "比值p50 | v MAE | 预测MAE model/line/heading | 方向命中 model/line/heading |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    res = []
    for label, rows in games:
        r = evaluate_game(label, rows)
        res.append(r)
        pe = r["pred"]
        ps = r["pred_sgn"]
        sp = ("-" if r["speed_ref"] is None
              else f"{r['speed_ref']:.1f} ({r['speed_p10']:.0f}~"
                   f"{r['speed_p90']:.0f})")
        print(f"| {label} | {r['ticks']} | {r['line_ticks']} | "
              f"{pct(r['line_cov'])} | {fmt(r['noise'], 3)} | {sp} | "
              f"{fmt(r['v_corr'])} | "
              f"{fmt(r['v_ratio_p50'])} | {fmt(r['v_mae'])} | "
              f"{fmt(pe['model'][0])}/{fmt(pe['line'][0])}/"
              f"{fmt(pe['heading'][0])} | "
              f"{pct(ps['model'])}/{pct(ps['line'])}/{pct(ps['heading'])} |")
    n = sum(r["cmd"]["n"] for r in res)
    ds = sum(r["cmd"]["diff_sign"] for r in res)
    mz = sum(r["cmd"]["model_zero"] for r in res)
    deltas = [r["cmd"]["abs_delta_p50"] for r in res
              if r["cmd"]["abs_delta_p50"] is not None]
    print(f"\n- 命令方向不一致：{ds}/{n}（{pct(ds / n if n else None)}）")
    print(f"- 模型杆值≈0 而线速要求动作：{mz}/{n}（{pct(mz / n if n else None)}）")
    print(f"- 命令幅值差 p50：{fmt(statistics.median(deltas) if deltas else None)}")
    print("\n## 读数")
    print("- `v相关` = 模型横速与线横速的相关系数；`比值p50` = model/line 中位。")
    print("- `预测MAE` = 因果估计（截至当前拍）预测 0.25s 后横位的误差（车道）。")
    print("- `方向命中` = 因果横速符号与未来 0.25s 实际位移方向一致的比例。")
    print("- `speed_ref` = 线角反推的等效前向速度中位（括号内 10~90%）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
