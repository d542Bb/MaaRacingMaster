# -*- coding: utf-8 -*-
"""密集段 R1 决策耗时分解（只读离线，2026-10-16 外部评审）：程序的时间被吃
在哪里。

口径（三个量分开，不混）：
- **每次 CHANGE 平均持续多久**（episode = 连续 CHANGE 拍，跨目标切换也拆）；
- **CHANGE 累计占对局时间比例**；
- **CHANGE→真实通过转化率**（episode 结束后的短窗内是否落 pass 事件——
  reach/贴邻位目标无实体 id，按"episode 窗内任意 pass"计，overtake 目标按
  track_id join）。

帧级口径：state/reason 逐拍求和；冷却（hold:cooling）单列。短间隔段=两次
通过间隔 ≤3s 的区间（长空窗的补集）。**人工局的 FSM 态是影子引擎意志，
不代表人——只作参照，真实对照=通过频率**（§1 口径）。缺失字段如实标注：
episode 目标 id 在 target_id 列（reach 目标为 null），pass track_id 可 join；
无 episode 级耗时直接列，由 dt 求和得出。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_r1_time.py
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
    "人工(影子参照)": ["20261016_160353_p1", "20261016_160437_p2"],
}
GAP_MIN = 3.0          # 长空窗界（s）；短间隔段=补集
PASS_AFTER_S = 1.5     # episode 结束后计入 pass 转化的短窗（s）


def load_ticks(ph):
    return [json.loads(l) for l in (DB / ph / f"trace_{ph}.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]


def analyze(name, phases):
    ticks = []
    for ph in phases:
        ticks += load_ticks(ph)
    n = len(ticks)
    total_t = sum(d.get("dt") or 0.0 for d in ticks)
    pass_ts = sorted((d["ts_ns"], {e["id"] for e in (d.get("passes") or [])})
                     for d in ticks if d.get("passes"))
    pass_times = [t for t, _ in pass_ts]
    gaps = [(pass_times[i], pass_times[i + 1])
            for i in range(len(pass_times) - 1)
            if (pass_times[i + 1] - pass_times[i]) / 1e9 > GAP_MIN]

    # ---- 状态时间占比 + 冷却 ----
    state_t = defaultdict(float)
    state_n = defaultdict(int)
    cool_t = 0.0
    for d in ticks:
        st = d.get("state")
        state_t[st] += d.get("dt") or 0.0
        state_n[st] += 1
        r = d.get("reason") or ""
        if "cooling" in r:
            cool_t += d.get("dt") or 0.0

    # ---- CHANGE episode 分解 ----
    # 状态转移拍（cancel:*/done:*）的 trace state 已是转移后的值——episode 的
    # 结局取「离开 CHANGE 的第一个非 CHANGE 拍」的 reason，episode 内的
    # hold:reach_gap 等是过程态不是结局。
    eps = []
    cur = None
    for d in ticks:
        if d.get("state") == "CHANGE":
            if cur is None:
                cur = dict(dt=0.0, kinds=defaultdict(float), tid=d.get("target_id"),
                           end=None, end_state=None, ts_end=None)
            cur["dt"] += d.get("dt") or 0.0
            cur["kinds"][d.get("target_kind")] += 1.0
        else:
            if cur is not None:
                cur["end"] = d.get("reason")
                cur["end_state"] = d.get("state")
                cur["ts_end"] = d["ts_ns"]
                eps.append(cur)
                cur = None
    if cur is not None:
        cur["end"] = "trace_end"
        eps.append(cur)
    ep_t = sum(e["dt"] for e in eps)
    ep_dur = [e["dt"] for e in eps]
    # episode 内过程 reason 时间占比（时间被吃在哪的直接读数）
    ep_reason_t = defaultdict(float)
    for d in ticks:
        if d.get("state") == "CHANGE":
            ep_reason_t[d.get("reason")] += d.get("dt") or 0.0

    # 结局分类 + pass 转化（**每次 pass 只归一个 episode**：归给 timestamp
    # 所在的 episode，否则归给结束于其前 ≤1.5s 的最近 episode——episode 窗
    # 互相重叠，旧口径把一次 pass 记给多个 episode，转化率虚高）
    end_kind = defaultdict(int)
    end_t = defaultdict(float)
    conv = dict(hit=0, total=0)
    conv_by_kind = defaultdict(lambda: [0, 0])
    ep_bounds = [(e["ts_end"] - int(e["dt"] * 1e9), e["ts_end"], e)
                 for e in eps if e["ts_end"]]
    for pt, ids in pass_ts:
        owner = next((e for s, t, e in ep_bounds if s <= pt <= t), None)
        if owner is None:
            owner = next((e for s, t, e in ep_bounds
                          if 0 < pt - t <= int(PASS_AFTER_S * 1e9)), None)
        if owner is None:
            continue                        # 冷却/CRUISE 期间的 pass：不归 episode
        conv["total"] += 1
        main_kind = max(owner["kinds"], key=lambda k: owner["kinds"][k]) \
            if owner["kinds"] else None
        conv_by_kind[main_kind][1] += 1
        owner["_pass"] = True
        conv["hit"] += 1                    # 被归到的 pass 总数（每 pass 至多一次）
    # episode 级转化：分母=episode 数，分子=有 pass 归入的 episode 数
    ep_with_pass = sum(1 for e in eps if e.get("_pass"))
    conv["ep_total"] = len(eps)
    conv["ep_hit"] = ep_with_pass
    for e in eps:
        r = e["end"] or "?"
        kind = ("done" if r.startswith("done") else
                "cancel" if r.startswith("cancel") else
                "switch/transition")
        end_kind[kind] += 1
        end_t[kind] += e["dt"]
        main_kind = max(e["kinds"], key=lambda k: e["kinds"][k]) \
            if e["kinds"] else None
        if e.get("_pass"):
            conv_by_kind[main_kind][0] += 1

    # ---- 短间隔段 vs 长空窗内的时间构成 ----
    def seg_state(seg_ticks):
        t = defaultdict(float)
        dt_total = sum(d.get("dt") or 0.0 for d in seg_ticks)
        for d in seg_ticks:
            t[d.get("state")] += d.get("dt") or 0.0
        return t, dt_total

    marks = [0] + pass_times + [ticks[-1]["ts_ns"] + 1]
    short_ticks, gap_ticks = [], []
    for d in ticks:
        in_gap = any(g0 <= d["ts_ns"] <= g1 for g0, g1 in gaps)
        (gap_ticks if in_gap else short_ticks).append(d)
    sh_t, sh_dt = seg_state(short_ticks)
    gp_t, gp_dt = seg_state(gap_ticks)

    # ---- 输出 ----
    print(f"\n== {name}：{n} 拍 / {total_t:.0f}s，{len(eps)} 个 CHANGE episode，"
          f"长空窗 {len(gaps)} 个 ==")
    print("  状态时间占比：")
    for st in sorted(state_t, key=lambda k: -state_t[k]):
        print(f"    {st or '?':12s} {state_t[st]:6.1f}s（{100*state_t[st]/total_t:4.1f}%）"
              f"  拍 {state_n[st]}")
    print(f"  其中冷却拍（hold:cooling）{cool_t:.1f}s"
          f"（{100*cool_t/total_t:.1f}%）")
    print(f"  ① 每次 CHANGE 均值 {statistics.mean(ep_dur):.2f}s / "
          f"P50 {statistics.median(ep_dur):.2f}s / max {max(ep_dur):.1f}s")
    print(f"  ② CHANGE 累计 {ep_t:.1f}s（{100*ep_t/total_t:.1f}% 对局时间）")
    print(f"  ③ pass 归属（每 pass 至多归一 episode）：归入 episode 的 pass "
          f"{conv['hit']}/{len(pass_ts)}，落在 {conv['ep_hit']}/{conv['ep_total']}"
          f" 个 CHANGE episode（{100*conv['ep_hit']/max(1,conv['ep_total']):.0f}%）"
          + "".join(f"  [{k}: {v[0]}/{v[1]}]" for k, v in conv_by_kind.items()))
    print("  episode 结局（离开 CHANGE 拍的 reason）：")
    for k in sorted(end_kind, key=lambda k: -end_t[k]):
        print(f"    {k:16s} {end_kind[k]:3d} 次 / {end_t[k]:6.1f}s"
              f"（占 CHANGE {100*end_t[k]/max(1,ep_t):4.1f}%）")
    print("  episode 内过程 reason 时间占比（CHANGE 累计时间的去向，top10）：")
    for r in sorted(ep_reason_t, key=lambda k: -ep_reason_t[k])[:10]:
        print(f"    {r:28s} {ep_reason_t[r]:6.1f}s"
              f"（占 CHANGE {100*ep_reason_t[r]/max(1,ep_t):4.1f}%）")
    print(f"  短间隔段（间隔≤3s）时间构成：共 {sh_dt:.0f}s —— " +
          "  ".join(f"{k}={100*v/sh_dt:.0f}%" for k, v in
                    sorted(sh_t.items(), key=lambda kv: -kv[1])[:5]))
    print(f"  长空窗内时间构成：共 {gp_dt:.0f}s —— " +
          "  ".join(f"{k}={100*v/gp_dt:.0f}%" for k, v in
                    sorted(gp_t.items(), key=lambda kv: -kv[1])[:5]))


for name, phases in GAMES.items():
    analyze(name, phases)
