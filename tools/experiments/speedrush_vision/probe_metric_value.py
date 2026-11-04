# -*- coding: utf-8 -*-
"""米制决策价值第一轮探针（stdout-only 零文件写）。

问题（刀二收窄后的唯一问题）：metric_distance_m 对驾驶决策到底有没有
像素以外的增量价值？三个检验：

  R1 冗余度——r(metric, h) 与 r(metric, cy)：若 |r|≈1，米制只是像素换皮，
     无增量；实测 -0.56（三局 1110 拍），深度带独立尺度信息（车物理尺寸
     歧义被打破），必要条件成立。
  R2 覆盖——决策拍（超车段）上米制读数的可得率：实测 47%（9/19 段），
     是近期任何米制消费的硬约束。
  R3 结局区分——逐车归因表（pass 事件 ↔ 决策锁定车）+ 贴身/宽裕 AUC +
     Spearman，米 vs 框高双窗（0.5s/1s）对照；判决规则预登记：米须两窗
     都超过框高才算胜出。贴身阈值 d_min<0.8 道，米读数限 2s 陈旧度。

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


LOOKBACKS_S = (0.5, 1.0)   # pass 前固定回看档（极速下 1s≈45m 外，0.5s≈25m）
STALE_S = 2.0              # 米读数陈旧度上限（超过视为无读数，防远古值冒充）


def nearest_in_window(seq: list[tuple[int, float]], lo: int, hi: int) -> float | None:
    """seq=[(fid, 值)] 按 fid 升序；取落在 [lo, hi] 内的最新值，无则 None。"""
    vals = [v for f, v in seq if lo <= f <= hi]
    return vals[-1] if vals else None


def attribution(beats: list[dict]) -> list[dict]:
    """逐车归因：每个 pass 事件（带 id）向前找决策锁定拍（target_id==id），
    取 pass 前 0.5s/1s 窗口内最新读数作同刻预测子（米读数限 2s 陈旧度）。
    done 段终止即 outcome=done。"""
    done_ids: set[int] = set()
    i = 0
    while i < len(beats):
        if beats[i].get("state") == "CHANGE":
            j = i
            while j < len(beats) and beats[j].get("state") == "CHANGE":
                j += 1
            end = (beats[j].get("reason") or "?") if j < len(beats) else ""
            if end.startswith("done") and j - 1 >= i:
                tid = beats[j - 1].get("target_id")
                if tid is not None:
                    done_ids.add(tid)
            i = j
        else:
            i += 1
    ms_all = [(b["fid"], x["id"], x["m"]) for b in beats for x in b.get("dist") or []
              if x.get("m") is not None]
    h_all = [(b["fid"], e["id"], e["h"]) for b in beats for e in b.get("car_h") or []]
    # 实测 fid 率（帧率≠拍率）：时长秒数上分摊的 fid 数
    dur_s = (beats[-1]["ts_ns"] - beats[0]["ts_ns"]) / 1e9
    fid_rate = (beats[-1]["fid"] - beats[0]["fid"]) / dur_s if dur_s > 0 else 38.0
    stale_fids = STALE_S * fid_rate
    lock_by_id: dict[int, list[int]] = defaultdict(list)
    for b in beats:
        tid = b.get("target_id")
        if tid is not None:
            lock_by_id[tid].append(b["fid"])
    rows = []
    for b in beats:
        for pid, dm in pass_dmins(b):
            if pid < 0:
                continue
            fid_p = b["fid"]
            m_seq = sorted((f, m) for f, i_, m in ms_all if i_ == pid)
            h_seq = sorted((f, h) for f, i_, h in h_all if i_ == pid)
            fids = lock_by_id.get(pid) or []
            row = {"id": pid, "d_min": dm,
                   "done": pid in done_ids,
                   "locked": any(f < fid_p for f in fids)}
            for tag, lb_s in zip(("m_05", "h_05", "m_1s", "h_1s"),
                                 (0.5, 0.5, 1.0, 1.0)):
                seq = m_seq if tag.startswith("m") else h_seq
                lo = round(fid_p - lb_s * fid_rate - (stale_fids if tag.startswith("m") else 0))
                row[tag] = nearest_in_window(seq, lo, round(fid_p - lb_s * fid_rate))
            m_last = [m for f, m in m_seq if f <= fid_p]
            row["m_last"] = m_last[-1] if m_last else None
            rows.append(row)
    return rows


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
    print(f"\n[R1 冗余度] 米-像素同拍 n={len(all_pairs)}（全部局合并）")
    print(f"  r(metric, h)  = {corr(ms, hs) and round(corr(ms, hs), 3)}")
    print(f"  r(metric, cy) = {corr(*zip(*sub)) and round(corr(*zip(*sub)), 3)}")

    # 逐车归因（预登记判据的执行表）：只看被决策锁定的车——米制的可能
    # 增量只存在于锁定子集。预测子取 pass 前 0.5s/1s 窗口最新读数（米限
    # 2s 陈旧度），对照框高基线。**判决规则（预登记）**：米须在 0.5s 与
    # 1s 两档都超过框高才算胜出——单档胜出视为窗口伪影（极速下 1s≈45m
    # 外结局未定型，单窗结论不稳）。
    all_rows: list[dict] = []
    for path in sys.argv[1:]:
        all_rows.extend(attribution(load(path)))
    locked = [r for r in all_rows if r["locked"]]
    print(f"\n[R3 逐车归因] pass 事件 n={len(all_rows)}，被决策锁定 n={len(locked)}")
    print("  id   d_min outcome m_05   h_05  m_1s   h_1s  m_last")
    for r in locked:
        def f(v, s=".2f"):
            return f"{v:{s}}" if v is not None else "  --"
        out = "done" if r["done"] else "cancel"
        print(f"  {r['id']:4d} {r['d_min']:.3f} {out:6s} {f(r['m_05'], '.1f'):6s} "
              f"{f(r['h_05'], '.0f'):5s} {f(r['m_1s'], '.1f'):6s} "
              f"{f(r['h_1s'], '.0f'):5s} {f(r['m_last'], '.1f'):6s}")
    # 贴身/宽裕区分度：贴身 = d_min < 0.8（道，操作化阈值）。米读数方向
    # 「小=近」，框高方向「大=近」，AUC 按各自方向计算；另报免阈值的
    # Spearman ρ（预测子 vs d_min）作辅助。任一侧样本不足只出表不判。
    def spearman(pairs: list[tuple[float, float]]) -> float | None:
        n = len(pairs)
        if n < 3:
            return None
        rx = {v: r for r, v in enumerate(sorted(v for v, _ in pairs))}
        ry = {v: r for r, v in enumerate(sorted(v for _, v in pairs))}
        den = (n * (n * n - 1)) / 6.0
        return 1 - sum((rx[v] - ry[w]) ** 2 for v, w in pairs) / den if den else None

    for name in ("m_05", "h_05", "m_1s", "h_1s"):
        low_is_near = name.startswith("m")
        tight = [r[name] for r in locked if r["d_min"] < 0.8 and r[name] is not None]
        loose = [r[name] for r in locked if r["d_min"] >= 0.8 and r[name] is not None]
        if tight and loose:
            wins = sum(1 for t in tight for l in loose
                       if (t < l if low_is_near else t > l))
            auc = wins / (len(tight) * len(loose))
            print(f"  [{name}] 贴身 n={len(tight)} p50={sorted(tight)[len(tight)//2]:.2f}  "
                  f"宽裕 n={len(loose)} p50={sorted(loose)[len(loose)//2]:.2f}  "
                  f"P(区分正确)={auc:.2f}")
        else:
            print(f"  [{name}] 样本不足（贴身 n={len(tight)} 宽裕 n={len(loose)}）")
        pairs = [(r[name], r["d_min"]) for r in locked if r[name] is not None]
        rho = spearman(pairs)
        print(f"  [{name}] Spearman vs d_min n={len(pairs)}"
              + (f" ρ={rho:.2f}" if rho is not None else "（n<3）"))


if __name__ == "__main__":
    main()
