# -*- coding: utf-8 -*-
"""R3 可达性离线反事实（GPT 三步走第 1 步）：旧判据 vs 新判据，同一批车次。

**要回答的问题**：第二道门（可达门）是不是"假门"——放宽时间条件能捞回
多少车次、捞回的有没有副作用（碰撞）、有没有真实收益（对应真实 pass）。

**预登记变体**（只改到达贴身区的时间条件，不动收益分/碰撞 veto/变道
模型/R2；写于看数之前）：
  - 旧判据：t_ov ≥ 0.3 + 0.4·|dx|/lane_w + 0.28（起步+位移+τ+margin）
  - V1 去起步：0.3s 起步是变道语义（打灯/回正），贴身跟随不要——
    t_ov ≥ 0.4·|dx|/lane_w + 0.28
  - V2 去起步+余量减半：t_ov ≥ 0.4·|dx|/lane_w + 0.14
  - V3 纯位移（激进上界）：t_ov ≥ 0.4·|dx|/lane_w

**车次口径**（GPT 纠正：互斥淘汰原因才能相加）：同一 id 连续车·拍聚成
车次（间隔 >2s 切分）；车次按「旧门通过 / 仅新门通过 / 全拒 / 碰撞 /
走廊外」互斥归因，每车次取最优拍（t_ov 最大者=最早机会）。

**pass 闭环**：passes 列带 track_id，与车次 id 直接 join——新增放行车次
里有多少最终对应真实过车。只读 debug 产物，不改闸。"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
# 归因局（R3 闸开，mcars 供数完整）
TRACES = ["20261016_222406_p1", "20261016_222451_p2",
          "20261016_222615_p1", "20261016_222700_p2"]

LANE_W = 3.38
HUG_LO, HUG_HI = 1.859, 2.366
CRASH = -99.0
HORIZON = 2.0
GAP_S = 2.0                          # 车次切分间隔
VARIANTS = {                         # (base, k, extra)
    "旧": (0.3, 0.4, 0.28),
    "V1去起步": (0.0, 0.4, 0.28),
    "V2去起步余量减半": (0.0, 0.4, 0.14),
    "V3纯位移": (0.0, 0.4, 0.0),
}


def need_s(dx_m: float, base: float, k: float) -> float:
    return base + k * abs(dx_m) / LANE_W


def gate(t_ov: float, dx_m: float, v: tuple) -> bool:
    base, k, extra = v
    return t_ov >= need_s(dx_m, base, k) + extra


# 车次聚合：trace × id → [(tick 数据)]
class Trip:
    def __init__(self, tid):
        self.tid = tid
        self.ticks = []

    def add(self, t):
        self.ticks.append(t)


trips = {}                           # (trace, id) -> Trip
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    last_ts = {}
    pass_ids = set()
    for r in rows:
        for x in (r.get("passes") or []):
            if x.get("id") is not None:
                pass_ids.add(x["id"])
    for r in rows:
        cs, ro, executed = r.get("cr_scores"), r.get("ro_off"), \
            r.get("executed_lane")
        if not cs or ro is None or executed is None:
            continue
        ts = r["ts_ns"]
        shift = executed * LANE_W
        n = len(cs)
        xs = [-ro * LANE_W + shift - n * 0.25 + (i + 0.5) * 0.5
              for i in range(n)]
        for m in (r.get("mcars") or []):
            tid = m.get("id")
            if tid is None:
                continue
            key = (d, tid)
            t = trips.get(key)
            if t is None:
                t = trips[key] = Trip(tid)
            elif key in last_ts and (ts - last_ts[key]) / 1e9 > GAP_S:
                t = trips[key] = Trip(tid)   # 间隔切分：新一次出现
            last_ts[key] = ts
            x_m, t_ov = m.get("x"), m.get("t_ov")
            if x_m is None or t_ov is None or t_ov > HORIZON:
                continue                      # 非 R3 域车·拍不进车次窗
            car_road = x_m + shift
            hcs = [(abs(car_road - xc), i) for i, xc in enumerate(xs)
                   if HUG_LO < abs(car_road - xc) <= HUG_HI]
            if not hcs:
                t.ticks.append(dict(kind="走廊外"))
                continue
            _, i = min(hcs)
            crash = cs[i] is not None and cs[i] <= CRASH
            dx = abs(xs[i] - shift)
            t.ticks.append(dict(kind="hug", t_ov=t_ov, dx=dx, crash=crash,
                                tid=tid))

# 车次互斥归因：每车次取 t_ov 最大的 hug 拍（最早机会）
cnt = collections.Counter()
new_ids = set()
for (d, tid), t in trips.items():
    hugs = [k for k in t.ticks if k["kind"] == "hug"]
    if not hugs:
        cnt["走廊外车次" if any(k["kind"] == "走廊外"
                                for k in t.ticks) else "非R3域车次"] += 1
        continue
    best = max(hugs, key=lambda k: k["t_ov"])
    if best["crash"]:
        cnt["碰撞判据杀车次"] += 1
        continue
    passed = [g for g, v in VARIANTS.items() if gate(best["t_ov"], best["dx"], v)]
    if not passed:
        cnt["全拒车次"] += 1
        continue
    if "旧" in passed:
        cnt["旧门已放行车次"] += 1
    else:
        # 新增放行：记录放行它的最严变体（V1<V2<V3 严格递减）
        first = next(g for g in ("V1去起步", "V2去起步余量减半", "V3纯位移")
                     if g in passed)
        cnt[f"仅{first}放行"] += 1
        new_ids.add((d, tid))

print("=== 车次互斥归因（同一批车次，取最早机会拍） ===")
for k, v in cnt.most_common():
    print(f"  {k}: {v}")
r3 = sum(v for k, v in cnt.items()
         if k not in ("非R3域车次",))
print(f"\nR3 域车次合计 {r3}；变体放行：",
      {g: sum(1 for (d, tid), t in trips.items()
              if (h := [k for k in t.ticks if k["kind"] == "hug"])
              and not (b := max(h, key=lambda k: k["t_ov"]))["crash"]
              and gate(b["t_ov"], b["dx"], v))
       for g, v in VARIANTS.items()})

# pass 闭环：新增放行车次 vs 旧门放行车次的真实 pass 率
def trip_pass(d, tid, pass_by_trace):
    return (d, tid) in pass_by_trace

pass_by_trace = collections.defaultdict(set)
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    for r in rows:
        for x in (r.get("passes") or []):
            if x.get("id") is not None:
                pass_by_trace[d].add(x["id"])

new_pass = sum(1 for d, tid in new_ids if tid in pass_by_trace[d])
old_pass = sum(1 for (d, tid), t in trips.items()
               if (h := [k for k in t.ticks if k["kind"] == "hug"])
               and not (b := max(h, key=lambda k: k["t_ov"]))["crash"]
               and gate(b["t_ov"], b["dx"], VARIANTS["旧"])
               and tid in pass_by_trace[d])
old_n = sum(1 for (d, tid), t in trips.items()
            if (h := [k for k in t.ticks if k["kind"] == "hug"])
            and not (b := max(h, key=lambda k: k["t_ov"]))["crash"]
            and gate(b["t_ov"], b["dx"], VARIANTS["旧"]))
print(f"\npass 闭环：旧门放行 {old_n} 车次，对应真实 pass {old_pass} "
      f"({old_pass/max(old_n,1)*100:.0f}%)；仅新门放行 {len(new_ids)} 车次，"
      f"对应真实 pass {new_pass} ({new_pass/max(len(new_ids),1)*100:.0f}%)")
