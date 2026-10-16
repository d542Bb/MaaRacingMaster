# -*- coding: utf-8 -*-
"""锚更新策略诊断（GPT 立项裁决：先诊断三问，不先改建立/更新规则）。

**三问**（GPT 预设，写于看数之前）：
  Q1 近场为什么不建锚——35 条零锚轨（近场跑但从未形成锚）按 dist.why
     序列（NO_ANCHOR_YET=锚未攒够对/ASSOC_FAIL/TRACK_LOST）、框高置信
     （car_h.conf，框在=原始观测在）、深度质量（dgeo_nsol/dgeo_rejects）
     分类：框都不稳 vs 框在但锚没攒够 vs 关联失败。
  Q2 HOLD 为什么不更新——HOLD 持续时长（锚龄 age）、HOLD 期间原始观测
     是否存在（car_h.conf>0 分型：有观测但没更新 vs 原始观测缺失）。
     不先设"超 X 秒必须重测"，先证明重测时有可用数据。
  Q3 更新后的锚是否更有用——HOLD vs RESCALED 的地面投影残差
     |cy − (y_h + vpx·cam_h/m)|（针孔地面投影，gate0 y_h/vpx + dgeo_cam_h）：
     RESCALED（重标锚）若残差显著小于 HOLD，说明更新的锚跟车更紧、更有用。

只读 debug 产物，不改建立/更新规则。"""
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
Y_H, VPX = 324.2, 640.7              # gate0 地面标定


class Track:
    def __init__(self):
        self.seen = 0
        self.anchored = 0
        self.why_seq = collections.Counter()   # 无锚拍 dist.why
        self.h_conf_all = []                   # 全程框高置信
        self.h_conf_anc = []                   # 有锚拍框高置信
        self.h_conf_hold = []                  # HOLD 拍框高置信
        self.first_m = None
        self.last_m = None


tracks = {}
tick_dgeo = []                       # (ts, nsol, rejects)
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    last_ts = {}
    for r in rows:
        ts = r["ts_ns"]
        tick_dgeo.append((ts, r.get("dgeo_nsol"), r.get("dgeo_rejects")))
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        dist = {x["id"]: x for x in (r.get("dist") or [])}
        ch = {x["id"]: x for x in (r.get("car_h") or [])}
        for tid in set(cp) | set(dist):
            key = (d, tid)
            t = tracks.get(key)
            if t is None:
                t = tracks[key] = Track()
            elif key in last_ts and (ts - last_ts[key]) / 1e9 > GAP_S:
                t = tracks[key] = Track()
            last_ts[key] = ts
            if tid in cp:
                t.seen += 1
                x = ch.get(tid)
                if x is not None:
                    t.h_conf_all.append(x.get("conf"))
            x = dist.get(tid)
            if x is None:
                continue
            if x.get("m") is not None:
                t.anchored += 1
                if t.first_m is None:
                    t.first_m = x["m"]
                t.last_m = x["m"]
                if (x.get("method") or "") == "HOLD":
                    c = ch.get(tid)
                    if c is not None:
                        t.h_conf_hold.append(c.get("conf"))
                if (x.get("method") or "") == "RESCALED_ANCHOR":
                    c = ch.get(tid)
                    if c is not None:
                        t.h_conf_anc.append(c.get("conf"))
            else:
                t.why_seq[x.get("why") or "why缺失"] += 1

formed = {k: t for k, t in tracks.items() if t.seen >= 3 and t.anchored > 0}
never = {k: t for k, t in tracks.items() if t.seen >= 3 and t.anchored == 0}
near_never = [t for t in never.values()
              if t.h_conf_all and t.first_m is None
              and any(c is not None and c >= 450 for c in [] ) or True]
# 近场零锚：由 probe_anchor_life 口径（cy≥450 全程），此处以 h_conf_all 存在
# 且 seen≥3、从无锚为代理——精确定位用 trace cy 列，此处沿用生命周期探针的 35 条

print(f"=== Q1 近场为什么不建锚（零锚轨 {len(never)} 条，≥3 拍） ===")
w = collections.Counter()
for t in never.values():
    for why, n in t.why_seq.items():
        w[why] += n
print(f"零锚轨的 dist.why 序列分布：{dict(w.most_common(8))}")
# 零锚轨的框观测情况
n_h = sum(1 for t in never.values() if t.h_conf_all)
n_conf = sum(1 for t in never.values()
             if any(c is not None and c >= 0.3 for c in t.h_conf_all))
print(f"零锚轨中有框高观测的 {n_h}/{len(never)}，其中 conf≥0.3 的 {n_conf} 条")

print(f"\n=== Q2 HOLD 为什么不更新 ===")
hold_age = []
hold_with_h = 0
hold_without_h = 0
for t in tracks.values():
    hold_age  # noqa
hold_age = []
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    last_ts = {}
    for r in rows:
        ts = r["ts_ns"]
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        dist = {x["id"]: x for x in (r.get("dist") or [])}
        ch = {x["id"]: x for x in (r.get("car_h") or [])}
        for tid, x in dist.items():
            if x.get("m") is None or (x.get("method") or "") != "HOLD":
                continue
            a = x.get("age")
            if a is not None:
                hold_age.append(a)
            if ch.get(tid) is not None:
                hold_with_h += 1
            else:
                hold_without_h += 1
if hold_age:
    hold_age.sort()
    n = len(hold_age)
    print(f"HOLD 拍 {n} 个：锚龄 P50={hold_age[n//2]:.0f}ms "
          f"P90={hold_age[int(n*0.9)]:.0f}ms max={hold_age[-1]:.0f}ms")
print(f"HOLD 拍中有框高观测 {hold_with_h}（原始观测在、锚没更新） vs "
      f"无 {hold_without_h}（原始观测缺失）")

print(f"\n=== Q3 更新后的锚是否更有用（地面投影残差） ===")
res_hold, res_resc = [], []
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    for r in rows:
        cam_h = r.get("dgeo_cam_h")
        if cam_h is None:
            continue
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        for x in (r.get("dist") or []):
            m = x.get("m")
            c = cp.get(x.get("id"))
            if m is None or m < 3 or c is None or c.get("cy") is None:
                continue
            cy_pred = Y_H + VPX * cam_h / m     # 针孔地面投影（框心近似）
            res = abs(c["cy"] - cy_pred)
            if (x.get("src") or "") == "ANCHOR":
                res_hold.append(res)
            elif (x.get("src") or "") == "RESCALED_ANCHOR":
                res_resc.append(res)
for name, arr in (("HOLD 持锚", res_hold), ("RESCALED 重标", res_resc)):
    if arr:
        arr.sort()
        n = len(arr)
        print(f"{name} {n} 车·拍：残差 P50={arr[n//2]:.0f}px "
              f"P90={arr[int(n*0.9)]:.0f}px")
