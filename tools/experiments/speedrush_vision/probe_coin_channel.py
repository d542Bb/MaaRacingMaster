# -*- coding: utf-8 -*-
"""币渠道诊断：代理为什么只吃 6-12 组（人工 55）？

对每回合找「币可见窗」（n_groups≥1 连续 ≥1s），逐窗问：
  A 收益场指没指币：cr_best 与币组 x（best_x）的距离——场选点和币线重合吗
  B 触发没触发：窗内 reach_win 拍数
  C 被什么打断：窗内 cancel:* 分布
  D 车动没动：窗内 executed 向 best_x 的位移
只读 trace。"""
from __future__ import annotations

import json
import os
import statistics as st
from pathlib import Path

ROOT = (Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug"
        / "speedrush").resolve()
LW = 3.38
SES = [("A/B-P1", "20261015_193301_p1"), ("A/B-P2", "20261015_193345_p2"),
       ("基线P2a", "20261015_130404_p2"), ("基线P2b", "20261015_130612_p2"),
       ("人工g1P2", "20261015_134902_p2"), ("人工g2P2", "20261015_135115_p2")]


def trace_path(sess: str) -> Path | None:
    p = (ROOT / sess / f"trace_{sess}.jsonl").resolve()
    if ROOT != p and ROOT not in p.parents:
        return None   # 路径约束：只允许 ROOT 下的会话目录
    return p if p.exists() else None


for label, s in SES:
    p = trace_path(s)
    if p is None:
        continue
    tr = [json.loads(l) for l in open(p, encoding="utf-8")]
    # 币可见窗：n_groups≥1 连续段
    wins = []
    cur = None
    for i, r in enumerate(tr):
        if (r.get("n_groups") or 0) >= 1:
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        else:
            if cur is not None and (tr[cur[1]]["ts_ns"] - tr[cur[0]]["ts_ns"]) / 1e9 >= 1.0:
                wins.append(cur)
            cur = None
    if cur is not None and (tr[cur[1]]["ts_ns"] - tr[cur[0]]["ts_ns"]) / 1e9 >= 1.0:
        wins.append(cur)

    # 全局：币可见拍里 reach_win 占比
    coin_ticks = [r for r in tr if (r.get("n_groups") or 0) >= 1]
    rw_in = sum(1 for r in coin_ticks if r["reason"] == "select:reach_win")
    # 场指没指币：币组路心系位置 = (best_x + executed)×LW；cr_best 同系（米）
    gaps = []
    for r in coin_ticks:
        if r.get("cr_best") is not None and r.get("best_x") is not None \
                and r.get("executed_lane") is not None:
            gaps.append(abs(r["cr_best"]
                            - (r["best_x"] + r["executed_lane"]) * LW))
    # 窗级：触发/取消/车动
    fired = canceled = approached = 0
    cancel_kinds = {}
    durs = []
    for a, b in wins:
        w = tr[a:b + 1]
        durs.append((w[-1]["ts_ns"] - w[0]["ts_ns"]) / 1e9)
        if any(r["reason"] == "select:reach_win" for r in w):
            fired += 1
        cs = [r["reason"] for r in w if r["reason"].startswith("cancel:")]
        if cs:
            canceled += 1
            for c in cs:
                cancel_kinds[c] = cancel_kinds.get(c, 0) + 1
        # 车动没动：窗末 executed 向 best_x 靠近了吗（起点执行位 vs 终点）
        e0, e1 = w[0].get("executed_lane"), w[-1].get("executed_lane")
        bx = w[0].get("best_x")
        if e0 is not None and e1 is not None and bx is not None:
            if abs(e1 - bx) < abs(e0 - bx) - 0.05:
                approached += 1
    print(f"== {label}（{s}）：币可见拍 {len(coin_ticks)}/{len(tr)}"
          f"（{len(coin_ticks)*100//len(tr)}%），窗 {len(wins)} 个"
          f"（时长 P50 {st.median(durs):.1f}s）" if wins else
          f"== {label}（{s}）：币可见拍 {len(coin_ticks)}/{len(tr)}，无 ≥1s 窗")
    if wins:
        print(f"  A 场指币：|cr_best−币x| P50 {st.median(gaps):.2f}m" if gaps else
              "  A 场指币：无可比拍")
        print(f"  B 触发：币可见拍中 reach_win {rw_in}/{len(coin_ticks)}"
              f"（{rw_in*100//max(len(coin_ticks),1)}%）；窗级触发 {fired}/{len(wins)}")
        print(f"  C 打断：含 cancel 的窗 {canceled}/{len(wins)}，{cancel_kinds}")
        print(f"  D 车动：窗内向币靠近 {approached}/{len(wins)}（位移 >0.05 道）")
    print()
