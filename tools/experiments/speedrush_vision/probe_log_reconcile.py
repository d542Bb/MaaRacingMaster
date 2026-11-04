# -*- coding: utf-8 -*-
"""g112.log × 本项目 trace/HUD 四回合对账 v3：日志到底有用到什么地步。

回合语义（本局日志实证）：
  - stage 1001（=本项目 P1）：30s 时长到点切 stage 1002，无 Round end、无 stat flush
  - stage 1002（=本项目 P2）：以血量判定结束（WIN/LOSE），有 Round end + stat flush
  - 回合结束 ~1s 内有同 stage 的 re-entry drive（结算画面态），跳过不重开
  - cycle_stats 周期结算（~10s）跨阶段累计 → 按时间戳归属窗口 [t0, t1+2s]
只读日志与本项目 debug/demos 产物。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

LOG = Path("E:/DFJS/g112/Saved/Logs/g112.log")
R = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
DBG = R / "debug" / "speedrush"
DEMOS = R / "data" / "speedrush" / "demos"

SES = [("g1P1", "20261015_134818_p1", "1001"), ("g1P2", "20261015_134902_p2", "1002"),
       ("g2P1", "20261015_135030_p1", "1001"), ("g2P2", "20261015_135115_p2", "1002")]

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
                                        destroy=int(m.group(3)), mileage_cm=int(m.group(4)))))
            continue
        m = RE_SETTLE.search(line)
        if m:
            ev.append((s, "settle", dict(overtake=int(m.group(2)),
                                         total=float(m.group(5)))))
            continue
        m = RE_120.search(line)
        if m:
            ev.append((s, "120", m.group(1)))
            continue
        if "Round end detected" in line:
            ev.append((s, "end", line.split("Round end detected in tick: ")[1].split(" ")[0]))
ev.sort()

# 回合窗口：drive 开、end 或下一 drive 关；end 后 2s 内的 re-entry drive 忽略
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
            continue   # 回合结束后结算画面态的 re-entry drive
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
            cur["settles"].append((s, kind, p))
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
            r["settles"].append((s, kind, p))
        else:
            r["s120"].append(p)
if cur is not None:
    cur["t1"] = ev[-1][0]
    rounds.append(cur)


def trace_stats(sess):
    ids, dodges = set(), 0
    with open(DBG / sess / f"trace_{sess}.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            for x in (r.get("passes") or []):
                if x.get("id") is not None:
                    ids.add(x["id"])
            if "dodge" in (r.get("reason") or ""):
                dodges += 1
    return len(ids), dodges


def final_score(sess):
    out = {}
    with open(DEMOS / f"manual_{sess}" / "hud.jsonl", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            for side in ("a", "b"):
                fd = d["fields"][f"score_{side}"]
                if fd.get("value") is not None and fd.get("side") in ("本机(蓝)", "对手(红)"):
                    out[fd["side"]] = fd["value"]
    return out


print("=== 四回合对账 ===")
acc = {}
for label, sess, stage in SES:
    hh, mm, ss = int(sess[9:11]), int(sess[11:13]), int(sess[13:15])
    t0 = hh * 3600 + mm * 60 + ss
    rd = next((r for r in rounds if r["stage"] == stage
               and abs(r["t0"] - t0) <= 8), None)
    if rd is None:
        print(f"{label}: 日志无配对窗口")
        continue
    st = trace_stats(sess)
    sc = final_score(sess)
    tot = sum(p["total"] for s_, k, p in rd["settles"])
    acc[label] = (rd, tot, sc.get("本机(蓝)"), st)
    fl = rd["flush"]
    t0, t1 = rd["t0"], (rd["t1"] or rd["t0"])
    print(f"\n{label}（{sess}）↔ Stage {rd['stage']} @{t0//3600}:{t0%3600//60:02}:"
          f"{t0%60:02}→{t1//3600}:{t1%3600//60:02}:"
          f"{t1%60:02}，{t1-t0:.0f}s，{rd['outcome'] or '时长切阶段'}")
    print(f"  ① 过车：{'flush overtake=' + str(fl['overtake']) if fl else '无 flush'}"
          f" vs 本项目 trace 过车去重={st[0]}"
          f"{'（差 ' + str(fl['overtake'] - st[0]) + '）' if fl else ''}")
    if fl:
        print(f"  ② 金币 collect_coin={fl['coin']} / destroy_car={fl['destroy']} / "
              f"里程={fl['mileage_cm']/100:.0f} m")
    print(f"  ③ Σcycle total={tot:.0f}（{len(rd['settles'])} 周期） / 120 派发 {len(rd['s120'])}"
          f"（{sorted(set(rd['s120']))}） / 本项目 dodge 拍={st[1]} / HUD 局末={sc.get('本机(蓝)')}"
          f"（对手 {sc.get('对手(红)')}）")

print("\n=== 两渠道配对（HUD 局末累计 vs Σcycle+120）===")
for game, keys in (("人工局1", ["g1P1", "g1P2"]), ("人工局2", ["g2P1", "g2P2"])):
    hud_end = acc[keys[1]][2]
    s_tot = sum(acc[k][1] for k in keys)
    n120 = sum(len(acc[k][0]["s120"]) for k in keys)
    print(f"{game}: HUD={hud_end} vs Σcycle={s_tot:.0f} + 120×{n120}={n120*120} → "
          f"残余={hud_end - s_tot - n120*120:.0f}（≈金币计分+连击，日志无逐笔）")
