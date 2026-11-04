# -*- coding: utf-8 -*-
"""离线影子：粗层占用窗 24 / 36 / 45 米的对照（撞车复盘实验 3）。

只读控制 trace，不 import 生产代码、不发控制指令。单变量：占用窗长度。
“占用资格”用米制车状态重算：车在窗口内（纵深 − 车头基准 ≤ 窗）且横向落在
碰撞包络内（|x| ≤ 2.3 米 = 自车半宽 + 车半宽 + 余量），即视为该窗能看到的
潜在占用。窗口越大，远处车越早获得占用资格。

冲突车判定：整条轨迹里曾经与自车并排（0 ≤ 纵深 ≤ 4 米）且横向在包络内。
非冲突车＝进入过窗口但从未并排贴近——它们代表“提前消费”的代价面。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

DATUM_M = 5.0       # 车头基准（与粗层纵深门同源）
ENVELOPE_M = 2.3    # 自车半宽 + 车半宽 + 碰撞余量
PERSIST = 2         # 连续拍数才算进入


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def _series(rows):
    """逐车时间序列：{id: [(拍, t, z, x, v, t_ov)]}。"""
    t = [0.0]
    for r in rows[1:]:
        t.append(t[-1] + (r.get("dt") or 0.05))
    out = {}
    for i, r in enumerate(rows):
        for m in r.get("mcars") or []:
            if m.get("z") is None or m.get("x") is None:
                continue
            out.setdefault(m["id"], []).append(
                (i, t[i], m["z"], m["x"], m.get("v"), m.get("t_ov")))
    return out


def _conflict(cid, series, pass_ids):
    """近距遭遇：该车有 pass 事件，或曾经在 18 米内且横向在包络内。"""
    if cid in pass_ids:
        return True
    return any(z <= 18.0 and abs(x) <= ENVELOPE_M for _i, _t, z, x, _v, _o in series)


def _first_entry(series, window_m):
    run = 0
    for i, tt, z, x, v, ov in series:
        if z - DATUM_M <= window_m and abs(x) <= ENVELOPE_M:
            run += 1
            if run >= PERSIST:
                return (i, tt, z, x, v, ov)
        else:
            run = 0
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--windows", default="24,36,45")
    ap.add_argument("--window-fids", default=None, help="打印事故窗口，如 1520-1570")
    ap.add_argument("--focus", type=int, default=None, help="打印指定车 id 的逐拍状态")
    args = ap.parse_args()
    wins = [float(w) for w in args.windows.split(",")]
    wf = None
    if args.window_fids:
        a, b = args.window_fids.split("-")
        wf = (int(a), int(b))
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        rows = [json.loads(line) for line in open(path, encoding="utf-8")]
        series = _series(rows)
        pass_ids = {p.get("id") for r in rows for p in (r.get("passes") or [])}
        conflict_ids = [cid for cid, s in series.items()
                        if _conflict(cid, s, pass_ids)]
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        print(f"   车 {len(series)}  冲突车 {len(conflict_ids)}")
        entries_by_w = {W: {cid: _first_entry(series[cid], W) for cid in series}
                        for W in wins}
        for W in wins:
            entries = entries_by_w[W]
            entrants = [cid for cid, e in entries.items() if e]
            false_ent = [cid for cid in entrants if cid not in conflict_ids]
            chit = sum(1 for cid in conflict_ids if entries[cid])
            print(f"   窗 {W:.0f}m：进入 {len(entrants)} 车"
                  f"（冲突车 {chit}/{len(conflict_ids)}，非冲突 {len(false_ent)}）")
        # 冲突车的提前量：相对最小窗，首次进入时刻提前了多少秒
        base_w = wins[0]
        for W in wins[1:]:
            gains = []
            for cid in conflict_ids:
                a, b = entries_by_w[base_w][cid], entries_by_w[W][cid]
                if a and b:
                    gains.append(a[1] - b[1])
            if gains:
                gains.sort()
                med = gains[len(gains) // 2]
                pos = sum(1 for g in gains if g > 0.05)
                print(f"   {base_w:.0f}→{W:.0f}m：可比冲突车 {len(gains)}，"
                      f"提前中位 {med:.2f}s，明显提前(>0.05s) {pos} 车")
        # 冲突车逐车：各窗首次进入时刻与到站时间
        print("   冲突车明细（拍 / 纵深 / 到站时间）")
        for cid in conflict_ids:
            parts = []
            for W in wins:
                e = _first_entry(series[cid], W)
                if e is None:
                    parts.append(f"{W:.0f}m:—")
                else:
                    i, tt, z, x, v, ov = e
                    parts.append(f"{W:.0f}m:拍{i} z={z:.1f} "
                                 f"t_ov={None if ov is None else round(ov, 2)}")
            print(f"     car{cid}: " + " | ".join(parts))
        if args.focus is not None:
            print(f"   焦点车 car{args.focus} 逐拍：")
            for i, tt, z, x, v, ov in series.get(args.focus, []):
                print(f"     fid={rows[i]['fid']} z={z:.1f} x={x:+.2f} "
                      f"v={v} t_ov={None if ov is None else round(ov, 2)}")
        if wf:
            print("   窗口逐车状态（仅冲突车）")
            for cid in conflict_ids:
                for i, tt, z, x, v, ov in series[cid]:
                    fid = rows[i]["fid"]
                    if wf[0] <= fid <= wf[1]:
                        tags = [f"{W:.0f}m" for W in wins if z - DATUM_M <= W]
                        print(f"     car{cid} fid={fid} z={z:.1f} x={x:+.2f} "
                              f"t_ov={None if ov is None else round(ov,2)} "
                              f"窗内={','.join(tags) or '—'}")


if __name__ == "__main__":
    main()
