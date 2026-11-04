# -*- coding: utf-8 -*-
"""锚点清单对账（asheet 列首局数据）：检测→清单→轨道漏斗逐环复算。

回答（实验 A 的阻断点定位）：零锚的损失在「检测→清单→轨道」的哪一环——
清单输入过滤 / 生产门槛 / 映射 / worker 滞后？

数据源：20261017 4 trace（asheet 列首局）。只读 debug 产物。"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = ["20261017_115032_p1", "20261017_115107_p2",
          "20261017_115223_p1", "20261017_115308_p2"]

GAP_S = 2.0


def _safe(d: str) -> Path:
    """trace 路径校验（安全扫描：固定目录 + 禁 .. 分量，越界即拒绝）。"""
    if ".." in Path(d).parts:
        raise ValueError(f"路径分量含 ..：{d}")
    p = (DBG / d).resolve()
    if DBG.resolve() not in p.parents:
        raise ValueError(f"路径越界：{d}")
    return p


def _rows(d: str) -> list[dict]:
    with open(_safe(d) / f"trace_{d}.jsonl", encoding="utf-8") as f:
        return [json.loads(l) for l in f]


# ── 环一：清单→轨道（条目 vs 映射、fid 滞后、orphan） ──
c1 = collections.Counter()
lag_all = collections.Counter()
for d in TRACES:
    for r in _rows(d):
        a = r.get("asheet")
        if not a:
            continue
        c1["asheet拍"] += 1
        c1["条目"] += a["entries"]
        c1["映射成功"] += a["mapped"]
        if a.get("orphan"):
            c1["orphan拍"] += 1
        if a.get("fid_lag") is not None:
            lag_all[a["fid_lag"]] += 1
print("=== 环一：清单→轨道 ===")
print(f"asheet拍 {c1['asheet拍']}，条目 {c1['条目']}，映射成功 {c1['映射成功']}"
      f"（差值 {c1['条目']-c1['映射成功']}，{100*(c1['条目']-c1['映射成功'])/max(c1['条目'],1):.1f}%），"
      f"orphan拍 {c1['orphan拍']}")
print(f"fid_lag（清单帧滞后，帧）分布：{dict(sorted(lag_all.items()))}")

# ── 环二：生产门槛（条目 vs 当前检测数） ──
c2 = collections.Counter()
for d in TRACES:
    for r in _rows(d):
        a = r.get("asheet")
        if not a:
            continue
        yolo = r.get("yolo_cars") or 0
        e = a["entries"]
        c2["条目=当前检测" if e == yolo
           else ("条目<当前检测" if e < yolo else "条目>当前检测")] += 1
print("\n=== 环二：生产门槛（asheet 拍上条目 vs 当前检测数） ===")
print(dict(c2))

# ── 环三：零锚轨的清单到达 ──
tot = collections.Counter()
lag_min = collections.Counter()
for d in TRACES:
    last_ts = {}
    tracks = collections.defaultdict(lambda: {"seen": 0, "anch": 0,
                                              "ticks": []})
    asheet_ts = []
    for r in _rows(d):
        ts = r["ts_ns"]
        a = r.get("asheet")
        if a:
            asheet_ts.append((ts, a.get("fid_lag")))
        cp = r.get("car_pos") or []
        dist = {x["id"]: x for x in (r.get("dist") or [])}
        for c in cp:
            key = (d, c["id"])
            t = tracks[key]
            if key in last_ts and ts - last_ts[key] > GAP_S * 1e9:
                tracks[key] = t = {"seen": 0, "anch": 0, "ticks": []}
            last_ts[key] = ts
            t["seen"] += 1
            t["ticks"].append(ts)
            x = dist.get(c["id"])
            if x and x.get("m") is not None:
                t["anch"] += 1
    for t in tracks.values():
        if t["seen"] < 3:
            continue
        tot["轨"] += 1
        if t["anch"]:
            tot["有锚轨"] += 1
            continue
        tot["零锚轨"] += 1
        lo, hi = min(t["ticks"]), max(t["ticks"])
        sheet_in = [lag for ts, lag in asheet_ts if lo <= ts <= hi]
        if sheet_in:
            tot["零锚:生命期有清单"] += 1
            lag_min[min(sheet_in)] += 1
        else:
            tot["零锚:生命期无清单"] += 1
print("\n=== 环三：零锚轨的清单到达 ===")
print(dict(tot))
print(f"零锚轨生命期内清单的 fid_lag 最小值分布：{dict(sorted(lag_min.items()))}")
