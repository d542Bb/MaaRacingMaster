# -*- coding: utf-8 -*-
"""V2 A/B 4v4 局级配对聚合（2026-10-12，只读）：新臂（control/fusion=true）vs 老臂（false）。

问（计划稿 §8 A/B，样本裁决=4v4 收案+离群回补）：行为切换对局级指标的影响。
新臂 4 局 = 2026-10-12 08:43/09:03/09:06/09:08；老臂 4 局 = 09:35/09:37/09:39/09:41。
一局 = p1+p2 两个阶段 trace；得分速度主指标从 depth_debug 原始帧 HUD 读（另步），
本脚本聚合 trace 侧全部可比指标：dodge 触发、conserving 段（crash 代理）、
hold 翻面率、跟道率、选择分布、米制覆盖、车流密度（协变量）。

口径：
- dodge = reason=='select:dodge' 触发拍；连续拍归并为一次 episode。
- conserving 段 = 连续 CONSERVE run，起点 tick>150 视为非热身段（crash 代理）。
- hold 翻面 = lane_hold 相邻非 None 值变化次数；跟道率 = |executed-lane_hold|≤0.75
  占 lane_hold 非 None 拍比。
- 米制覆盖 = dist 列表中有 src=='ANCHOR' 且 v 非 None（v_close 已发布）的拍占比。

用法（仓库根，.venv，输出走 stdout）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_ab_pairs.py
"""
from __future__ import annotations

import glob
import json
import os
import re

ROOT = os.path.expandvars(
    r"%APPDATA%\MaaRacingMaster\debug\speedrush\legacy_control_traces")

NEW = ["20261012_084313", "20261012_090338", "20261012_090605", "20261012_090814"]
OLD = ["20261012_093523", "20261012_093733", "20261012_093935", "20261012_094143"]
WARMUP = 150


def _ts(name: str) -> str:
    return re.search(r"trace_(\d{8}_\d{6})_", name).group(1)


def load(p1_prefix: str):
    """一局 = p1 + 时间上紧随其后的 p2（p2 起拍分钟可能与 p1 不同）。"""
    out = []
    p1s = sorted(glob.glob(os.path.join(ROOT, f"trace_{p1_prefix}_p1.jsonl")))
    if not p1s:
        return out
    out.append([json.loads(l) for l in open(p1s[0], encoding="utf-8")])
    t0 = _ts(p1s[0])
    p2s = sorted(glob.glob(os.path.join(ROOT, "trace_*_p2.jsonl")))
    later = [f for f in p2s if _ts(f) > t0]
    if later:
        out.append([json.loads(l) for l in open(min(later, key=_ts), encoding="utf-8")])
    return out


def agg(phases) -> dict:
    ticks = sum(len(p) for p in phases)
    dur = sum((p[-1]["ts_ns"] - p[0]["ts_ns"]) / 1e9 for p in phases if len(p) > 1)
    dodge_ticks = dodge_eps = 0
    flips = hold_ticks = follow = 0
    pick_flips = 0
    conserve_bad = cons_segments = 0
    metric_ticks = 0
    car_views_sum = 0
    picks: dict[float, int] = {}
    prev_hold = None
    prev_pick = None
    for rows in phases:
        run_con = 0
        for i, r in enumerate(rows):
            if r.get("reason") == "select:dodge":
                dodge_ticks += 1
                if not (i > 0 and rows[i - 1].get("reason") == "select:dodge"):
                    dodge_eps += 1
            if r["state"] == "CONSERVE":
                run_con += 1
            else:
                if run_con > 0:
                    cons_segments += 1
                    if i - run_con >= WARMUP:
                        conserve_bad += 1
                run_con = 0
            h = r.get("lane_hold")
            if h is not None:
                hold_ticks += 1
                if prev_hold is not None and h != prev_hold:
                    flips += 1
                prev_hold = h
                if abs(r.get("executed_lane", 0.0) - h) <= 0.75:
                    follow += 1
            else:
                prev_hold = None
            if run_con == 0 and i == len(rows) - 1 and rows[-1]["state"] == "CONSERVE":
                cons_segments += 1
                if len(rows) - 1 - run_con >= WARMUP:
                    conserve_bad += 1
            dist = r.get("dist")
            if isinstance(dist, list) and any(
                    isinstance(c, dict) and c.get("src") == "ANCHOR" and c.get("v") is not None
                    for c in dist):
                metric_ticks += 1
            car_views_sum += r.get("car_views") or 0
            pk = r.get("lane_pick")
            if pk is not None:
                picks[pk] = picks.get(pk, 0) + 1
                if prev_pick is not None and pk != prev_pick:
                    pick_flips += 1
                prev_pick = pk
    return {
        "games_phases": len(phases), "ticks": ticks, "dur_s": round(dur, 1),
        "dodge_eps": dodge_eps, "dodge_ticks": dodge_ticks,
        "conserve_segments": cons_segments, "conserve_non_warmup": conserve_bad,
        "hold_flips": flips, "hold_ticks": hold_ticks, "pick_flips": pick_flips,
        "flip_per_min": round(flips / (dur / 60), 2) if dur else None,
        "follow_rate": round(follow / hold_ticks, 3) if hold_ticks else None,
        "metric_cov": round(metric_ticks / ticks, 3) if ticks else None,
        "car_views_avg": round(car_views_sum / ticks, 2) if ticks else None,
        "picks": dict(sorted(picks.items(), key=lambda kv: -kv[1])),
    }


def main() -> None:
    for name, arms in (("新臂 control/fusion=true", NEW), ("老臂 control/fusion=false", OLD)):
        print(f"== {name} ==")
        for gp in arms:
            phases = load(gp)
            if not phases:
                print(f"  {gp}: 无 trace")
                continue
            a = agg(phases)
            print(f"  {gp}: ticks={a['ticks']} dur={a['dur_s']}s "
                  f"dodge={a['dodge_eps']}ep/{a['dodge_ticks']}tk "
                  f"conserve={a['conserve_segments']}seg(非热身{a['conserve_non_warmup']}) "
                  f"hold_flip={a['hold_flips']}({a['flip_per_min']}/min) "
                  f"pick_flip={a['pick_flips']} "
                  f"follow={a['follow_rate']} metric={a['metric_cov']} "
                  f"cars={a['car_views_avg']}")
            print(f"      picks={a['picks']}")


if __name__ == "__main__":
    main()
