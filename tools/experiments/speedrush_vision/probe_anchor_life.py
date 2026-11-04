# -*- coding: utf-8 -*-
"""米制锚事件分解（GPT 三步走第 2 步）：锚生命周期 × 近/中远场。

**要回答的两个问题**：
  1. 「没锚」的损失主体在哪一环——从未形成锚 / 锚形成但未更新（HOLD 持锚
     原值不更新）/ 锚更新后丢失 / 轨迹终止（车已过/消失）？
  2. 「越近越没锚」的真实机理——近场深度测量失灵，还是**远场形成锚后
     进近场丢失**（跟踪/关联问题）？两种情况修复方案完全不同。

检验面（每条轨 = trace×id 的一次连续出现，间隔>2s 切分）：
  - 锚是否形成（dist.m 是否出现过）、首次形成距离、丢失时距离与画面位置
  - 丢锚分型：丢失拍 dist.why（TRACK_LOST=本拍丢框/CLUSTER_SWITCH/
    ASSOC_FAIL/EXPIRED）vs 无 dist 条目（跟踪已终结）
  - 「远场形成近场丢失」：last_m≥10m 且丢失时 cy≥400（近场）的轨数；
  - 「近场失灵」：全程 cy≥400（一直在近场）却从未形成锚的轨数。
只读 debug 产物。"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = ["20261016_222406_p1", "20261016_222451_p2",
          "20261016_222615_p1", "20261016_222700_p2",
          "20261016_224328_p1", "20261016_224412_p2"]

GAP_S = 2.0
CY_HORIZON = 350                     # 地平线 y_h≈324；<350=远景/地平线上
CY_NEAR = 450                        # ≥450=近场（画面下部）


def m_bucket(m: float) -> str:
    if m < 10:
        return "近<10m"
    if m <= 20:
        return "中10-20m"
    return "远>20m"


class Track:
    def __init__(self):
        self.seen = 0                # car_pos 在场拍
        self.anchored = 0            # dist.m 有值拍
        self.hold = 0                # method=HOLD 拍（持锚不更新）
        self.first_m = None          # 首次锚距离
        self.last_m = None           # 丢失前最后锚距离
        self.last_cy_anchor = None   # 丢失前最后锚时刻 cy
        self.lost_why = collections.Counter()  # 丢失拍 why（有 m 历史）
        self.cy_all = []             # 全程 cy（近场失灵检验）
        self.m_history = []          # (ts, m) 锚序列


tracks = {}
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    last_ts = {}
    for r in rows:
        ts = r["ts_ns"]
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        dist = {x["id"]: x for x in (r.get("dist") or [])}
        ids = set(cp) | set(dist)
        for tid in ids:
            key = (d, tid)
            t = tracks.get(key)
            if t is None:
                t = tracks[key] = Track()
            elif key in last_ts and (ts - last_ts[key]) / 1e9 > GAP_S:
                t = tracks[key] = Track()   # 间隔切分：新一次出现
            last_ts[key] = ts
            c = cp.get(tid)
            if c is not None:
                t.seen += 1
                t.cy_all.append(c.get("cy"))
            x = dist.get(tid)
            if x is None:
                # 无 dist 条目：跟踪已终结（连 why 都没有）
                if t.anchored > 0:
                    t.lost_why["无dist条目"] += 1
                continue
            if x.get("m") is not None:
                t.anchored += 1
                if (x.get("method") or "") == "HOLD":
                    t.hold += 1
                if t.first_m is None:
                    t.first_m = x["m"]
                t.last_m = x["m"]
                t.last_cy_anchor = c.get("cy") if c else t.last_cy_anchor
                t.m_history.append((ts, x["m"]))
            else:
                if t.anchored > 0:
                    t.lost_why[x.get("why") or "why缺失"] += 1


def formed(t: Track) -> bool:
    return t.anchored > 0


n_seen = sum(1 for t in tracks.values() if t.seen >= 3)   # ≥3 拍算真轨迹
formed_tr = [t for t in tracks.values() if t.seen >= 3 and formed(t)]
never = [t for t in tracks.values() if t.seen >= 3 and not formed(t)]

print(f"=== 锚生命周期（{len(TRACES)} 条 trace，≥3 拍轨迹 {n_seen} 条） ===")
print(f"形成锚 {len(formed_tr)} ({len(formed_tr)/n_seen*100:.0f}%) | "
      f"从未形成锚 {len(never)} ({len(never)/n_seen*100:.0f}%)")

# 锚更新：HOLD 持锚 vs 更新
hold_ticks = sum(t.hold for t in formed_tr)
anc_ticks = sum(t.anchored for t in formed_tr)
print(f"锚在场的 {anc_ticks} 车·拍中：HOLD 持锚不更新 {hold_ticks} "
      f"({hold_ticks/max(anc_ticks,1)*100:.0f}%)，更新 {anc_ticks-hold_ticks} "
      f"({(anc_ticks-hold_ticks)/max(anc_ticks,1)*100:.0f}%)")

# 首次形成距离分桶
fc = collections.Counter(m_bucket(t.first_m) for t in formed_tr)
print(f"锚首次形成距离：{dict(fc)}")

# 丢锚分型（形成过锚的轨）
lc = collections.Counter()
for t in formed_tr:
    for why, n in t.lost_why.items():
        lc[why] += n
print(f"丢失拍 why 分型：{dict(lc.most_common(8))}")

# 「远场形成近场丢失」 vs 「近场失灵」
far_formed_near_lost = 0
for t in formed_tr:
    if t.last_m is not None and t.last_m >= 10 and \
            t.last_cy_anchor is not None and t.last_cy_anchor >= CY_NEAR:
        far_formed_near_lost += 1
near_only_never = 0
for t in never:
    cys = [cy for cy in t.cy_all if cy is not None]
    if cys and min(cys) >= CY_NEAR:
        near_only_never += 1
print(f"\n「远场形成近场丢失」检验：")
print(f"  形成过锚且最后锚距离≥10m、丢失时刻在近场（cy≥{CY_NEAR}）的轨："
      f"{far_formed_near_lost}/{len(formed_tr)}")
print(f"  全程在近场（cy≥{CY_NEAR}）却从未形成锚的轨：{near_only_never}/{len(never)}")

# 从未形成锚的轨的近远分布
nc = collections.Counter()
for t in never:
    cys = [cy for cy in t.cy_all if cy is not None]
    if not cys:
        nc["无cy"] += 1
    elif min(cys) < CY_HORIZON:
        nc["全程远景(未上路)"] += 1
    elif min(cys) >= CY_NEAR:
        nc["全程近场"] += 1
    else:
        nc["中远场出现过"] += 1
print(f"从未形成锚的 {len(never)} 轨的画面分布：{dict(nc)}")
