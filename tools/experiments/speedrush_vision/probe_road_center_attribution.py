# -*- coding: utf-8 -*-
"""P2 归因：用产线**现成审计列**拆深度侧抖动，不新造实验、不改控制。

历史结论审计（2026-10-27 本轮）：

* 10-07 `a013e61` 已在 demo 上量过"翻跳主源=找边锁错结构，非 MoGe 噪声"
  （近静止窗栅格路心二阶差 P90 0~0.11 道，找边左右缘二阶差 1.8/1.9 道），
  并背书"栅格区读数当供数主人"。
* 10-17 归因：走廊缺席拍 9 成以上 = 主候选被供数门拒绝。
* 10-27 `probe_lane_velocity` 撤回"线位移比深度细 3~10 倍"——左右一致是
  **共模抖动**，巡航段线位移自身就有 0.8 道/s 噪声。

所以本轮要答的不是"谁抖"（已知），而是**产线节拍上、同拍对照下**：

1. 稳的那一路（栅格）到底有多少拍可用？
2. 它可用时，供数主人是谁？（优先级链有没有把稳的一路让出去）
3. 两路同拍的分歧面有多大？
4. 各自的一阶差与二阶差（复现 a013e61 口径，但用产线 trace 而非 demo）
5. 供数源切换四组的台阶（wall/region 两两配对，含符号）

口径：只用**同一拍同时记录**的列，跨拍比较要求间隔 ≤ 0.2 s（避免把黑视当抖动）。
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

MAX_GAP_S = 0.2


def q(v, p):
    return sorted(v)[min(len(v) - 1, int(p * len(v)))] if v else float("nan")


def load(root: Path):
    out = []
    for ls in sorted((root / "logs").iterdir()):
        p = ls / "trace" / "speedrush.jsonl"
        if not p.is_file():
            continue
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8")
                .splitlines() if l.strip()]
        out.append((ls.name, [r for r in rows
                              if isinstance(r.get("fid"), int)
                              and r.get("executed_lane") is not None]))
    return out


def series(rows, key, tkey="ts_mono_ns"):
    v = []
    for r in rows:
        x = r.get(key)
        if isinstance(x, (int, float)):
            v.append((r.get(tkey) or r.get("ts_ns") or 0, float(x)))
    return v


def diffs(v):
    """一阶差（|Δ|）与二阶差（|x[t+1]−2x[t]+x[t−1]|），只连间隔 ≤0.2s 的相邻拍。"""
    d1 = [abs(b - a) for (t0, a), (t1, b) in zip(v, v[1:])
          if t1 and t0 and (t1 - t0) / 1e9 <= MAX_GAP_S]
    d2 = [abs(c - 2 * b + a) for (t0, a), (t1, b), (t2, c) in zip(v, v[1:], v[2:])
          if t0 and t1 and t2 and max(t1 - t0, t2 - t1) / 1e9 <= MAX_GAP_S]
    return d1, d2


def hop_pairs(rows):
    """新鲜路心读数按供数源配对（四组），含符号。"""
    pts = []
    for r in rows:
        if r.get("ro_hold") or r.get("dgeo_new") is False:
            continue
        if isinstance(r.get("road_offset"), (int, float)):
            pts.append((r.get("ts_mono_ns") or 0, float(r["road_offset"]),
                        r.get("ro_source")))
    g = defaultdict(list)
    for (t0, a, s0), (t1, b, s1) in zip(pts, pts[1:]):
        if not t0 or (t1 - t0) / 1e9 > MAX_GAP_S:
            continue
        g[(s0, s1)].append(b - a)
    return g, Counter(s for _, _, s in pts)


def main() -> None:
    root = Path.home() / "AppData/Roaming/MaaRacingMaster"
    tot = defaultdict(list)
    src_mix = Counter()
    grid_when_edge = Counter()
    for name, rows in load(root):
        gd = series(rows, "ro_grid")
        ed = series(rows, "ro_edge")
        od = series(rows, "ro_off")
        n = len(rows)
        avail = {"grid": len(gd) / n, "edge": len(ed) / n, "off": len(od) / n}
        for k, v in avail.items():
            tot["avail_" + k].append(v)
        g1, g2 = diffs(gd)
        e1, e2 = diffs(ed)
        o1, o2 = diffs(od)
        for tag, (d1, d2) in (("grid", (g1, g2)), ("edge", (e1, e2)),
                              ("off", (o1, o2))):
            tot[tag + "_d1"] += d1
            tot[tag + "_d2"] += d2
        byt = {t: (r.get("ro_grid"), r.get("ro_edge"), r.get("ro_source"))
               for r in rows for t in [r.get("ts_mono_ns") or 0]}
        for r in rows:
            g, e = r.get("ro_grid"), r.get("ro_edge")
            if isinstance(g, (int, float)) and isinstance(e, (int, float)):
                tot["diverge"].append(abs(g - e))
                src_mix[r.get("ro_source")] += 1
                grid_when_edge["grid_present"] += 1
        for k, v in hop_pairs(rows)[1].items():
            src_mix["fresh:" + str(k)] += v
        for (s0, s1), ds in hop_pairs(rows)[0].items():
            tot[str((s0, s1))] += ds
    print("会话数", len(load(root)))
    print("\n一、可用性（占全部拍）")
    for k in ("avail_grid", "avail_edge", "avail_off"):
        print(f"  {k[6:]:5s} 各会话均值 {100*statistics.mean(tot[k]):.0f}%")
    print("\n二、同拍分歧 |栅格路心 − 找边对心|（两路都有值的拍）")
    dv = tot["diverge"]
    print(f"  n={len(dv)}  p50 {q(dv,.5):.2f} 道  p90 {q(dv,.9):.2f} 道  "
          f">0.5 道占 {100*sum(1 for v in dv if v>0.5)/len(dv):.0f}%")
    print("\n三、各自稳定性（产线 trace，间隔≤0.2s 相邻拍）")
    for tag, label in (("grid", "栅格路心"), ("edge", "找边对心"),
                       ("off", "实际供数 off")):
        d1, d2 = tot[tag + "_d1"], tot[tag + "_d2"]
        print(f"  {label:10s} 一阶差 p50 {q(d1,.5):.3f} p90 {q(d1,.9):.3f} | "
              f"二阶差 p50 {q(d2,.5):.3f} p90 {q(d2,.9):.3f} 道  n {len(d1)}")
    print("\n四、新鲜路心读数按供数源配对（含符号；正=读数向右移）")
    for k, v in sorted(tot.items(), key=lambda kv: -len(kv[1])):
        if not k.startswith("(") or len(v) < 8:
            continue
        print(f"  {k:26s} n={len(v):4d} |Δ|p50 {q([abs(x) for x in v],.5):.3f} "
              f"p90 {q([abs(x) for x in v],.9):.3f}  均值 {statistics.mean(v):+.3f}")
    print("\n五、供数源占比")
    fresh = {k[5:]: v for k, v in src_mix.items() if k.startswith("fresh:")}
    allf = sum(fresh.values()) or 1
    for k, v in sorted(fresh.items(), key=lambda kv: -kv[1]):
        print(f"  {str(k):12s} {100*v/allf:4.0f}%  ({v})")


if __name__ == "__main__":
    main()
