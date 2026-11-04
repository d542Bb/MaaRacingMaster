# -*- coding: utf-8 -*-
"""速度暖机离线反事实（只读，2026-10-16 外部评审）：更早的速度估计能不能
更早给出**可行动**目标——不是更早输出一个数字。

问（GPT 收紧后）：用前两帧原始锚（method=HOLD，未外推）差分能提前多少拍
给出速度？提前给出的估计方向对不对、量级误差多大、有多少落入 R2 可猎窗
（t_ov∈(2,6]s）且方向正确？

口径：
- **参考速度** = 该轨全部已出速度估计的中位数（比首个 v 稳健；参考自身有
  误差——RANGE_PROP 外推值，不能当绝对真值，如实标注）。
- **反事实估计** = 连续两个原始锚（method=HOLD）的差分 v=(m_prev−m_now)/dt；
  首个可算点 = 反事实提前点。
- **方向误判** = sign(cf) 与 sign(ref) 不一致（会把远离车当目标/反之）。
- **新增可行动目标** = 反事实在当前首个 v **之前**给出的、方向正确且
  t_ov∈(2,6] 的估计——当前口径这些拍无速度不可用。观测桶占比≠可挽回机会
  占比，本数是机会口径不是桶口径。
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
    "人工": ["20261016_160353_p1", "20261016_160437_p2"],
}
T_LO, T_HI = 2.0, 6.0      # R2 可猎窗（t_ov，s）


def load_ticks(ph):
    return [json.loads(l) for l in (DB / ph / f"trace_{ph}.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]


def analyze(name, phases):
    tracks = defaultdict(list)      # (ph, id) → [(ts, m, v, method)]
    for ph in phases:
        for d in load_ticks(ph):
            for e in (d.get("dist") or []):
                if e.get("id") is None:
                    continue
                tracks[(ph, e["id"])].append(
                    (d["ts_ns"], e.get("m"), e.get("v"), e.get("method")))

    n_tracks = n_ref = 0
    leads = []              # 提前量（拍）
    dir_mis = [0, 0]        # 反事实方向误判 [错, 总]（首个可算点 vs ref）
    mag_err = []            # 量级误差 |cf−ref|/|ref|（首个可算点）
    gain_tracks = 0         # 反事实提前给出方向正确且在窗内的轨
    gain_ticks = 0          # 上面的累计拍数（新增可行动 tick·轨）
    t_ov_agree = [0, 0]     # 窗内/窗外分类一致 [一致, 总]
    tracks_wo_cf = 0        # 暖机窗内原始锚不足两帧、反事实算不出的轨
    for key, seq in tracks.items():
        seq.sort()
        n_tracks += 1
        vs = [v for _t, _m, v, _mt in seq if v is not None]
        if not vs:
            continue
        n_ref += 1
        ref = statistics.median(vs)
        first_v_i = next(i for i, (_t, _m, v, _mt) in enumerate(seq)
                         if v is not None)
        # 反事实：**锚事件**差分——method=HOLD 是持锚原值，连续两条 HOLD 的 m
        # 相同（同一锚，差分恒 0 无信息）；m 变化=新锚形成。用前两个锚事件的
        # 间距差分（当前暖机门=锚间 ≥150ms 才出 v，反事实回答"锚事件更早
        # 凑齐两帧时能提前多少"）
        events = []         # 锚事件 (ts, m)：m 相对上一条变化
        prev_m = None
        for t, m, _v, _mt in seq:
            if m is None:
                continue
            if prev_m is not None and m != prev_m:
                events.append((t, m))
            prev_m = m
        cf = []             # (idx_in_seq, v_cf)
        for j in range(1, len(events)):
            dt = (events[j][0] - events[j - 1][0]) / 1e9
            if dt <= 0:
                continue
            v_cf = (events[j - 1][1] - events[j][1]) / dt
            # 定位事件在 seq 里的拍序（首个 ≥ 该 ts 的条目）
            idx = next((i for i, (t, _m, _v, _mt) in enumerate(seq)
                        if t >= events[j][0]), len(seq) - 1)
            cf.append((idx, v_cf))
        cf_pre = [(i, v) for i, v in cf if i < first_v_i]
        if not cf_pre:
            tracks_wo_cf += 1
            continue
        idx_cf, v_cf = cf_pre[0]
        leads.append(first_v_i - idx_cf)
        # 方向与量级（首个可算点 vs ref）
        if v_cf * ref <= 0:
            dir_mis[0] += 1
        dir_mis[1] += 1
        if abs(ref) > 1e-9:
            mag_err.append(abs(v_cf - ref) / abs(ref))
        # 反事实提前段里方向正确且 t_ov∈(2,6] 的可行动估计
        m_at = seq[idx_cf][1]
        gain = False
        for i, v in cf_pre:
            m = seq[i][1]
            if v * ref > 0 and m is not None and m > 0 and v > 0 \
                    and T_LO < m / v <= T_HI:
                gain = True
                gain_ticks += 1
        if gain:
            gain_tracks += 1
        # t_ov 分类一致性（反事实 vs 参考，在同一点）
        if m_at is not None and m_at > 0 and v_cf > 0 and ref > 0:
            t_cf, t_ref = m_at / v_cf, m_at / ref
            same = (T_LO < t_cf <= T_HI) == (T_LO < t_ref <= T_HI)
            t_ov_agree[0] += same
            t_ov_agree[1] += 1

    print(f"\n== {name}：{n_tracks} 条轨（有参考速度 {n_ref}）==")
    if leads:
        print(f"  反事实可算轨 {len(leads)}（暖机窗内原始锚不足两帧 "
              f"{tracks_wo_cf} 条）")
        print(f"  提前量：P50={statistics.median(leads):.0f} 拍 "
              f"P90={sorted(leads)[int(len(leads)*0.9)]} 拍 "
              f"max={max(leads)}（1 拍≈0.05s）")
    if dir_mis[1]:
        print(f"  首个反事实估计方向误判 {dir_mis[0]}/{dir_mis[1]}"
              f"（{100*dir_mis[0]/dir_mis[1]:.0f}%）"
              + (f"  量级误差 P50={100*statistics.median(mag_err):.0f}%"
                 if mag_err else ""))
    if t_ov_agree[1]:
        print(f"  t_ov∈(2,6] 窗分类一致（cf vs ref） {t_ov_agree[0]}/{t_ov_agree[1]}"
              f"（{100*t_ov_agree[0]/t_ov_agree[1]:.0f}%）")
    print(f"  新增可行动目标：{gain_tracks} 条轨 / {gain_ticks} 个 tick·轨"
          f"（当前口径这些拍无速度不可用——机会口径非桶口径）")


for name, phases in GAMES.items():
    analyze(name, phases)
