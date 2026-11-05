# -*- coding: utf-8 -*-
"""离线台架：四项待验证问题的量化（第 7 期/±2 之后的遗留口子）。

- A. 资格闭锁缺口：逐身份 slack 序列的丢检间隔分布；请求保持（0.2s 冻结）
  与资格闭锁（0.3s 保留）各能覆盖多少，(0.2,0.3] 区间的缺口有多少次。
- B. 重锚清证据：anchor_follow/epoch_reset 事件前 3s 内同侧请求的累计
  证据，事件后被清掉的量；若"换基准不清零"，多少次能直接攒满确认。
- C. 并线盲窗：merging 拍里相邻道光锥可达的币（按契约不发请求），并线
  结束后这枚币还在不在（错过=并线期间流失）。
- D. 路心跳变：单拍 >0.4 道的跳变，按"换源/没换源、同拍自车横移"分类——
  参考系在跳还是车在动。

用法::

    python tools/experiments/world_state/bench_verify.py \
        --session 20261102_091045 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402


def _path(session: str) -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def _pct(v):
    if not v:
        return None
    s = sorted(v)
    q = lambda p: round(s[int((len(s) - 1) * p)], 3)  # noqa: E731
    return {"n": len(s), "p50": q(.5), "p90": q(.9), "max": s[-1]}


def _split_games(rows):
    games, prev = [[rows[0]]], None
    for r in rows[1:]:
        s = r.get("stage_id")
        if prev is not None and s is not None and s < prev:
            games.append([])
        games[-1].append(r)
        if s is not None:
            prev = s
    return games


def analyze_game(gi, rs):
    ts = [r["ts_ns"] / 1e9 for r in rs]
    dts = [0.0] + [max(0.0, min(0.2, b - a)) for a, b in zip(ts, ts[1:])]

    # ---- A. 逐身份丢检间隔 ----
    seq = defaultdict(list)          # oid -> [(i, slack)]
    for i, r in enumerate(rs):
        opp = (r.get("sel_dbg") or {}).get("opp") or {}
        for kind in ("rw", "coin"):
            for e in (opp.get(kind) or []):
                if isinstance(e, (list, tuple)) and len(e) >= 2 \
                        and isinstance(e[0], (int, float)):
                    seq[e[0]].append((i, e[1]))
    gaps = []
    gaps_02_03 = 0
    for oid, s_ in seq.items():
        for (i1, _), (i2, _) in zip(s_, s_[1:]):
            g = ts[i2] - ts[i1]
            if g > 0.02:
                gaps.append(g)
                if 0.2 < g <= 0.3:
                    gaps_02_03 += 1
    over02 = sum(1 for g in gaps if g > 0.2)
    over03 = sum(1 for g in gaps if g > 0.3)

    # ---- B. 重锚清证据 ----
    req_hist = []                    # (i, side)
    for i, r in enumerate(rs):
        q = (r.get("sel_dbg") or {}).get("req")
        if q is not None:
            req_hist.append((i, 1 if q > 0 else -1))
    reanchor = {"credit_lost_s": 0.0, "events_with_credit": 0,
                "events": 0, "would_confirm": 0}
    for i, r in enumerate(rs):
        rt = r.get("route") or {}
        if rt.get("vd") not in ("anchor_follow", "epoch_reset"):
            continue
        reanchor["events"] += 1
        # 事件前 3s 内同侧请求的累计证据（相邻请求拍 ≤0.2s 视为同一次意图）
        side = None
        credit = 0.0
        for j in range(len(req_hist) - 1, -1, -1):
            ij, sj = req_hist[j]
            if ts[i] - ts[ij] > 3.0:
                break
            if side is None:
                side = sj
                credit = dts[ij]
            elif sj == side and ts[ij + 1] - ts[ij] <= 0.22:
                credit += dts[ij]
            else:
                break
        if credit > 0.0:
            reanchor["events_with_credit"] += 1
            reanchor["credit_lost_s"] += credit
            if credit >= 0.25:
                reanchor["would_confirm"] += 1

    # ---- C. 并线盲窗 ----
    blind = {"ticks": 0, "coins_seen": 0, "reachable_adj": 0,
             "gone_after": 0}
    coin_pos = {}
    for i, r in enumerate(rs):
        rt = r.get("route") or {}
        if rt.get("ph") != "merging":
            continue
        blind["ticks"] += 1
        sd = r.get("sel_dbg") or {}
        opp = (sd.get("opp") or {}).get("coin") or []
        for (x, t), e in zip((sd.get("coin") or []), opp):
            if x is None:
                continue
            blind["coins_seen"] += 1
            slack = e[1] if isinstance(e, list) and len(e) >= 2 else None
            if t is None or not (0.0 < t <= 6.0) or slack is None or slack < 0:
                continue
            ref = rt.get("ref")
            if ref is None or round(x - ref) not in (-1, 1, -2, 2):
                continue
            blind["reachable_adj"] += 1
    # 并线结束后最近一次可见的币是否消失（粗查：并线段后 1.5s 内不再出现）
    merge_spans = []
    start = None
    for i, r in enumerate(rs):
        ph = (r.get("route") or {}).get("ph")
        if ph == "merging" and start is None:
            start = i
        elif ph != "merging" and start is not None:
            merge_spans.append((start, i))
            start = None
    if start is not None:
        merge_spans.append((start, len(rs)))
    for s0, s1 in merge_spans:
        sd = rs[s0].get("sel_dbg") or {}
        opp = (sd.get("opp") or {}).get("coin") or []
        ids = {e[0] for e in opp
               if isinstance(e, (list, tuple)) and len(e) >= 2}
        after = [j for j in range(s1, min(s1 + 30, len(rs)))
                 if (rs[j].get("route") or {}).get("ph") == "keep"]
        for j in after[:10]:
            oj = ((rs[j].get("sel_dbg") or {}).get("opp") or {}).get("coin") \
                or []
            ids &= {e[0] for e in oj
                    if isinstance(e, (list, tuple)) and len(e) >= 2} | ids
        still = {e[0] for e in (((rs[s1].get("sel_dbg") or {}).get("opp")
                 or {}).get("coin") or [])
                 if isinstance(e, (list, tuple)) and len(e) >= 2}
        blind["gone_after"] += len(ids - still)

    # ---- D. 路心跳变 ----
    hops = {"src_chg": 0, "no_src": 0, "with_exec_move": 0, "n": 0,
            "mags": []}
    for i, r in enumerate(rs):
        h = r.get("ro_hop") or 0.0
        if h <= 0.4:
            continue
        hops["n"] += 1
        hops["mags"].append(h)
        if r.get("ro_src_chg"):
            hops["src_chg"] += 1
        else:
            hops["no_src"] += 1
        # 同拍自车横移（executed 变化 >0.15 道 = 车真的在动）
        if i and rs[i - 1].get("executed_lane") is not None \
                and r.get("executed_lane") is not None \
                and abs(r["executed_lane"] - rs[i - 1]["executed_lane"]) > 0.15:
            hops["with_exec_move"] += 1

    return {"game": gi, "ticks": len(rs),
            "A_dropout_gaps": _pct(gaps), "A_gaps_over02": over02,
            "A_gaps_over03": over03, "A_gaps_02_03": gaps_02_03,
            "B_reanchor": reanchor,
            "C_merge_blind": blind,
            "D_lane_hops": {**hops, "mags": _pct(hops["mags"])}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261102_091045")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rows = list(iter_rows(_path(args.session)))
    rep = {"session": args.session,
           "games": [analyze_game(gi, rs)
                     for gi, rs in enumerate(_split_games(rows), 1)]}
    if args.json:
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    for g in rep["games"]:
        print(f"== 局{g['game']}  {g['ticks']} 拍")
        print(f"  A 丢检间隔 {g['A_dropout_gaps']}")
        print(f"    >0.2s（请求保持覆盖外）{g['A_gaps_over02']} 次、"
              f">0.3s（闭锁也盖不住）{g['A_gaps_over03']} 次、"
              f"(0.2,0.3] {g['A_gaps_02_03']} 次")
        print(f"  B 重锚/换纪 {g['B_reanchor']['events']} 次，带证据 "
              f"{g['B_reanchor']['events_with_credit']} 次，清掉 "
              f"{g['B_reanchor']['credit_lost_s']:.2f}s，够确认门槛 "
              f"{g['B_reanchor']['would_confirm']} 次")
        print(f"  C 并线盲窗 {g['C_merge_blind']['ticks']} 拍，可见币 "
              f"{g['C_merge_blind']['coins_seen']}，相邻道可达 "
              f"{g['C_merge_blind']['reachable_adj']}，并完即失 "
              f"{g['C_merge_blind']['gone_after']}")
        d = g["D_lane_hops"]
        print(f"  D 路心跳变 {d['n']} 次（换源 {d['src_chg']} / 没换源 "
              f"{d['no_src']} / 同拍自车真动 {d['with_exec_move']}），"
              f"幅度 {d['mags']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
