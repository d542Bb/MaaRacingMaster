# -*- coding: utf-8 -*-
"""P0 过车吞吐量诊断：代理在哪丢的车？vs 人工 pads 实况。

三笔账（智能局 trace R2 三阶段 + 人工局 pads 四回合）：
  A 改道时间账：CHANGE 系状态时长占比、完成改道次数——vs 人工实际转向占比/次数
  B 丢车形态：改道中 vs 持守时的过车数、traj_blocked/lat_veto/reach_lost 次数
  C 人工基线：pads lx 转向实况（带滞回的改道计数）
只读 trace/pads，不改仓库外状态。"""
from __future__ import annotations

import json
import os
from pathlib import Path

R = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
DBG = R / "debug" / "speedrush"
DEMOS = R / "data" / "speedrush" / "demos"

AGENT = [("智能P1", "20261015_130528_p1"), ("智能P2a", "20261015_130404_p2"),
         ("智能P2b", "20261015_130612_p2")]
HUMAN = [("人工g1P1", "20261015_134818_p1"), ("人工g1P2", "20261015_134902_p2"),
         ("人工g2P1", "20261015_135030_p1"), ("人工g2P2", "20261015_135115_p2")]

# CHANGE 系 reason（改道占用窗口）；hold/巡航系=其余
CHANGE_REASONS = {"select:reach_win", "moving:reach", "moving:overtake",
                  "aborting", "abort:settled", "cancel:lat_veto",
                  "cancel:reach_lost", "cancel:traj_blocked"}
HOLD_REASONS = {"hold:reach_hold", "select:dodge"}


def agent_report(label, sess):
    tr = [json.loads(l) for l in open(DBG / sess / f"trace_{sess}.jsonl",
                                       encoding="utf-8")]
    dur = (tr[-1]["ts_ns"] - tr[0]["ts_ns"]) / 1e9
    t_change = 0.0
    t_hold = 0.0
    for i, r in enumerate(tr[1:], 1):
        dt = (r["ts_ns"] - tr[i - 1]["ts_ns"]) / 1e9
        if r["reason"] in CHANGE_REASONS:
            t_change += dt
        else:
            t_hold += dt
    # 完成改道次数：converged 收敛拍
    converged = sum(1 for r in tr if r["reason"] == "done:converged:feedback")
    # 取消形态
    cancels = {k: sum(1 for r in tr if r["reason"] == k)
               for k in ("cancel:lat_veto", "cancel:reach_lost", "cancel:traj_blocked")}
    # 过车在改道中 vs 持守时发生
    p_ch, p_hold = 0, 0
    for r in tr:
        n = len(r.get("passes") or [])
        if not n:
            continue
        if r["reason"] in CHANGE_REASONS:
            p_ch += n
        else:
            p_hold += n
    p_total = sum(len(r.get("passes") or []) for r in tr)
    print(f"\n== {label}（{sess}）时长 {dur:.0f}s")
    print(f"  A 改道时间账：CHANGE 系 {t_change:.0f}s（{t_change*100/dur:.0f}%）"
          f" vs 持守/巡航 {t_hold:.0f}s（{t_hold*100/dur:.0f}%）；完成改道 {converged} 次"
          f"（均值 {t_change/max(converged,1):.1f}s/次）")
    print(f"  B 丢车形态：过车总数 {p_total}（改道中 {p_ch} / 持守时 {p_hold}）；"
          f"取消 {cancels}")
    return dur, t_change, converged, p_total


def human_report(label, sess):
    pads = [json.loads(l) for l in open(DEMOS / f"manual_{sess}" / "pads.jsonl",
                                        encoding="utf-8")]
    if len(pads) < 2:
        return
    dur = (pads[-1]["ts_ns"] - pads[0]["ts_ns"]) / 1e9
    # 带滞回的转向计数：|lx|>6000 进入转向，|lx|<2500 归中；方向变化=一次改道
    ON, OFF = 6000, 2500
    steering_t = 0.0
    changes = 0
    cur_dir = 0      # -1 左 / 0 中 / 1 右
    armed = True
    last_t = pads[0]["ts_ns"]
    for p in pads[1:]:
        dt = (p["ts_ns"] - last_t) / 1e9
        last_t = p["ts_ns"]
        lx = p["lx"]
        on = abs(lx) > ON
        if on:
            steering_t += dt
            d = 1 if lx > 0 else -1
            if cur_dir == 0 and armed:
                cur_dir = d
                armed = False
            elif cur_dir != 0 and d != cur_dir and not armed:
                changes += 1
                cur_dir = d
        elif abs(lx) < OFF:
            if cur_dir != 0:
                changes += 1 if False else 0   # 归中不计数，方向反转才计
                armed = True
                cur_dir = 0
    print(f"\n== {label}（{sess}）时长 {dur:.0f}s")
    print(f"  C 人工转向实况：转向时间 {steering_t:.0f}s（{steering_t*100/max(dur,0.01):.0f}%）；"
          f"方向反转改道 {changes} 次")


tot_dur = tot_ch = 0
for label, sess in AGENT:
    d, c, _, _ = agent_report(label, sess)
    tot_dur += d
    tot_ch += c
print(f"\n== 智能局合计：{tot_dur:.0f}s 中改道 {tot_ch:.0f}s（{tot_ch*100/tot_dur:.0f}%）")
for label, sess in HUMAN:
    human_report(label, sess)
