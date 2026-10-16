# -*- coding: utf-8 -*-
"""实验 B：HOLD 期间外推反事实（§8 预登记，看数之前写于设计稿）。

**候选规则**（一次只动一个）：
  - 基线（现行 HOLD）：暴露 m=锚点原值（滞后锚龄 age）；
  - B1：有 v_close（锚间差分已出）也外推 m'=m−v·age/1000；
  - B2：仅锚龄 >150ms 且 v_close 存在时外推。

**预登记指标**（对照同批车·拍，按车次汇总不按帧）：
  1. 外推量分布 |v·age/1000|（异常 >30% m = v_close 错则外推放大错误）；
  2. 投影一致性残差 |cy − (y_h+vpx·cam_h/m)|（距离分桶，外推 vs 原值）；
  3. 速度方向翻转率（车次内连续锚间 v 符号变化）；
  4. 新增可行动目标：外推后 t_ov'=m'/v 落 R3 域 (0.58,2] 的**新增**车·拍
     （原 t_ov 已在域内不算），附可达门通过率（贴身候选 dx 近似）。

**否定条件（预登记）**：同距离桶投影残差 P50 恶化 >20%，或新增可行动目标
主要由外推量异常车贡献，或方向翻转率显著上升 → 否定候选。
只读 debug 产物，不改生产。"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = ["20261016_222406_p1", "20261016_222451_p2",
          "20261016_222615_p1", "20261016_222700_p2",
          "20261016_224328_p1", "20261016_224412_p2"]

Y_H, VPX = 324.2, 640.7
LANE_W = 3.38
BASE, K, TAU, MARGIN = 0.3, 0.4, 0.13, 0.15
R3_LO, R3_HI = 0.58, 2.0
GAP_S = 2.0


def bucket(m: float) -> str:
    if m < 10:
        return "5-10m"
    if m < 20:
        return "10-20m"
    return "20m+"


def need_s(dx_m: float) -> float:
    return BASE + K * abs(dx_m) / LANE_W


# 车次内 v 序列（方向翻转率）
v_seq = collections.defaultdict(list)   # (trace,id) -> [(ts, v)]
res = collections.defaultdict(lambda: {"hold": [], "cf": []})
push = collections.Counter()
new_act = collections.Counter()
new_act_trip = set()
n_hold = 0
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    last_ts = {}
    for r in rows:
        ts = r["ts_ns"]
        cam_h = r.get("dgeo_cam_h")
        executed = r.get("executed_lane")
        shift = executed * LANE_W if executed is not None else None
        cs = r.get("cr_scores")
        xs = None
        if cs and shift is not None:
            n = len(cs)
            xs = [(-r["ro_off"]) * LANE_W + shift - n * 0.25 + (i + 0.5) * 0.5
                  for i in range(n)]
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        for x in (r.get("dist") or []):
            tid = x.get("id")
            m, v, age = x.get("m"), x.get("v"), x.get("age")
            if (x.get("src") or "") != "ANCHOR" or (x.get("method") or "") != "HOLD":
                continue                     # 只看 HOLD 车·拍
            key = (d, tid)
            if key in last_ts and (ts - last_ts[key]) / 1e9 > GAP_S:
                v_seq[key].clear()           # 车次切分
            last_ts[key] = ts
            if m is None:
                continue
            n_hold += 1
            if v is not None and age is not None and v > 0:
                # 外推候选（B1 条件）；B2 = 外加 age>150ms
                age_s = age / 1000.0
                m_cf = m - v * age_s
                push["总量"] += 1
                push["异常(>30%m)" if abs(m_cf - m) > 0.3 * m else "正常"] += 1
                if v_seq[key] and v_seq[key][-1][1] * v < 0:
                    push["方向翻转(车·拍)"] += 1
                v_seq[key].append((ts, v))
                c = cp.get(tid)
                if c is not None and c.get("cy") is not None and cam_h:
                    b = bucket(m)
                    res[(b)]["hold"].append(
                        abs(c["cy"] - (Y_H + VPX * cam_h / m)))
                    res[b]["cf"].append(abs(c["cy"] - (Y_H + VPX * cam_h / max(m_cf, 0.5))))
                t_ov, t_ov_cf = m / v, m_cf / v if m_cf > 0.5 else None
                if t_ov_cf is not None and R3_LO < t_ov_cf <= R3_HI \
                        and not (R3_LO < t_ov <= R3_HI):
                    push["新增可行动(t_ov落R3域)"] += 1
                    new_act[key] = new_act.get(key, 0) + 1
                    new_act_trip.add(key)
                    # 可达门通过率（贴身候选 dx 近似）
                    if xs is not None and c is not None:
                        car_road = c.get("x", 0) if False else None
                    # dx 需米制横向位（car_pos.x 为像素），可达门率另案——
                    # 此处只报新增落域数（预登记的可达门率列在归因局 mcars
                    # trace 才有，记录局 dist 无横向位）

print(f"=== 实验 B 反事实（HOLD 车·拍 {n_hold}） ===")
print(f"外推量分布：{dict(push)}")
print("\n投影一致性残差（同批车·拍，外推 vs 原值，距离分桶）：")
for b in ("5-10m", "10-20m", "20m+"):
    h = sorted(res[b]["hold"])
    c = sorted(res[b]["cf"])
    if h and c:
        n = min(len(h), len(c))
        h50, c50 = h[len(h)//2], c[len(c)//2]
        delta = (c50 - h50) / max(h50, 1) * 100
        print(f"  {b}: n={n} HOLD P50={h50:.0f}px → 外推 P50={c50:.0f}px "
              f"({delta:+.0f}%)")
n_trips = len([k for k in v_seq])
print(f"\n速度方向翻转：{push.get('方向翻转(车·拍)', 0)} 车·拍 / "
      f"{n_trips} 车次")
print(f"新增可行动目标：{push.get('新增可行动(t_ov落R3域)', 0)} 车·拍 / "
      f"{len(new_act_trip)} 车次")
