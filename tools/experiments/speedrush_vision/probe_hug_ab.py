# -*- coding: utf-8 -*-
"""R3 贴身猎车实机 3 局初筛对账：R3-on（20261016 21:5x）× R3-off 基线 × g112.log。

主判据（decision.json _note 归位义务）：积分率（trace 跨度口径，HUD 累计分
÷ 总时长）与局胜负。机制面：通过频率（去重过车/s）、贴身占比（d_min 落
贴身窗 [1.859,2.366] 的过车比）、R3 活动占比（平台分带 [130,160] 拍——
legacy 单币组上限 106.5、双组 183 跨不进该带）、改道 churn（CHANGE 系拍
占比 + ABORT 拍/s）。

R3-on 证据基线：基线局两局平台带拍数=0、cr_max≈105（legacy 上限），新局
5/6 出现平台带 → 闸已吃到实机（215209_p1 无带=该 25s 短阶段无米制锚贴身
候选，cr_max 115 略超 legacy 单组上限 106.5=衰减带上小币，存疑不改判）。

只读日志与本项目 debug 产物，不改闸、不调权重。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

LOG = Path("E:/DFJS/g112/Saved/Logs/g112.log")
DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"

# (label, 局内 P1, P2)；基线 A=0.15 期、B=0.35 期（均 R3-off）
GAMES = [
    ("R3局1", "20261016_215209_p1", "20261016_215240_p2"),
    ("R3局2", "20261016_215352_p1", "20261016_215437_p2"),
    ("R3局3", "20261016_215552_p1", "20261016_215637_p2"),
    ("基线B", "20261016_135024_p1", "20261016_135108_p2"),
    ("基线A", "20261016_130735_p1", "20261016_130819_p2"),
]

HUG_LO, HUG_HI = (0.625 - 0.075) * 3.38, (0.625 + 0.075) * 3.38   # 1.859/2.366 m
BAND_LO, BAND_HI = 130.0, 160.0

TS = re.compile(r"\[\d{4}\.\d{2}\.\d{2}-(\d{2})\.(\d{2})\.(\d{2}):(\d{3})\]")
RE_SYNC = re.compile(r"_onRunStateSync: RoundNum=(\w+), Stage=(\w+), .*State=(\w+)")
RE_FLUSH = re.compile(r"Round stat flush: overtake=(\d+) collect_coin=(\d+) "
                      r"destroy_car=(\d+) mileage_cm=(\d+)")
RE_SETTLE = re.compile(r"Settled\. mileage=([\d.]+)m, overtake=(\d+), "
                       r"mileageScore=([\d.]+), overtakeScore=([\d.]+), total=([\d.]+)")
RE_120 = re.compile(r"dispatchPlayerCarScore: type=(\w+), score=120\b")


def sec_of(line):
    m = TS.search(line)
    if m is None:
        return None
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            + int(m.group(4)) / 1000.0)


ev = []
with open(LOG, encoding="utf-8", errors="replace") as f:
    for line in f:
        s = sec_of(line)
        if s is None:
            continue
        m = RE_SYNC.search(line)
        if m and m.group(3) == "DRIVING":
            ev.append((s, "drive", m.group(2)))
            continue
        m = RE_FLUSH.search(line)
        if m:
            ev.append((s, "flush", dict(overtake=int(m.group(1)), coin=int(m.group(2)),
                                        destroy=int(m.group(3)),
                                        mileage_cm=int(m.group(4)))))
            continue
        m = RE_SETTLE.search(line)
        if m:
            ev.append((s, "settle", dict(total=float(m.group(5)))))
            continue
        m = RE_120.search(line)
        if m:
            ev.append((s, "120", m.group(1)))
            continue
        if "Round end detected" in line:
            ev.append((s, "end", line.split("Round end detected in tick: ")[1].split(" ")[0]))
ev.sort()

# 回合窗口：drive 开、end 或下一 drive 关；end 后 2s 内 re-entry 忽略
rounds = []
cur = None
last_end_at = None
for s, kind, p in ev:
    if kind == "drive":
        if cur is not None:
            cur["t1"] = s
            rounds.append(cur)
            cur = None
        elif last_end_at is not None and s - last_end_at <= 2:
            continue
        cur = {"stage": p, "t0": s, "t1": None, "outcome": None,
               "settles": [], "s120": [], "flush": None}
    elif cur is not None:
        if kind == "end":
            cur["t1"] = s
            cur["outcome"] = p
            rounds.append(cur)
            cur = None
            last_end_at = s
        elif kind == "settle":
            cur["settles"].append(p)
        elif kind == "120":
            cur["s120"].append(p)
        elif kind == "flush":
            cur["flush"] = p
    elif kind == "flush" and last_end_at is not None and s - last_end_at <= 2 and rounds:
        rounds[-1]["flush"] = p
        rounds[-1]["t1"] = s
    elif kind in ("settle", "120") and last_end_at is not None and s - last_end_at <= 2 and rounds:
        r = rounds[-1]
        if kind == "settle":
            r["settles"].append(p)
        else:
            r["s120"].append(p)
if cur is not None:
    cur["t1"] = ev[-1][0]
    rounds.append(cur)


def trace_metrics(sess):
    rows = [json.loads(l) for l in
            open(DBG / sess / f"trace_{sess}.jsonl", encoding="utf-8")]
    n = len(rows)
    span = (rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9
    ids = set()
    hug_pass = 0
    for r in rows:
        for x in (r.get("passes") or []):
            if x.get("id") is None:
                continue
            if x["id"] not in ids:
                ids.add(x["id"])
                if HUG_LO <= x.get("d_min", -1) <= HUG_HI:
                    hug_pass += 1
    st = {}
    for r in rows:
        s = r.get("state")
        st[s] = st.get(s, 0) + 1
    chg = st.get("CHANGE", 0) + st.get("ABORT_CHANGE", 0)
    band = sum(1 for r in rows if r.get("cr_scores")
               and any(BAND_LO <= s <= BAND_HI for s in r["cr_scores"]))
    score = 0
    with open(DBG / sess / "game_summary.jsonl", encoding="utf-8") as f:
        for line in f:
            score = json.loads(line).get("score") or 0
    return dict(n=n, span=span, passes=len(ids), hug_pass=hug_pass,
                chg=chg, chg_share=chg / n, abort=st.get("ABORT_CHANGE", 0),
                cruise=st.get("CRUISE", 0) / n, band=band, band_share=band / n,
                score=score)


def log_match(sess, stage):
    """按 stage + trace 起点配对日志回合窗口。"""
    hh, mm, ss = int(sess[9:11]), int(sess[11:13]), int(sess[13:15])
    t0 = hh * 3600 + mm * 60 + ss
    return next((r for r in rounds if r["stage"] == stage
                 and abs(r["t0"] - t0) <= 8), None)


print("=== R3 贴身猎车实机 3 局初筛（主判据：积分率+局胜负） ===")
print(f"贴身窗 [{HUG_LO:.3f}, {HUG_HI:.3f}] m；平台分带 [{BAND_LO:.0f},{BAND_HI:.0f}]")
for label, p1, p2 in GAMES:
    m1, m2 = trace_metrics(p1), trace_metrics(p2)
    total_s = m1["span"] + m2["span"]
    gain = m2["score"] - m1["score"]     # HUD 累计分：P2 增量=P2 阶段得分
    rate = m2["score"] / total_s         # 局积分率（累计分/总时长）
    rd2 = log_match(p2, "1002")
    print(f"\n--- {label}（{p1[:15]}… / {p2[:15]}…）---")
    print(f"  P1 {m1['span']:.1f}s 分{m1['score']}（{m1['score']/m1['span']:.1f}/s）| "
          f"P2 {m2['span']:.1f}s 增{gain}（{gain/m2['span']:.1f}/s）| "
          f"局积分率 {rate:.1f}/s")
    print(f"  过车：P1 {m1['passes']}（{m1['passes']/m1['span']:.2f}/s）+ "
          f"P2 {m2['passes']}（{m2['passes']/m2['span']:.2f}/s）= {m1['passes']+m2['passes']}；"
          f"贴身窗内 {m1['hug_pass']+m2['hug_pass']}")
    print(f"  改道churn：CHANGE系 {m1['chg_share']*100:.0f}%/{m2['chg_share']*100:.0f}%，"
          f"ABORT {m1['abort']}+{m2['abort']} 拍（{(m1['abort']+m2['abort'])/total_s:.2f}/s）；"
          f"R3平台带 {m1['band']}+{m2['band']} 拍（{m1['band_share']*100:.0f}%/{m2['band_share']*100:.0f}%）")
    if rd2 is None:
        print("  日志无 P2 配对窗口")
        continue
    t0, t1 = rd2["t0"], (rd2["t1"] or rd2["t0"])
    fl = rd2["flush"]
    tot = sum(p["total"] for p in rd2["settles"])
    print(f"  日志 P2：{t1-t0:.0f}s {rd2['outcome'] or '时长切'}；"
          f"{'overtake=' + str(fl['overtake']) + ' coin=' + str(fl['coin']) + ' destroy=' + str(fl['destroy']) + ' 里程=' + format(fl['mileage_cm']/100, '.0f') + 'm' if fl else '无 flush'}；"
          f"Σcycle {tot:.0f}（{len(rd2['settles'])} 周期）/ 120×{len(rd2['s120'])}")
