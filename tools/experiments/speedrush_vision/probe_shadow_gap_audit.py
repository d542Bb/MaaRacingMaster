# -*- coding: utf-8 -*-
"""影子审计 v2（不改驾驶）：长空窗内的可超车机会 + 程序局逐次通过归因。

v2 修正：closing 用同 id 相邻拍差分（v1 误存上一拍距离）；横位四分类
（同道/邻道/远道/未知）；可超车拍 = 邻/同道 且闭合>1m/s；归因只对程序局
（人工局 best_x 是影子引擎意志，不代表人），用 cr_coins 区分"瞄准币"。
"""
import json
import statistics
from pathlib import Path

DB = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\debug\speedrush")
GAMES = {
    "程序A": [("p1", "20261016_130735_p1"), ("p2", "20261016_130819_p2")],
    "程序B": [("p1", "20261016_135024_p1"), ("p2", "20261016_135108_p2")],
}
HUMAN = [("p1", "20261016_160353_p1"), ("p2", "20261016_160437_p2")]
GAP_MIN = 3.0
M_LO, M_HI = 5.0, 80.0


def load_ticks(ph):
    return [json.loads(l) for l in (DB / ph / f"trace_{ph}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def xclass(x):
    if x is None:
        return "unk"
    ax = abs(x)
    return "same" if ax < 0.5 else ("adj" if ax < 1.5 else "far")


def audit(gname, phases, attribution=True):
    ticks = []
    for _, ph in phases:
        ticks += load_ticks(ph)
    base = ticks[0]["ts_ns"]
    pass_ts = sorted(d["ts_ns"] for d in ticks for _ in (d.get("passes") or []))
    gaps = [(pass_ts[i], pass_ts[i + 1]) for i in range(len(pass_ts) - 1)
            if (pass_ts[i + 1] - pass_ts[i]) / 1e9 > GAP_MIN]

    # 预计算每拍：dist m 差分闭合（同 id，跨 ≤3 拍）
    m_hist = {}          # id -> (ts, m)
    closing_of = {}      # (tick_idx, id) -> closing m/s
    for i, d in enumerate(ticks):
        for e in (d.get("dist") or []):
            mid, m = e.get("id"), e.get("m")
            if mid is None or m is None:
                continue
            if mid in m_hist:
                pt, pm = m_hist[mid]
                dt = (d["ts_ns"] - pt) / 1e9
                if 0 < dt <= 0.4:
                    closing_of[(i, mid)] = (pm - m) / dt
            m_hist[mid] = (d["ts_ns"], m)

    print(f"\n== {gname} 长空窗 {len(gaps)} 个 ==")
    tot = dict(over=0, coin=0, far=0, none=0, ticks=0)
    for gi, (g0, g1) in enumerate(gaps):
        seg = [(i, d) for i, d in enumerate(ticks) if g0 <= d["ts_ns"] <= g1]
        c = dict(over=0, coin=0, far=0, none=0, unk=0)
        states = {}
        for i, d in seg:
            states[d.get("state")] = states.get(d.get("state"), 0) + 1
            xs = {c["id"]: c.get("x") for c in (d.get("car_pos") or [])}
            best = None
            for e in (d.get("dist") or []):
                m = e.get("m")
                if m is None or not (M_LO <= m <= M_HI):
                    continue
                cl = closing_of.get((i, e["id"]))
                xc = xclass(xs.get(e["id"]))
                if xc in ("same", "adj") and cl is not None and cl > 1.0:
                    best = "over" if best != "over" else best
                elif xc in ("same", "adj"):
                    best = best or "near"
            if best == "over":
                c["over"] += 1
            elif best == "near":
                c["near"] = c.get("near", 0) + 1
            elif (d.get("n_groups") or 0) > 0:
                c["coin"] += 1
            elif any(e.get("m") is not None and M_LO <= e["m"] <= M_HI
                     for e in (d.get("dist") or [])):
                c["far"] += 1
            else:
                c["none"] += 1
        n = len(seg)
        tot["over"] += c["over"]; tot["coin"] += c["coin"]
        tot["far"] += c["far"]; tot["none"] += c["none"]; tot["ticks"] += n
        print(f"  空窗{gi} t={(g0-base)/1e9:6.1f}→{(g1-base)/1e9:6.1f}s ({(g1-g0)/1e9:4.1f}s) "
              f"可超={c['over']:3d} 邻近慢闭={c.get('near',0):3d} 有币={c['coin']:3d} "
              f"仅远车={c['far']:3d} 无目标={c['none']:3d}  状态={ {k: v for k, v in sorted(states.items(), key=lambda kv: -kv[1])} }")
    n = max(1, tot["ticks"])
    print(f"  小计：可超车拍 {tot['over']}/{n}（{100*tot['over']/n:.0f}%）  纯币拍 {100*tot['coin']/n:.0f}%  "
          f"无目标拍 {100*tot['none']/n:.0f}%")

    if not attribution:
        return
    # ---- 程序局逐次归因（lookback 3s：瞄准被超车 / 瞄准币 / 其他） ----
    hunted = coin_t = other = 0
    for pt in pass_ts:
        look = [(i, d) for i, d in enumerate(ticks) if 0 <= (pt - d["ts_ns"]) / 1e9 <= 3.0]
        if not look:
            other += 1
            continue
        pre_i, pre = look[-1]
        cand = [(e["id"], e["m"]) for e in (pre.get("dist") or []) if e.get("m") is not None]
        if not cand:
            other += 1
            continue
        cid, _ = min(cand, key=lambda t: t[1])
        cx = next((c.get("x") for c in (pre.get("car_pos") or []) if c["id"] == cid), None)
        if cx is None:
            other += 1
            continue
        if any(d.get("best_x") is not None and abs(d["best_x"] - cx) <= 0.6 for _, d in look):
            hunted += 1
        elif any(any(cc is not None and d.get("best_x") is not None
                     and abs(d["best_x"] - cc) <= 0.6
                     for cc in (d.get("cr_coins") or [])) for _, d in look):
            coin_t += 1
        else:
            other += 1
    print(f"  归因（{len(pass_ts)} 次通过）：瞄准被超车={hunted}  瞄准币={coin_t}  其他/证据不足={other}")


audit("程序A", GAMES["程序A"])
audit("程序B", GAMES["程序B"])
audit("人工(影子)", HUMAN, attribution=False)
