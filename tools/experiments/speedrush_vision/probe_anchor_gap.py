# -*- coding: utf-8 -*-
"""距离锚供数缺口分解（只读离线，2026-10-16 外部评审）：已被看见的车辆从
检测到可信距离锚之间缺在哪。

问：哪些 dist.why 原因损失最多有效目标时间、发生条件、可修复性、修复后
能补回多少猎车拍。口径：
- **目标级**（tick·car 观测）：每个原因损失多少观测、多少条轨、持续多久、
  之后是否恢复——帧级原因会重叠（同拍可既有无锚车也有有锚车），不相加。
- **帧级**：有锚拍 / 有无锚车的拍（两者重叠，只并排报）。
- **逐轨生命周期**：按 (phase, id) 分组——轨的锚可用率、最长无锚段、原因、
  恢复与否、最后已知距离（损失观测的 m 缺失，用该轨最后已知锚距/无锚=
  距离未知）。
- 修复后补回估计是**上界**：损失观测的速度未知，t_ov 无法复算——只报
  「距离合理 + 轨存续够暖机」的观测数，不冒充实测。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_anchor_gap.py
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

DB = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\debug\speedrush")
GAMES = {
    "程序A": ["20261016_130735_p1", "20261016_130819_p2"],
    "程序B": ["20261016_135024_p1", "20261016_135108_p2"],
    "人工": ["20261016_160353_p1", "20261016_160437_p2"],
}
WHYS = ("NO_ANCHOR_YET", "ASSOC_FAIL", "TRACK_LOST", "EXPIRED",
        "CLUSTER_SWITCH")
D_LO, D_HI = 5.0, 80.0      # 猎车距离合理域（§1.1 同口径）


def load_ticks(ph):
    return [json.loads(l) for l in (DB / ph / f"trace_{ph}.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]


def analyze(name, phases):
    ticks = []
    for ph in phases:
        for d in load_ticks(ph):
            d["_ph"] = ph
            ticks.append(d)

    # ---- 目标级：tick·car 观测桶（互斥） ----
    obs = defaultdict(int)          # 桶 → tick·car 观测数
    anchors_v = []                  # 有锚+v 的观测 (m, v)
    anchors_nov = []                # 有锚速度未稳的观测 m
    no_dist_ids = 0
    for d in ticks:
        pos = {c["id"]: c for c in (d.get("car_pos") or [])
               if c.get("id") is not None}
        dist = {e["id"]: e for e in (d.get("dist") or [])
                if e.get("id") is not None}
        for cid in set(pos) | set(dist):
            e = dist.get(cid)
            if e is None:
                obs["无 dist 条目"] += 1
                no_dist_ids += 1
                continue
            m, v, why = e.get("m"), e.get("v"), e.get("why")
            if m is not None:
                if v is not None and v > 0:
                    obs["有锚+速度稳"] += 1
                    anchors_v.append((m, v))
                elif v is not None:
                    obs["有锚+远离/掠过"] += 1
                else:
                    obs["有锚+速度未稳"] += 1
                    anchors_nov.append(m)
            else:
                obs[f"无锚:{why}"] += 1

    # ---- 帧级（重叠，只并排报） ----
    n = len(ticks)
    f_anch = sum(1 for d in ticks
                 if any(e.get("m") is not None for e in (d.get("dist") or [])))
    f_noanch = sum(1 for d in ticks
                   if any(e.get("m") is None for e in (d.get("dist") or [])))

    # ---- 逐轨生命周期 ----
    tracks = defaultdict(list)      # (ph, id) → [(ts, m, v, why)]
    for d in ticks:
        for e in (d.get("dist") or []):
            if e.get("id") is None:
                continue
            tracks[(d["_ph"], e["id"])].append(
                (d["ts_ns"], e.get("m"), e.get("v"), e.get("why")))
    # 每轨：锚可用率、最长无锚段（原因）、段后是否恢复、最后已知锚距
    why_tracks = defaultdict(lambda: dict(n=0, run_len=[], recovered=0,
                                          d_last=[]))
    lost_tracks = 0
    for key, seq in tracks.items():
        seq.sort()
        nobs = len(seq)
        nanch = sum(1 for _t, m, _v, _w in seq if m is not None)
        # 最长连续无锚段（首尾无锚也算段）
        best = None
        run = 0
        run_why = None
        for i, (_t, m, _v, w) in enumerate(seq):
            if m is None:
                run += 1
                run_why = run_why or w or "?"
                is_last = i == nobs - 1
                if best is None or run > best[0]:
                    best = (run, run_why, is_last)
            else:
                run = 0
                run_why = None
        if best is None:
            continue
        run_len, why, at_end = best
        why_tracks[why]["n"] += 1
        why_tracks[why]["run_len"].append(run_len)
        # 段后是否恢复：非结尾段（后面还有锚）= 恢复
        if not at_end:
            why_tracks[why]["recovered"] += 1
        last_m = next((m for _t, m, _v, _w in reversed(seq)
                       if m is not None), None)
        if last_m is not None:
            why_tracks[why]["d_last"].append(last_m)
        if nanch == 0:
            lost_tracks += 1

    # ---- 输出 ----
    total_obs = sum(obs.values())
    print(f"\n== {name}：{n} 拍，{len(tracks)} 条轨，{total_obs} 个 tick·车观测 ==")
    print(f"  帧级（重叠）：有锚拍 {f_anch}（{100*f_anch/n:.0f}%）  "
          f"含无锚车拍 {f_noanch}（{100*f_noanch/n:.0f}%）")
    print("  目标级观测桶（互斥）：")
    for k in sorted(obs, key=lambda k: -obs[k]):
        pct = 100 * obs[k] / total_obs
        print(f"    {k:24s} {obs[k]:6d}（{pct:4.1f}%）")
    print(f"  逐轨（按最长无锚段的原因）：全生命周期无锚轨 {lost_tracks}/{len(tracks)}")
    for why in (*WHYS, "?"):
        t = why_tracks.get(why)
        if not t or not t["n"]:
            continue
        rl = t["run_len"]
        dl = sorted(t["d_last"])
        dstr = (f"P50={dl[len(dl)//2]:.0f}m max={dl[-1]:.0f}m"
                if dl else "距离未知")
        print(f"    {why:14s} 轨 {t['n']:3d}  最长无锚段 P50={statistics.median(rl):.0f}拍 "
              f"max={max(rl)}  段后恢复 {100*t['recovered']/t['n']:.0f}%  "
              f"最后已知距 {dstr}")
    # ---- 修复后补回估计（上界）----
    print("  修复后补回上界（速度未知，t_ov 不可复算——只报距离合理+轨存续≥3拍的损失观测）：")
    for why in (*WHYS, "?"):
        t = why_tracks.get(why)
        if not t or not t["n"]:
            continue
        plausible = sum(1 for m in t["d_last"] if D_LO <= m <= D_HI)
        print(f"    {why:14s} 距离合理轨 {plausible}/{t['n']}"
              f"（{100*plausible/t['n']:.0f}%）")


for name, phases in GAMES.items():
    analyze(name, phases)
