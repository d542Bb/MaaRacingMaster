"""场跟随修复的离线验收探针（2026-10-18 方案阶段 0）。

**要回答的问题**：实机 trace 里，场目标是否稳定、执行是否跟得上、失败拍
是否被错误拉动、横向速度状态是否被路心噪声主导。对应方案「验收 · 离线」
表的全部指标，改动前后各跑一次即可对照。

**口径**：
- 场目标（路心系道）= ``cr_best / lane_w``（``cr_best`` 是路心系米；旧
  trace 没有 ``goal`` 列时同式还原）。
- 执行分支（旧 trace 无 ``exec_branch`` 列时按 reason 前缀还原）：
  follow / hold / pause。
- 「失败拍」= ``traj_diag.ok is False``（轨迹采样全灭）。
- 目标稳定 run：相邻场目标差 ≤ 0.2 道算同一段（±0.1 带），统计段长。
- 方向翻转：相邻场目标变化 > 0.05 道且符号翻面，按秒归一。
- 保留率：场要求 |goal−exec| > 0.5 道时，输出同向且 |x_target−exec| > 0.3 的占比。
- 走廊缺席跳变：``exec_branch`` 为 hold/pause 拍的 |Δx_target| 最大值与 P90。
- v_lat 跳变：|Δv_lat_est| > 0.135 道/s 的拍占比（45% 杆量上限的拍级代理）。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_trace_accept.py
    ... [--root <debug/speedrush>] [--session 20261018_182922_p2 ...] [--json]

输出：逐会话指标 + 四局汇总（stdout）；--json 额外落
``<root>/probe_trace_accept.json``（调试图目录，不入库）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

_DEFAULT_ROOT = (Path(os.environ.get("APPDATA", "."))
                 / "MaaRacingMaster" / "debug" / "speedrush")


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


def _f(x):
    return None if x is None else round(float(x), 3)


def _branch(r: dict) -> str:
    b = r.get("exec_branch")
    if b:
        return b
    reason = r.get("reason") or ""
    if reason.startswith("pause"):
        return "pause"
    if reason.startswith("hold"):
        return "hold"
    return "follow"


def _goal_lane(r: dict, lane_w: float):
    g = r.get("goal")
    return None if g is None else float(g)


def analyze(rows: list[dict], lane_w: float) -> dict:
    goals = [_goal_lane(r, lane_w) for r in rows]
    xs = [r.get("x_target") for r in rows]
    execs = [r.get("executed_lane") for r in rows]
    branches = [_branch(r) for r in rows]

    # ---- 场目标稳定 run（±0.1 带）----
    runs, cur = [], 0
    prev = None
    for g in goals:
        if g is None:
            if cur:
                runs.append(cur)
            cur, prev = 0, None
            continue
        if prev is not None and abs(g - prev) <= 0.2:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur = 1
        prev = g
    if cur:
        runs.append(cur)

    # ---- 方向翻转（相邻变化 >0.05 道且反号）----
    flips, last_sign, prev = 0, None, None
    n_delta = 0
    for g in goals:
        if g is None:
            prev, last_sign = None, None
            continue
        if prev is not None:
            d = g - prev
            if abs(d) > 0.05:
                n_delta += 1
                if last_sign is not None and (d > 0) != last_sign:
                    flips += 1
                last_sign = d > 0
        prev = g
    t_span = sum((r.get("dt") or 0.05) for r in rows)
    flips_per_s = flips / t_span if t_span > 0 else None

    # ---- 失败拍错误拉动 ----
    fail_big = fail_rev = n_fail = 0
    for r, g, x, e in zip(rows, goals, xs, execs):
        diag = r.get("traj_diag") or {}
        if diag.get("ok") is not False or x is None or e is None:
            continue
        n_fail += 1
        if abs(x - e) > 0.3:
            fail_big += 1
        if g is not None and abs(g - e) > 0.1 and abs(x - e) > 0.1 \
                and ((x - e) > 0) != ((g - e) > 0):
            fail_rev += 1

    # ---- 目标保留率 ----
    keep = n_ask = 0
    for g, x, e in zip(goals, xs, execs):
        if g is None or x is None or e is None or abs(g - e) <= 0.5:
            continue
        n_ask += 1
        if abs(x - e) > 0.3 and ((x - e) > 0) == ((g - e) > 0):
            keep += 1

    # ---- 缺席跳变 ----
    jumps = []
    prev_x = None
    for r, x, b in zip(rows, xs, branches):
        if x is not None and prev_x is not None and b in ("hold", "pause"):
            jumps.append(abs(x - prev_x))
        prev_x = x

    # ---- 全灭率 ----
    diags = [r.get("traj_diag") for r in rows if r.get("traj_diag") is not None]
    n_veto = sum(1 for d in diags if d.get("ok") is False)

    # ---- v_lat 噪声 ----
    vs = [r.get("v_lat_est") for r in rows]
    dv = [abs(b - a) for a, b in zip(vs, vs[1:]) if a is not None and b is not None]
    sats = [abs(v) for v in vs if v is not None
            and abs(v) >= 2.499]

    coins = sum(1 for r in rows if r.get("cr_coins"))
    maneuvres = sum(1 for r in rows if r.get("maneuver"))

    return {
        "n": len(rows),
        "seconds": round(t_span, 1),
        "group_present_pct": _f(100.0 * sum(1 for r in rows if r.get("n_groups")) / max(1, len(rows))),
        "goal_run_median": _pct(runs, 0.5),
        "goal_run_p90": _pct(runs, 0.9),
        "flips_per_s": _f(flips_per_s),
        "n_fail": n_fail,
        "fail_big_pct": _f(100.0 * fail_big / n_fail) if n_fail else None,
        "fail_rev_pct": _f(100.0 * fail_rev / n_fail) if n_fail else None,
        "retention_pct": _f(100.0 * keep / n_ask) if n_ask else None,
        "absent_jump_max": _f(max(jumps)) if jumps else None,
        "absent_jump_p90": _f(_pct(jumps, 0.9)),
        "veto_pct": _f(100.0 * n_veto / len(diags)) if diags else None,
        "dvlat_gt_0135_pct": _f(100.0 * sum(1 for a in dv if a > 0.135) / len(dv)) if dv else None,
        "vlat_sat_pct": _f(100.0 * len(sats) / len(dv)) if dv else None,
        "cr_coins_pct": _f(100.0 * coins / max(1, len(rows))),
        "maneuver_pct": _f(100.0 * maneuvres / max(1, len(rows))),
    }


def _merge(per: list[dict]) -> dict:
    """四局汇总（加权口径：百分比按样本数加权，分位取各局中位/最大值）。"""
    keys_pct = ("group_present_pct", "fail_big_pct", "fail_rev_pct",
                "retention_pct", "veto_pct", "dvlat_gt_0135_pct",
                "vlat_sat_pct", "cr_coins_pct", "maneuver_pct")
    out = {"n": sum(x["n"] for x in per),
           "seconds": round(sum(x["seconds"] for x in per), 1)}
    for k in keys_pct:
        vals = [x[k] for x in per if x[k] is not None]
        out[k] = _f(sum(vals) / len(vals)) if vals else None
    for k in ("goal_run_median", "goal_run_p90", "flips_per_s",
              "absent_jump_max", "absent_jump_p90"):
        vals = [x[k] for x in per if x[k] is not None]
        out[k] = _f(sum(vals) / len(vals)) if vals else None
    out["n_fail"] = sum(x["n_fail"] for x in per)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    ap.add_argument("--session", action="append", default=None,
                    help="只跑指定会话目录名（可重复）")
    ap.add_argument("--json", action="store_true", help="额外落 JSON 汇总")
    args = ap.parse_args()

    lane_w = load_calib().lane_w_m
    sessions = sorted(p for p in args.root.glob("*")
                      if p.is_dir() and re.fullmatch(r"\d{8}_\d{6}(_p\d+)?", p.name)
                      and list(p.glob("trace_*.jsonl")))
    if args.session:
        sessions = [p for p in sessions if p.name in set(args.session)]
    if not sessions:
        print(f"没有找到 trace_*.jsonl：{args.root}")
        return 1

    per, report = [], {}
    for sess in sessions:
        rows = []
        for f in sorted(sess.glob("trace_*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        rows.sort(key=lambda r: r.get("fid", 0))
        if len(rows) < 50:            # 合成/夹具会话不是对局样本
            continue
        m = analyze(rows, lane_w)
        per.append(m)
        report[sess.name] = m
        print(f"\n[{sess.name}] n={m['n']} {m['seconds']}s")
        for k, v in m.items():
            if k not in ("n", "seconds"):
                print(f"  {k:22s} {v}")

    total = _merge(per)
    report["_all"] = total
    print("\n[汇总]")
    for k, v in total.items():
        print(f"  {k:22s} {v}")

    if args.json:
        out = args.root / "probe_trace_accept.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\nJSON → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
