# -*- coding: utf-8 -*-
"""等待期机会台账（只读，2026-10-16 外部评审）：冷却与等币拍里有没有被挡
住的可达机会——还是本来就没料？

问（GPT 收紧后）：hold:cooling（B 对局 41.6%）与 hold:reach_gap（CHANGE 的
77%）的等待拍里，逐拍回答「是否存在安全、可达、预期积分更高的目标，被门
挡住」。

口径（trace 列直接可读，ledger 在冷却拍不落盘——_select 提前返回，账本
None，如实标注）：
- **等待拍** = reason 含 cooling（CRUISE 冷却）或 == hold:reach_gap
  （CHANGE 内等下一组币）。两类是不同状态的时间，不相加。
- **币可见** = n_groups>0（场内任何位置有币组）；**场选点想挪** =
  |cr_best − executed_lane| > 触发死区（收益场看见了别的位置）。
- **车在近旁** = car_views>0；**等待中落 pass** = 该拍 passes 非空
  （等待期间车还是从身边过去了——机会存在但引擎没追）。
- **被冷却挡住的候选** = cooling 拍里场选点想挪 且 场最佳分 ≥ 一组满币
  （90 分，conf 折前 76.5——用 cr_scores max 分布对比）。
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
TRIGGER_DZ = 0.35        # reach_win 触发死区（道，decision.json）
HIGH_SCORE = 76.5        # 一组满币值（30×3×0.85，收益量级参照）


def load_ticks(ph):
    return [json.loads(l) for l in (DB / ph / f"trace_{ph}.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]


def analyze(name, phases):
    ticks = []
    for ph in phases:
        ticks += load_ticks(ph)
    n = len(ticks)

    buckets = {}
    for tag, pred in (("冷却拍", lambda r: "cooling" in r),
                      ("等币拍", lambda r: r == "hold:reach_gap"),
                      ("其他拍", lambda r: "cooling" not in r
                       and r != "hold:reach_gap")):
        seg = [d for d in ticks if pred(d.get("reason") or "")]
        t_cnt = sum(d.get("dt") or 0 for d in seg)
        b = dict(n=len(seg), t=t_cnt, coin=0, want=0, car=0, pas=0,
                 cool_block=0, score_med=None)
        scores = []
        for d in seg:
            cr_best = d.get("cr_best")
            cr_scores = d.get("cr_scores") or []
            exec_x = d.get("executed_lane")
            if (d.get("n_groups") or 0) > 0:
                b["coin"] += 1
            if cr_best is not None and exec_x is not None \
                    and abs(cr_best - exec_x) > TRIGGER_DZ:
                b["want"] += 1
                if "cooling" in (d.get("reason") or "") \
                        and cr_scores and max(cr_scores) >= HIGH_SCORE:
                    b["cool_block"] += 1
            if (d.get("car_views") or 0) > 0:
                b["car"] += 1
            if d.get("passes"):
                b["pas"] += sum(len(d["passes"]) for _ in (1,))
            if cr_scores:
                scores.append(max(cr_scores))
        if scores:
            b["score_med"] = statistics.median(scores)
        buckets[tag] = b

    total_pass = sum(d.get("passes") and len(d["passes"]) or 0 for d in ticks)
    print(f"\n== {name}：{n} 拍，全局 pass {total_pass} 次 ==")
    for tag in ("冷却拍", "等币拍", "其他拍"):
        b = buckets[tag]
        d = max(1, b["n"])
        print(f"  {tag}：{b['n']} 拍 / {b['t']:.0f}s"
              + (f"（{100*b['t']/sum(x['t'] for x in buckets.values()):.0f}%）"
                 if tag != "其他拍" else ""))
        print(f"    币可见 {100*b['coin']/d:.0f}%  场选点想挪 {100*b['want']/d:.0f}%  "
              f"车在近旁 {100*b['car']/d:.0f}%  该拍落 pass {b['pas']} 次  "
              f"场最佳分 P50={b['score_med'] if b['score_med'] is None else round(b['score_med'],1)}")
        if tag == "冷却拍":
            print(f"    → 被冷却挡住的高价值候选（想挪且场最佳分≥{HIGH_SCORE:.0f}）："
                  f"{b['cool_block']} 拍")


for name, phases in GAMES.items():
    analyze(name, phases)
