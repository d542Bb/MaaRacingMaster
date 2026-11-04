# -*- coding: utf-8 -*-
"""人工对照局（录制影子观察）四回合取数：分数 / 收益场选点 vs 人工横位 /
提前量 / 影子 FSM。只读 trace 与录制 HUD，不碰仓库外状态。"""
from __future__ import annotations

import json
import math
import os
import statistics as st
from pathlib import Path

R = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
DBG = R / "debug" / "speedrush"
DEMOS = R / "data" / "speedrush" / "demos"

SESSIONS = [
    ("人工局1-P1", "20261015_134818_p1"),
    ("人工局1-P2", "20261015_134902_p2"),
    ("人工局2-P1", "20261015_135030_p1"),
    ("人工局2-P2", "20261015_135115_p2"),
]

LANE_W = 3.38          # m，lane_w_m 真源换算面（与 control 局同值）
DEAD_LANE = 0.15       # 道，reach_win 死区
DEAD_M = DEAD_LANE * LANE_W


def rows(p: Path):
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def final_score(demo: Path):
    """录制 HUD 末段可信分数行（局结局取数面）。"""
    out = {}
    for d in rows(demo / "hud.jsonl"):
        for side in ("a", "b"):
            f = d["fields"][f"score_{side}"]
            if f.get("value") is not None and f.get("side") in ("本机(蓝)", "对手(红)"):
                out[f["side"]] = (f["value"], d["frame_id"])
    return out


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


for label, sess in SESSIONS:
    tr = rows(DBG / sess / f"trace_{sess}.jsonl")
    gs = rows(DBG / sess / "game_summary.jsonl")
    sc = final_score(DEMOS / f"manual_{sess}")
    if not tr:
        print(f"{label}: 无 trace（{sess}）")
        continue
    n = len(tr)
    dur = tr[-1]["ts_ns"] - tr[0]["ts_ns"] if n > 1 else 0
    # FSM 分布
    fsm = {}
    for r in tr:
        fsm[r["reason"]] = fsm.get(r["reason"], 0) + 1
    # 影子收益场覆盖
    cr = [r for r in tr if r.get("cr_best") is not None]
    # 分歧：|人工 executed − cr_best|（路心系米）
    div = [abs(r["executed_lane"] * LANE_W - r["cr_best"]) for r in cr]
    big = [d for d in div if d > DEAD_M]   # reach 想改而人没改（死区外）
    # 提前量：reach_win 拍 min(cr_coins)
    lead = [min(r["cr_coins"]) for r in tr
            if r.get("cr_coins") and r["reason"] == "select:reach_win"]
    # 反事实杆值幅度
    steer = [abs(r["steer_x"]) for r in tr if r.get("steer_x") is not None]
    # 人工横位（|executed_lane| P50/P90，偏离路心多远）
    el = [abs(r["executed_lane"]) for r in tr if r.get("executed_lane") is not None]

    print(f"== {label}（{sess}）")
    print(f"  tick {n}，时长 {dur/1e9:.0f}s；分数：{sc if sc else 'HUD 无可信行'}")
    if gs:
        g = gs[-1]
        print(f"  game_summary: ticks={g.get('ticks')} dur={g.get('dur_s')}s "
              f"dodge={g.get('dodge_episodes')} conserve={g.get('conserve_segments')} "
              f"score={g.get('score')}")
    top = sorted(fsm.items(), key=lambda kv: -kv[1])[:6]
    print(f"  FSM top: {top}")
    print(f"  cr_best 覆盖 {len(cr)}/{n}（{len(cr)*100//max(n,1)}%）；"
          f"reach_win {sum(1 for r in tr if r['reason']=='select:reach_win')} 拍")
    if div:
        print(f"  |人工横位−cr_best| m：P50 {st.median(div):.2f} / P90 {pct(div,0.9):.2f} / "
              f"max {max(div):.2f}；死区外（>{DEAD_M:.2f}m）{len(big)} 拍（{len(big)*100//len(div)}%）")
    if lead:
        print(f"  提前量（reach_win min cr_coins）P50 {st.median(lead):.2f}s "
              f"n={len(lead)}")
    if steer:
        print(f"  反事实杆值 |steer| P50 {st.median(steer):.3f} / P90 {pct(steer,0.9):.3f}")
    if el:
        print(f"  人工横位 |executed| 道：P50 {st.median(el):.2f} / P90 {pct(el,0.9):.2f}")
    print()
