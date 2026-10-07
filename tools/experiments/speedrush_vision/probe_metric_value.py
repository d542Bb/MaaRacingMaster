# -*- coding: utf-8 -*-
"""米制决策价值第一轮探针（stdout-only 零文件写）。

问题（刀二收窄后的唯一问题）：metric_distance_m 对驾驶决策到底有没有
像素以外的增量价值？三个检验：

  R1 冗余度——r(metric, h) 与 r(metric, cy)：若 |r|≈1，米制只是像素换皮，
     无增量；实测 -0.56（三局 1110 拍），深度带独立尺度信息（车物理尺寸
     歧义被打破），必要条件成立。
  R2 覆盖——决策拍（超车段）上米制读数的可得率：实测 47%（9/19 段），
     是近期任何米制消费的硬约束。
  R3 结局区分——按段终止原因（cancel 细分/done）分组的段内米读数分布；
     实测 18/19 段 cancel（结局方差近零），当前样本答不了，pass 带
     track_id（45cd7d1）后逐车归因再判。

用法：.venv python probe_metric_value.py <trace.jsonl> [...]
（car_pos 缺失的旧 trace 自动少 cy 维；passes 列新旧 schema 均兼容。）
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        beats = [json.loads(l) for l in f if l.strip()]
    beats.sort(key=lambda b: b["fid"])
    return beats


def corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def med(xs: list[float]) -> str:
    xs = sorted(x for x in xs if x is not None)
    return f"n={len(xs)} p50={xs[len(xs)//2]:.1f}" if xs else "n=0"


def pass_dmins(b: dict) -> list[tuple[int, float]]:
    """兼容新旧 schema：新 [{id,d_min}] / 旧 [float]（旧无 id 记 -1）。"""
    out = []
    for e in b.get("passes") or []:
        if isinstance(e, dict):
            out.append((e["id"], e["d_min"]))
        else:
            out.append((-1, float(e)))
    return out


def main() -> None:
    all_pairs: list[tuple[float, int, float | None]] = []
    for path in sys.argv[1:]:
        beats = load(path)
        segs: list[tuple[str, list[float], list[float], int | None, float | None]] = []
        i = 0
        while i < len(beats):
            if beats[i].get("state") == "CHANGE":
                j = i
                while j < len(beats) and beats[j].get("state") == "CHANGE":
                    j += 1
                seg = beats[i:j]
                end = (beats[j].get("reason") or "?").split(":")[0] if j < len(beats) else "尾"
                ms = [x["m"] for b in seg for x in b.get("dist") or []
                      if x["id"] == b.get("target_id") and x.get("m") is not None]
                hs = [e["h"] for b in seg for e in b.get("car_h") or []
                      if e["id"] == b.get("target_id")]
                tid = seg[-1].get("target_id")
                t0 = seg[-1]["ts_ns"]
                dmin = next((dm for b in beats[j:j + 100]
                             if (b["ts_ns"] - t0) / 1e9 <= 5.0
                             for k, dm in pass_dmins(b) if tid is None or k in (tid, -1)),
                            None)
                segs.append((end, ms, hs, tid, dmin))
                i = j
            else:
                i += 1
        for b in beats:
            hh = {e["id"]: e["h"] for e in b.get("car_h") or []}
            pp = {e["id"]: e for e in b.get("car_pos") or []}
            for x in b.get("dist") or []:
                if x.get("m") is None or x["id"] not in hh:
                    continue
                all_pairs.append((x["m"], hh[x["id"]],
                                  pp.get(x["id"], {}).get("cy")))

        print(f"\n=== {os.path.basename(path)} ===")
        print(f"超车 CHANGE 段 n={len(segs)}，终止原因 {dict(Counter(s[0] for s in segs))}")
        print(f"米制段内覆盖: {sum(1 for s in segs if s[1])}/{len(segs)} 段")
        for out in sorted(set(s[0] for s in segs)):
            g = [s for s in segs if s[0] == out]
            print(f"  {out:10s} 段内米 {med([m for s in g for m in s[1]])}  "
                  f"段内框高 {med([h for s in g for h in s[2]])}  "
                  f"5s内pass dmin {med([s[4] for s in g])}")

    ms = [p[0] for p in all_pairs]
    hs = [p[1] for p in all_pairs]
    sub = [(m, c) for m, h, c in all_pairs if c is not None]
    print(f"\n[R1 冗余度] 米-像素同拍 n={len(all_pairs)}（三局合并）")
    print(f"  r(metric, h)  = {corr(ms, hs) and round(corr(ms, hs), 3)}")
    print(f"  r(metric, cy) = {corr(*zip(*sub)) and round(corr(*zip(*sub)), 3)}")


if __name__ == "__main__":
    main()
