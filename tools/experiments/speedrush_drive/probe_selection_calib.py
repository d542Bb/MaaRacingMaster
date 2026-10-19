# -*- coding: utf-8 -*-
"""录制模式选择层对照（2026-10-18 走廊重建）。

读一局 trace（录制模式=人驾、决策链影子），回答三件事：

1. 选择层选了什么、持续多久、有没有来回翻面；
2. 它看到的金币/车/奖励车，和人工实际横位差多少（人工去没去）；
3. 人工自己变 0.5/1 道要多久——给 ``selection.t_move`` 一个真实参照。

用法：
    .venv\\Scripts\\python.exe tools/experiments/speedrush_drive/probe_selection_calib.py <trace.jsonl>

只读，不依赖实验区之外的包。输出为文本；需要逐拍表加 ``--rows``。
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter


def _q(v, p=0.5):
    if not v:
        return None
    s = sorted(v)
    return round(s[min(len(s) - 1, int(len(s) * p))], 3)


def _runs(seq):
    out, cur, n = [], object(), 0
    for x in seq:
        if x == cur:
            n += 1
        else:
            if n:
                out.append((cur, n))
            cur, n = x, 1
    if n:
        out.append((cur, n))
    return out


def _human_moves(executed, fids):
    """人工横移段：方向一致的连续 executed 变化；输出 0.5/1 道用时估计。

    录制模式里 executed 是规划层对人工横位的连续重锚，采样率约 20Hz。
    只取单段位移 ≥0.15 道、持续 ≤3s 的段；0.5/1 道按段内平均速度折算。
    """
    out = {"n_segments": 0, "v_peak": [], "v_mean": [], "t_half": [],
           "t_one": []}
    i = 0
    while i < len(executed) - 1:
        d = executed[i + 1] - executed[i]
        if abs(d) < 1e-4:
            i += 1
            continue
        sign = 1 if d > 0 else -1
        j = i + 1
        start = executed[i]
        while j + 1 < len(executed):
            nd = executed[j + 1] - executed[j]
            if nd * sign <= 0:
                break
            j += 1
        span = abs(executed[j] - start)
        dur = (fids[j] - fids[i]) * 0.05
        if 0.15 <= span and 0 < dur <= 3.0:
            out["n_segments"] += 1
            v = span / dur
            out["v_mean"].append(v)
            out["v_peak"].append(max(
                abs(executed[k + 1] - executed[k]) / 0.05
                for k in range(i, j)))
            out["t_half"].append(0.5 / v)
            out["t_one"].append(1.0 / v)
        i = max(i + 1, j)
    return {k: (_q(v, 0.5), _q(v, 0.9), len(v)) if isinstance(v, list) else v
            for k, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--rows", action="store_true", help="打印逐拍选择行")
    args = ap.parse_args()
    rows = []
    with open(args.trace, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                if isinstance(r.get("fid"), int):
                    rows.append(r)
    if not rows:
        print("无有效 trace 行")
        return
    print(f"rows={len(rows)} fid={rows[0]['fid']}..{rows[-1]['fid']}")
    print("layer", Counter(r.get("sel_why") for r in rows).most_common())
    rr = _runs([r.get("sel_why") for r in rows])
    print("layer runs", len(rr),
          "len_p50/p90", _q([n for _, n in rr]), _q([n for _, n in rr], 0.9),
          "max", max(n for _, n in rr))
    print("layer flips/s", round(
        sum(1 for a, b in zip(rr, rr[1:]) if a[0] != b[0])
        / max(1e-9, (rows[-1]["fid"] - rows[0]["fid"]) * 0.05), 2))

    dbg = [r for r in rows if r.get("sel_dbg")]
    print("sel_dbg rows", len(dbg))
    if dbg:
        band = dbg[0]["sel_dbg"].get("band", 0.5)
        # 中心带：选择层自己认为在带内时，目标是否还在动
        inside, held, moved = 0, 0, 0
        for r in dbg:
            d = r["sel_dbg"]
            ego, c8, tgt = d.get("ego"), d.get("c8"), d.get("target")
            if None in (ego, c8, tgt):
                continue
            if abs(ego - c8) <= band:
                inside += 1
                if abs(tgt - ego) <= 0.05:
                    held += 1
                elif abs(tgt - ego) > 0.1:
                    moved += 1
        print(f"center: inside={inside} held={held} moved>0.1={moved}"
              f" ({100*moved/max(1,inside):.1f}%)")
        # 机会：目标 t 时刻人工横位与目标差
        for key, label in (("coin", "coin"), ("car", "car"), ("rw", "reward")):
            diffs, cnt = [], 0
            for r in dbg:
                d = r["sel_dbg"]
                if r.get("sel_why") != label:
                    continue
                tgt, ego = d.get("target"), d.get("ego")
                if tgt is None or ego is None:
                    continue
                diffs.append(abs(tgt - ego))
                cnt += 1
            if cnt:
                print(f"{label}: selected={cnt} |target-human_now|"
                      f" p50/p90={_q(diffs)}/{_q(diffs,0.9)}")
        # 人工变道用时（selection.t_move 的真实参照）
        fids = [r["fid"] for r in rows if isinstance(r.get("executed_lane"), (int, float))]
        ex = [r["executed_lane"] for r in rows if isinstance(r.get("executed_lane"), (int, float))]
        if fids:
            hm = _human_moves(ex, fids)
            print("human moves", hm)
    # 墙线/供数
    print("wlc health", Counter(r.get("wlc_health") for r in rows).most_common())
    print("ro source", Counter(r.get("ro_source") for r in rows).most_common())
    rates = [r["hud_rate"] for r in rows if isinstance(r.get("hud_rate"), (int, float))]
    if rates:
        print("hud rate p50/p90/max", _q(rates), _q(rates, 0.9), max(rates))
    if args.rows:
        for r in rows:
            d = r.get("sel_dbg") or {}
            print(r.get("fid"), r.get("sel_why"), r.get("x_target"),
                  r.get("executed_lane"), d.get("ego"), d.get("c0"),
                  d.get("c8"), d.get("target"), r.get("wlc_health"))


if __name__ == "__main__":
    main()
