#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探针：人工局的「车头角 × 车速」能否替代绝对速度，以及人工变道的车头角模板。

三个问题（全部只读录制数据，不进入生产链）：

1. 由标线测得的**相对车道的车头角** psi（线角减 2s 慢基线）与横向位移，能否
   反解一个稳定的前向速度 v？做法：1s 窗内位移 = (v/lane_w)·Σtan(psi)·dt，
   半样本拟合 v、另半样本留出验证；与常数 45 m/s、与线上计划自带的 1s 预测
   比 1s 横向位移误差。
2. 人工变道时车头角多大、持续多久、峰值杆值多少（「远见 + 小变向」的量化模板）。
3. 程序并线段的同一组量是多少（对照）。

数据：logs/<run>/trace/speedrush.jsonl + debug/speedrush/<run>_manual_p*/pads.jsonl。
本脚本自包含（不 import maaracing_master），路径由 --root 或 APPDATA 推导。

用法：
    python probe_manual_heading.py
    python probe_manual_heading.py --root D:/data/MaaRacingMaster
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
from pathlib import Path

LANE_W = 3.5            # 米制道宽（与 gate0 标定同值；本探针只做量级换算）
BASELINE_TAU_S = 2.0    # 线角慢基线（与产线 HeadingObserver 同口径）
SLOPE_MIN_TAN = 0.002   # 进入速度拟合的最小 |Σtan·dt|（条件数门）
SPEED_FALLBACK = 45.0   # 旧实现的兜底车速（对照用）


def rows_of(trace: Path) -> list[dict]:
    out = []
    with open(trace, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "fid" in r and "route" in r and "lane_obs" in r:
                out.append(r)
    return out


def pads_of(path: Path) -> list[tuple[int, int]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "ts_ns" in r and "lx" in r:
                out.append((int(r["ts_ns"]), int(r["lx"])))
    return out


def theta_of(r: dict) -> float | None:
    th = (r.get("lane_obs") or {}).get("theta")
    return None if th is None else float(th)


def lines_of(r: dict) -> dict[int, float]:
    lo = r.get("lane_obs") or {}
    return {int(c["k"]): float(c["L"]) for c in (lo.get("lines") or [])
            if c.get("accepted") and c.get("k") is not None}


def lane_w_of(r: dict) -> float:
    s = r.get("scene") or {}
    return float(s.get("lane_w") or LANE_W)


def times_of(rows: list[dict]) -> list[float]:
    t = [0.0]
    for r in rows:
        t.append(t[-1] + float(r.get("dt") or 0.05))
    return t


def psi_series(rows: list[dict]) -> list[float | None]:
    """相对车道的车头角：线角减 2s 慢基线；同 fid 重复读数不重复推进基线。"""
    base = None
    last_fid = None
    out: list[float | None] = []
    for r in rows:
        th = theta_of(r)
        if th is None:
            out.append(None)
            continue
        fid = (r.get("lane_obs") or {}).get("fid")
        dt = float(r.get("dt") or 0.05)
        if fid is None or fid != last_fid:
            last_fid = fid
            if base is None:
                base = th
            else:
                a = 1.0 - math.exp(-dt / BASELINE_TAU_S)
                base += a * (th - base)
        out.append(th - base if base is not None else 0.0)
    return out


def plan_pred_1s(r: dict) -> float | None:
    """线上计划自己给出的 1s 横向位移预测（道单位，相对起点）。"""
    pl = r.get("plan") or {}
    if not pl.get("valid") or not pl.get("cand"):
        return None
    ch = pl.get("chosen")
    if ch is None or ch < 0 or ch >= len(pl["cand"]):
        return None
    xs = pl["cand"][ch]
    if len(xs) < 21:
        return None
    return (xs[20] - xs[0]) / lane_w_of(r)


def stable_span(rows: list[dict], i: int, j: int) -> bool:
    """参考系在窗内没有跳变（frame_shift / 帧偏置大跳）。"""
    for q in rows[i:j + 1]:
        why = str((q.get("plan") or {}).get("reason") or "")
        if why.startswith("plan:frame_shift"):
            return False
    fd0 = (rows[i].get("plan") or {}).get("fd")
    fd1 = (rows[j].get("plan") or {}).get("fd")
    if fd0 is None or fd1 is None or abs(fd1 - fd0) > 0.3:
        return False
    return True


def phys_disp(rows: list[dict], i: int, j: int) -> float | None:
    """同一 k 的标线在窗两端的 ΔL 反推物理横移（道单位，右正）。"""
    a, b = lines_of(rows[i]), lines_of(rows[j])
    common = [k for k in a if k in b]
    if not common:
        return None
    dxs = [-(b[k] - a[k]) for k in common]
    if len(dxs) >= 2 and max(dxs) - min(dxs) > 0.4:
        return None
    return statistics.median(dxs)


def collect_windows(rows: list[dict]) -> list[dict]:
    """1s 窗：psi 积分、参考系位移、标线位移、线上模型预测。"""
    times = times_of(rows)
    psi = psi_series(rows)
    wins = []
    n = len(rows)
    for i in range(n):
        j = i
        while j + 1 < n and times[j + 1] < times[i] + 1.0:
            j += 1
        if times[j] < times[i] + 0.85 or not stable_span(rows, i, j):
            continue
        lst = [(psi[k], float(rows[k].get("dt") or 0.05))
               for k in range(i, j)]
        if any(p is None for p, _ in lst):
            continue
        wins.append({
            "s": sum(math.tan(p) * dt for p, dt in lst),
            "act_frame": float(rows[j]["executed_lane"]) - float(rows[i]["executed_lane"]),
            "act_phys": phys_disp(rows, i, j),
            "model_pred": plan_pred_1s(rows[i]),
        })
    return wins


def fit_speed(wins: list[dict], key: str) -> float | None:
    """位移 = (v/lane_w)·s 的最小二乘解（只认条件数足够的窗）。"""
    num = den = 0.0
    for w in wins:
        act = w.get(key)
        if act is None or abs(w["s"]) < SLOPE_MIN_TAN:
            continue
        num += w["s"] * act
        den += w["s"] ** 2
    return (num / den) * LANE_W if den > 1e-9 else None


def eval_speed(wins: list[dict], key: str, v_mps: float | None):
    if v_mps is None:
        return None
    vl = v_mps / LANE_W
    errs = [abs(vl * w["s"] - w[key]) for w in wins
            if w.get(key) is not None and abs(w["s"]) >= SLOPE_MIN_TAN]
    if len(errs) < 20:
        return None
    errs.sort()
    return statistics.median(errs) * LANE_W, errs[int(len(errs) * 0.9)] * LANE_W, len(errs)


def eval_model(wins: list[dict], key: str):
    errs = [abs(w["model_pred"] - w[key]) for w in wins
            if w.get(key) is not None and w.get("model_pred") is not None]
    if len(errs) < 20:
        return None
    errs.sort()
    return statistics.median(errs) * LANE_W, errs[int(len(errs) * 0.9)] * LANE_W, len(errs)


def summarize(name: str, wins: list[dict]) -> None:
    if not wins:
        print("  %s: 无可用窗口" % name)
        return
    half = len(wins) // 2
    print("  %s: 1s 窗 %d" % (name, len(wins)))
    for key, kn in (("act_frame", "参考系"), ("act_phys", "标线物理")):
        v_fit = fit_speed(wins[:half], key)
        r = eval_speed(wins[half:], key, v_fit)
        r45 = eval_speed(wins[half:], key, SPEED_FALLBACK)
        rm = eval_model(wins[half:], key)
        if r is None:
            print("    真值=%s: 窗口不足" % kn)
            continue
        v_txt = "—" if v_fit is None else "%.1f" % v_fit
        m45 = "—" if r45 is None else "%.2f" % r45[0]
        mm = "—" if rm is None else "%.2f" % rm[0]
        print("    真值=%s 拟合 v=%s m/s | 留出 中位=%.2fm p90=%.2fm | 常数45 中位=%sm"
              " | 线上模型 中位=%sm (n=%d)"
              % (kn, v_txt, r[0], r[1], m45, mm, r[2]))
    print("    |Σtan·dt| 分桶（逐窗比值 v，道/s per tan）:")
    for lo, hi in ((0.002, 0.005), (0.005, 0.01), (0.01, 0.02),
                   (0.02, 0.05), (0.05, 0.2)):
        vals = [w["act_frame"] / w["s"] for w in wins
                if lo <= abs(w["s"]) <= hi and abs(w["act_frame"]) > 0.05]
        if len(vals) > 20:
            vals.sort()
            print("      %.3f~%.3f: n=%d 中位=%.1f p25=%.1f p75=%.1f"
                  % (lo, hi, len(vals), statistics.median(vals),
                     vals[len(vals) // 4], vals[int(len(vals) * 0.75)]))


def stick_series(rows: list[dict], pads: list[tuple[int, int]]) -> list[float | None]:
    ts = [p[0] for p in pads]
    out = []
    for r in rows:
        t = r.get("ts_ns")
        if t is None:
            out.append(None)
            continue
        k = bisect.bisect_left(ts, t)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(pads):
                d = abs(pads[j][0] - t)
                if best is None or d < best[0]:
                    best = (d, pads[j][1])
        out.append(None if best is None or best[0] > 20_000_000
                   else best[1] / 32767.0)
    return out


def human_episodes(rows: list[dict], pads: list[tuple[int, int]]) -> list[dict]:
    """手柄连续同向推杆 ≥0.3s 记一次人工变道，量车头角/时长/横移。"""
    times = times_of(rows)
    psi = psi_series(rows)
    u = stick_series(rows, pads)
    episodes = []
    n = len(rows)
    i = 0
    while i < n:
        if u[i] is None or abs(u[i]) < 0.15:
            i += 1
            continue
        sign = 1 if u[i] > 0 else -1
        j = i
        while j + 1 < n and u[j + 1] is not None and abs(u[j + 1]) >= 0.10 \
                and (u[j + 1] > 0) == (sign > 0):
            j += 1
        dur = times[j] - times[i]
        if dur >= 0.3:
            peaks = [abs(p) for p in psi[i:j + 1] if p is not None]
            sticks = [abs(x) for x in u[i:j + 1] if x is not None]
            pk = max(peaks) if peaks else None
            hold = None
            if pk:
                k = i
                while k <= j and abs(psi[k] or 0.0) < 0.5 * pk:
                    k += 1
                m = k
                while m + 1 < n and abs(psi[m + 1] or 0.0) >= 0.5 * pk:
                    m += 1
                if k <= j:
                    hold = times[m] - times[k]
            episodes.append({
                "fid": rows[i].get("fid"), "sign": sign, "dur_s": dur,
                "peak_deg": None if pk is None else math.degrees(pk),
                "hold_s": hold,
                "peak_stick": max(sticks) if sticks else None,
                "disp_lane": phys_disp(rows, i, j),
            })
        i = j + 1
    return episodes


def robot_merges(rows: list[dict]) -> list[dict]:
    """程序并线段的车头角模板（与人工同口径）。"""
    times = times_of(rows)
    psi = psi_series(rows)
    out = []
    n = len(rows)
    i = 0
    while i < n:
        if (rows[i].get("route") or {}).get("ph") != "merging":
            i += 1
            continue
        j = i
        while j + 1 < n and (rows[j + 1].get("route") or {}).get("ph") == "merging":
            j += 1
        peaks = [abs(p) for p in psi[i:j + 1] if p is not None]
        sticks = [abs(float(r.get("steer_norm") or 0.0)) for r in rows[i:j + 1]]
        out.append({
            "fid": rows[i].get("fid"), "dur_s": times[j] - times[i],
            "peak_deg": None if not peaks else math.degrees(max(peaks)),
            "peak_stick": max(sticks) if sticks else None,
            "disp_lane": phys_disp(rows, i, j),
        })
        i = j + 1
    return out


def stat_txt(vals, fmt="%.2f") -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "—"
    vals.sort()
    return (fmt + " / p90 " + fmt) % (statistics.median(vals),
                                      vals[int(len(vals) * 0.9)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    args = ap.parse_args()
    root = Path(args.root)

    # p1/p2 共用同一份 trace：合并两段手柄，按局只算一次
    picked: dict[str, dict] = {}
    for d in sorted((root / "debug" / "speedrush").glob("*_manual_p*")):
        run = d.name.split("_manual")[0]
        trace = root / "logs" / run / "trace" / "speedrush.jsonl"
        pads = d / "pads.jsonl"
        if not (trace.is_file() and pads.is_file()):
            continue
        item = picked.setdefault(run, {"names": [], "trace": trace,
                                       "pads": []})
        item["names"].append(d.name)
        item["pads"].append(pads)
    sessions = [(run, "+".join(it["names"]), it["trace"], it["pads"])
                for run, it in sorted(picked.items())]
    if not sessions:
        print("没有找到人工局（需要 logs/<run>/trace + debug/*_manual_p*/pads.jsonl）")
        return

    robots = []
    for d in sorted((root / "debug" / "speedrush").iterdir()):
        if "_manual" in d.name or not d.is_dir():
            continue
        run = d.name.split("_p")[0]
        trace = root / "logs" / run / "trace" / "speedrush.jsonl"
        if d.name.endswith("_p1") and trace.is_file():
            robots.append((run, trace))
    robots = robots[-2:]

    print("=" * 78)
    print("人工局：车头角 × 车速 的速度估计（前半拟合 / 后半留出）")
    all_human = []
    for run, dname, trace, pads in sessions:
        rows = rows_of(trace)
        wins = collect_windows(rows)
        print("\n[%s / %s] ticks=%d" % (run, dname, len(rows)))
        summarize(run, wins)
        all_pads: list[tuple[int, int]] = []
        for q in pads:
            all_pads.extend(pads_of(q))
        all_pads.sort()
        eps = human_episodes(rows, all_pads)
        all_human.extend([dict(e, run=run) for e in eps])
        print("    人工变道 %d 段：峰值车头角 %s° | 推杆时长 %s s | 峰值杆值 %s"
              " | 横移 %s 道"
              % (len(eps),
                 stat_txt([e["peak_deg"] for e in eps]),
                 stat_txt([e["dur_s"] for e in eps]),
                 stat_txt([e["peak_stick"] for e in eps]),
                 stat_txt([e["disp_lane"] for e in eps])))

    print("\n" + "=" * 78)
    print("程序局：并线段的车头角模板（对照）")
    for run, trace in robots:
        rows = rows_of(trace)
        mg = robot_merges(rows)
        if not mg:
            print("\n[%s] 无并线段" % run)
            continue
        print("\n[%s] 并线 %d 段：峰值车头角 %s° | 时长 %s s | 峰值杆值 %s"
              " | 横移 %s 道"
              % (run, len(mg),
                 stat_txt([m["peak_deg"] for m in mg]),
                 stat_txt([m["dur_s"] for m in mg]),
                 stat_txt([m["peak_stick"] for m in mg]),
                 stat_txt([m["disp_lane"] for m in mg])))

    print("\n" + "=" * 78)
    print("人工变道明细（前 20 段）")
    for e in all_human[:20]:
        hold = "—" if e["hold_s"] is None else "%.2f" % e["hold_s"]
        disp = "—" if e["disp_lane"] is None else "%.2f" % e["disp_lane"]
        print("  %s fid=%s sign=%+d 峰值=%.2f° 推杆=%.2fs 保持=%ss 杆=%.2f 横移=%s道"
              % (e["run"], e["fid"], e["sign"], e["peak_deg"] or 0.0,
                 e["dur_s"], hold, e["peak_stick"] or 0.0, disp))


if __name__ == "__main__":
    main()
