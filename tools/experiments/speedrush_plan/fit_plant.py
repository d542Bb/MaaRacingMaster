# -*- coding: utf-8 -*-
"""横向 plant 标定：从控制 trace 拟合 (a_lat_gain, tau_align_s, v_lat_max, tau_steer_s)。

口径（motion_plan 设计稿 A0）：
- 数据=生产 trace 的逐拍 `steer_norm`（低通后杆值）、`executed_lane`、
  `v_lat_est`、`road_offset`、`dt`。
- 似然=**一步转移 + 生产同款 α-β 观测模型**：用上一拍状态与下一拍杆值积分，
  有路心观测的拍按 `obs_alpha/obs_beta` 修正后再与记录的下一拍状态比较。
  修正拍不剔除——它们正是“模型预测 vs 观测”的证据。
- 变道事件：杆值同号连续 ≥5 拍且峰值 ≥0.15 的段，样本 <8 判不可辨识。
- 留出验证：≥2 段 trace 按整段留一；只有 1 段时按时间 70/30 切分。
- 参数 CI：最小二乘 Jacobian 近似协方差（数据相关，仅作退化检查）。
- `sigma_ego(t)`：用拟合参数做多步开环预测，按 horizon 分组的残差 RMS 线性
  拟合（道→米用 lane_w）；这是保守口径（含路心观测噪声）。
- 旧 trace 没有 `steer_raw` 列：`tau_steer` 不可辨识 → `calibrated=false`；
  新 trace 带 `steer_raw` 后可四参数联合拟合。
- 任一参数贴界、CI 相对宽度 >0.5、留出误差超限、事件 <8、或缺 steer_raw →
  `calibrated=false`；`motion.mode=live` 加载时按此 fail-loud。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_plan/fit_plant.py \
        --trace <trace.jsonl> [--trace ...] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

HORIZONS = (5, 10, 20, 40)      # σ_ego 多步评估的步数（≈0.25/0.5/1/2s @20Hz）
PARAM_NAMES = ("a_lat_gain", "tau_align_s", "v_lat_max", "tau_steer_s")
BOUNDS = ((1.0, 15.0), (0.1, 2.0), (0.5, 5.0), (0.02, 0.5))
MIN_DT, MAX_DT = 0.01, 0.2
V_WEIGHT = 0.5                  # 速度残差权重（道/s → 道 量纲折中）


def load_series(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows.sort(key=lambda r: r["fid"])
    out = []
    for r in rows:
        if r.get("state") != "CRUISE":
            continue
        if not isinstance(r.get("dt"), (int, float)):
            continue
        if not MIN_DT <= r["dt"] <= MAX_DT:
            continue
        vals = (r.get("executed_lane"), r.get("v_lat_est"),
                r.get("steer_norm"))
        if any(v is None or not math.isfinite(float(v)) for v in vals):
            continue
        r["_session"] = path.stem
        out.append(r)
    return out


def _fresh(r: dict) -> bool:
    return bool(r.get("dgeo_new")) and r.get("ro_source") not in (
        "pair_slot", "wall_occl", "none", None)


def _finite(v) -> bool:
    return v is not None and math.isfinite(float(v))


def _params(vec, use_raw, fixed_tau):
    p = {k: float(v) for k, v in zip(PARAM_NAMES, vec)}
    p["tau_steer_s"] = p.get("tau_steer_s", fixed_tau) if use_raw else fixed_tau
    return p


def _next_state(x, v, steer0, u, dt, p, use_raw):
    if use_raw:
        alpha = 1.0 - math.exp(-dt / p["tau_steer_s"])
        steer = steer0 + alpha * (u - steer0)
    else:
        steer = u                     # 无原始杆值：记录的低通值就是 plant 输入
    v = v + (p["a_lat_gain"] * steer - v / p["tau_align_s"]) * dt
    v = max(-p["v_lat_max"], min(p["v_lat_max"], v))
    x = x + v * dt
    return x, v


def transition_residuals(pairs, vec, use_raw, fixed_tau, cfg, with_ro=True):
    """逐对残差：预测下一拍状态（含 α-β 修正）减记录值。"""
    p = _params(vec, use_raw, fixed_tau)
    out = []
    for a, b in pairs:
        ro = b.get("road_offset")
        u = b.get("steer_raw") if use_raw else b.get("steer_norm")
        if u is None or not _finite(u):
            continue
        xp, vp = _next_state(float(a["executed_lane"]), float(a["v_lat_est"]),
                             float(a["steer_norm"]), float(u), float(b["dt"]), p,
                             use_raw)
        if with_ro and _finite(ro):
            r = float(ro) - xp
            xp += cfg.planner.obs_alpha * r
            if not cfg.planner.obs_position_only and _fresh(b):
                vp += cfg.planner.obs_beta * r / float(b["dt"])
        out.append(xp - float(b["executed_lane"]))
        out.append(V_WEIGHT * (vp - float(b["v_lat_est"])))
    return np.asarray(out, dtype=float)


def fit(pairs, use_raw, init, fixed_tau, cfg):
    if use_raw:
        x0 = np.array([init[k] for k in PARAM_NAMES])
        lo = np.array([b[0] for b in BOUNDS])
        hi = np.array([b[1] for b in BOUNDS])
    else:
        x0 = np.array([init[k] for k in PARAM_NAMES[:3]])
        lo = np.array([b[0] for b in BOUNDS[:3]])
        hi = np.array([b[1] for b in BOUNDS[:3]])
    res = least_squares(
        lambda z: transition_residuals(pairs, z, use_raw, fixed_tau, cfg),
        np.clip(x0, lo + 1e-9, hi - 1e-9),
        bounds=(lo, hi), loss="soft_l1", f_scale=0.05, max_nfev=4000)
    return res


def param_report(res, use_raw, fixed_tau):
    n = res.x.size
    p = {k: float(v) for k, v in zip(PARAM_NAMES[:n], res.x)}
    if not use_raw:
        p["tau_steer_s"] = fixed_tau
    dof = max(1, res.fun.size - n)
    s2 = 2.0 * res.cost / dof
    cov = np.linalg.pinv(res.jac.T @ res.jac) * s2
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    ci, at_bound = {}, {}
    for k, v, s in zip(PARAM_NAMES[:n], res.x, se):
        ci[k] = [float(v - 1.96 * s), float(v + 1.96 * s)]
        b = BOUNDS[PARAM_NAMES.index(k)]
        span = b[1] - b[0]
        at_bound[k] = bool(v - b[0] < 0.05 * span or b[1] - v < 0.05 * span)
    if not use_raw:
        ci["tau_steer_s"] = None
        at_bound["tau_steer_s"] = False
    return p, ci, at_bound


def rmse(pairs, p, use_raw, cfg):
    vec = np.asarray([p[k] for k in PARAM_NAMES[:4 if use_raw else 3]])
    r = transition_residuals(pairs, vec, use_raw, p["tau_steer_s"], cfg)
    if not r.size:
        return float("nan")
    xr = r[0::2]
    return float(np.sqrt(np.mean(xr * xr)))


def sigma_ego(series, p, use_raw):
    """多步开环残差：所有 horizon 用同一批起点（起点与每个终点都必须新鲜）。"""
    H = max(HORIZONS)
    xs, ys = [], []
    for h in HORIZONS:
        rr, ts = [], []
        for rows in series:
            for i in range(0, len(rows) - H, 3):
                r0 = rows[i]
                if not _fresh(r0) or not _finite(r0.get("road_offset")):
                    continue
                if not all(_fresh(rows[i + k])
                           and _finite(rows[i + k].get("road_offset"))
                           for k in HORIZONS):
                    continue
                x, v = float(r0["road_offset"]), float(r0["v_lat_est"])
                t, ok = 0.0, True
                for k in range(1, h + 1):
                    r = rows[i + k]
                    u = r.get("steer_raw") if use_raw else r["steer_norm"]
                    if u is None:
                        ok = False
                        break
                    t += float(r["dt"])
                    x, v = _next_state(
                        x, v, float(rows[i + k - 1]["steer_norm"]),
                        float(u), float(r["dt"]), p, use_raw)
                if not ok:
                    continue
                rr.append(x - float(rows[i + h]["road_offset"]))
                ts.append(t)
        if len(rr) >= 10:
            xs.append(float(np.mean(ts)))
            ys.append(float(np.sqrt(np.mean(np.square(rr)))))
    if len(xs) < 2:
        return {"sigma_ego0_m": None, "sigma_ego_rate_mps": None,
                "sigma_ego_samples": 0}
    rate, base = np.polyfit(xs, ys, 1)
    if rate < 0.0:
        rate, base = 0.0, max(ys)
    return {"sigma_ego0_m": float(max(base, 0.0)),
            "sigma_ego_rate_mps": float(rate),
            "sigma_ego_samples": len(xs),
            "residual_rms_by_horizon": [
                {"t_s": round(t, 3), "sigma_lane": round(s, 4)}
                for t, s in zip(xs, ys)]}


def count_events(rows: list[dict]) -> int:
    n, run, peak = 0, 0, 0.0
    for r in rows:
        s = float(r["steer_norm"])
        if abs(s) >= 0.15 and (run == 0 or s * peak > 0):
            run += 1
            peak = s
        else:
            if run >= 5:
                n += 1
            run, peak = (1, s) if abs(s) >= 0.15 else (0, 0.0)
    if run >= 5:
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", default=[],
                    help="trace_*.jsonl（可重复）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    if not args.trace:
        ap.error("至少给一个 --trace")
    cfg = load_decision()
    cal = load_calib()
    init = {k: getattr(cfg.planner, k) for k in PARAM_NAMES}

    series = [load_series(Path(p)) for p in args.trace]
    series = [s for s in series if s]
    pairs, by_session = [], {}
    for rows in series:
        sp = [(rows[i], rows[i + 1]) for i in range(len(rows) - 1)]
        by_session[rows[0]["_session"]] = sp
        pairs.extend(sp)
    use_raw = bool(pairs) and all(
        b.get("steer_raw") is not None for _, b in pairs)
    events = sum(count_events(rows) for rows in series)
    report = {
        "schema": 1, "sessions": len(series), "pairs": len(pairs),
        "events": events, "input": "steer_raw" if use_raw else "steer_norm(fallback)",
        "initial": init, "calibrated": False, "reasons": [],
    }
    if len(series) < 2 or len(pairs) < 200:
        report["reasons"].append("样本不足：需 ≥2 段 trace 且 ≥200 个转移对")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    res = fit(pairs, use_raw, init, init["tau_steer_s"], cfg)
    p, ci, at_bound = param_report(res, use_raw, init["tau_steer_s"])
    train = rmse(pairs, p, use_raw, cfg)
    test_scores = []
    for sid, held in by_session.items():
        train_p = [pr for s, sp in by_session.items() if s != sid for pr in sp]
        if train_p and held:
            r = fit(train_p, use_raw, init, init["tau_steer_s"], cfg)
            pp, _, _ = param_report(r, use_raw, init["tau_steer_s"])
            test_scores.append(rmse(held, pp, use_raw, cfg))
    test = float(np.mean(test_scores)) if test_scores else train

    report.update({
        "params": p, "ci95": ci, "at_bound": at_bound,
        "train_rmse_lane": train, "test_rmse_lane": test,
    })
    sg = sigma_ego(series, p, use_raw)
    sg["sigma_ego0_m"] = None if sg["sigma_ego0_m"] is None \
        else sg["sigma_ego0_m"] * cal.lane_w_m
    sg["sigma_ego_rate_mps"] = None if sg["sigma_ego_rate_mps"] is None \
        else sg["sigma_ego_rate_mps"] * cal.lane_w_m
    report["sigma_ego"] = sg

    if not use_raw:
        report["reasons"].append("trace 缺 steer_raw：tau_steer 不可辨识")
    if any(at_bound.values()):
        report["reasons"].append("参数贴界：" + ",".join(
            k for k, v in at_bound.items() if v))
    for k, c in ci.items():
        if c is None:
            continue
        width = c[1] - c[0]
        if p[k] and width / abs(p[k]) > 0.5:
            report["reasons"].append(f"{k} CI 相对宽度 {width / abs(p[k]):.2f} > 0.5")
    if events < 8:
        report["reasons"].append(f"有效变道事件 {events} < 8")
    if test > max(0.15, 1.5 * train):
        report["reasons"].append(
            f"留出 RMSE {test:.3f} 道超限（train {train:.3f}）")
    report["calibrated"] = not report["reasons"]
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
