#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探针：计划滚动预测的「起点横速」用哪个来源，1 秒位移误差最小？

背景（2026-10-30 并线复盘）：程序局的模型横速 ``v_lat_est`` 在并线中读到
0.4~1.1 道/s，而标线实测只有 0.1~0.2 道/s（3~5 倍虚高）。计划与刹停预算都
消费这个状态，于是提前反打、并线净位移接近 0。候选滚动预测从哪个速度起步，
决定了「计划的后果预估」对不对。

做法（只读，不 import maaracing_master）：
    1 s 窗内 ``Δx = v0·T + a·∫(T−τ)u dτ``（a=6 道/s²，与产线同值），
    v0 分别取 v_lat_est / v_ctrl / v_line / v_line 优先的融合，与标线位移
    ΔL（同 k、两端一致）比误差。窗内参考系跳动（frame_shift / fd 突变）丢弃。

数据：logs/<run>/trace/speedrush.jsonl。

用法：
    python probe_rollout_velocity.py
    python probe_rollout_velocity.py --root D:/data/MaaRacingMaster
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path

A_LAT = 6.0      # 与产线 planner.a_lat_gain 同值
WIN_S = 1.0
JUMP_LANE = 0.5
FD_JUMP = 0.3


def rows_of(trace: Path) -> list[dict]:
    out = []
    with open(trace, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "lane_obs" in r and "lat_ctrl" in r:
                out.append(r)
    return out


def lines_at(r: dict) -> dict[int, float]:
    lo = r.get("lane_obs") or {}
    return {int(c["k"]): float(c["L"]) for c in (lo.get("lines") or [])
            if c.get("accepted") and c.get("k") is not None}


def disp(a: dict, b: dict) -> float | None:
    a, b = lines_at(a), lines_at(b)
    common = [k for k in a if k in b]
    if not common:
        return None
    d = [a[k] - b[k] for k in common]
    if len(d) >= 2 and max(d) - min(d) > 0.4:
        return None
    return statistics.median(d)


def stable(rows: list[dict], i: int, j: int) -> bool:
    for q in rows[i:j + 1]:
        why = str((q.get("plan") or {}).get("reason") or "")
        if why.startswith("plan:frame_shift"):
            return False
    f0 = (rows[i].get("plan") or {}).get("fd")
    f1 = (rows[j].get("plan") or {}).get("fd")
    if f0 is not None and f1 is not None and abs(f1 - f0) > FD_JUMP:
        return False
    return True


def windows(rows: list[dict]) -> list[dict]:
    t = [0.0]
    for r in rows:
        t.append(t[-1] + float(r.get("dt") or 0.05))
    n = len(rows)
    out = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and t[j + 1] < t[i] + WIN_S:
            j += 1
        if t[j] < t[i] + WIN_S * 0.85:
            break
        dx = disp(rows[i], rows[j])
        if dx is not None and stable(rows, i, j):
            A = 0.0
            for q in range(i, j):
                A += (t[j] - t[q]) * float(rows[q].get("steer_norm") or 0) \
                    * (t[q + 1] - t[q])
            lc = rows[i].get("lat_ctrl") or {}
            out.append({
                "t": t[i], "dx": dx, "A": A, "T": t[j] - t[i],
                "merging": (rows[i].get("route") or {}).get("ph")
                == "merging",
                "v_model": float(rows[i].get("v_lat_est") or 0.0),
                "v_ctrl": lc.get("v_ctrl"),
                "v_line": lc.get("v_line"),
            })
        i = j
    return out


SOURCES = (
    ("模型 v_lat_est", lambda q: q["v_model"]),
    ("控制融合 v_ctrl", lambda q: q["v_ctrl"]),
    ("标线 v_line", lambda q: q["v_line"]),
    ("v_line 优先/退化 v_ctrl", lambda q: q["v_line"] if q["v_line"]
     is not None else q["v_ctrl"]),
    ("零速（只算杆积分）", lambda q: 0.0),
)


def report(name: str, wins: list[dict]) -> None:
    if len(wins) < 20:
        print("\n[%s] 窗不足（%d）" % (name, len(wins)))
        return
    print("\n[%s] 1s 窗 %d（并线中 %d）"
          % (name, len(wins), sum(1 for q in wins if q["merging"])))
    for label, get in SOURCES:
        errs, errs_m = [], []
        for q in wins:
            v0 = get(q)
            if v0 is None:
                continue
            e = abs(v0 * q["T"] + A_LAT * q["A"] - q["dx"])
            errs.append(e)
            if q["merging"]:
                errs_m.append(e)
        if len(errs) < 20:
            print("  %-22s 可用窗不足（%d）" % (label, len(errs)))
            continue
        errs.sort()
        m_txt = "—"
        if len(errs_m) >= 15:
            errs_m.sort()
            m_txt = "%.2f" % statistics.median(errs_m)
        print("  %-22s n=%4d 中位 %.2f道 p90 %.2f道 | 并线中位 %s道"
              % (label, len(errs), statistics.median(errs),
                 errs[int(len(errs) * 0.9)], m_txt))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    args = ap.parse_args()
    root = Path(args.root) / "logs"
    runs = sorted(d.name for d in root.iterdir()
                  if d.is_dir() and d.name[0].isdigit()
                  and (d / "trace" / "speedrush.jsonl").is_file())
    allw = []
    for run in runs:
        w = windows(rows_of(root / run / "trace" / "speedrush.jsonl"))
        if w:
            report(run, w)
            allw.extend(w)
    print("\n" + "=" * 70)
    report("全部会话合计", allw)


if __name__ == "__main__":
    main()
