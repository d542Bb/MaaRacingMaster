# -*- coding: utf-8 -*-
"""离线影子：丢框保持的计时基准审计（撞车复盘实验 9）。

只读控制 trace，不 import 生产代码、不发控制指令。

tracking._distance_of 在「本拍丢框」分支用
    held_ms = 现在 − 锚点采集时刻
再与 track_lost_hold_ms=150 比。但该分支的意图是「丢框后短窗沿用」——
计时应按「距最后一次 2D 观测」算，不是按锚龄。锚龄在正常跟车时就能到
300~450ms（RESCALED 外推段），于是单拍丢框即判 UNKNOWN。

本探针统计每次「上一拍有 z、本拍丢框」的转换：
  - 锚龄（代码实际用的时钟）是否已超 150ms → 保持是否根本没机会触发；
  - 若改用「距上次 2D」计时，150/300/450ms 能桥接多少拍。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

HOLD_MS = 150.0


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def analyze(path: str):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    t = [0.0]
    for r in rows[1:]:
        t.append(t[-1] + (r.get("dt") or 0.05))
    ids = set()
    ticks = []
    for i, r in enumerate(rows):
        vis = {c["id"] for c in (r.get("car_h") or [])}
        z = {m["id"]: m.get("z") for m in (r.get("mcars") or [])}
        age = {m["id"]: m.get("age") for m in (r.get("mcars") or [])}
        why = {d["id"]: d.get("why") for d in (r.get("dist") or [])}
        ids |= set(z) | vis
        ticks.append({"t": t[i], "fid": r.get("fid"), "vis": vis,
                      "z": z, "age": age, "why": why})
    out = []
    for cid in ids:
        last_sight = None
        for k in range(1, len(ticks)):
            a, b = ticks[k - 1], ticks[k]
            if cid in b["vis"] or b["z"].get(cid) is not None:
                last_sight = b["t"]
            # 丢框转换：上一拍正常有 z，本拍状态机报 TRACK_LOST
            # （无论保持是否生效——HELD 与 UNKNOWN 都带这个原因）
            if a["z"].get(cid) is None or b["why"].get(cid) != "TRACK_LOST":
                continue
            age = b["age"].get(cid)
            kept = b["z"].get(cid) is not None
            since = None if last_sight is None else (b["t"] - last_sight) * 1000
            nxt = next((j for j in range(k, len(ticks))
                        if ticks[j]["z"].get(cid) is not None), None)
            blind = (None if nxt is None else
                     ticks[nxt]["t"] - b["t"])
            out.append({"cid": cid, "fid": b["fid"], "t": b["t"],
                        "age": age, "since": since, "blind": blind,
                        "kept": kept})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    args = ap.parse_args()
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        ev = analyze(path)
        held = [e for e in ev if e["kept"]]
        failed = [e for e in ev if not e["kept"]]
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        print(f"   丢框转换 {len(ev)}：保持生效 {len(held)}，"
              f"保持未生效 {len(failed)}（无法保持占比 "
              f"{len(failed)*100/(len(ev) or 1):.0f}%）")
        ages = [e["age"] for e in ev if e["age"] is not None]
        ages_f = [e["age"] for e in failed if e["age"] is not None]
        if ages:
            print(f"   丢框时锚龄：全部 中位 {_pct(ages,.5):.0f}ms P90 {_pct(ages,.9):.0f}ms")
        if ages_f:
            print(f"              未保持 中位 {_pct(ages_f,.5):.0f}ms "
                  f"P90 {_pct(ages_f,.9):.0f}ms")
        for e in sorted(failed, key=lambda x: -(x["age"] or 0))[:12]:
            b = "—" if e["blind"] is None else f"{e['blind']:.2f}s"
            print(f"   car{e['cid']} fid{e['fid']} t={e['t']:.2f} "
                  f"锚龄 {e['age']}ms 盲区 {b}")


if __name__ == "__main__":
    main()
