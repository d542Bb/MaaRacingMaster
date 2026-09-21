"""§七.3 回放一致率基线：决策输出 vs 人驾 pads 的原始分布（先出数，后定阈值）。

**定位**（设计稿 §七.3 + 验收分级）：这不是"判 V1 及格"——阈值要基线出来后由
维护者定。本探针回答两问：
① 人驾变道的时刻，机器在干什么（状态/reason/被哪道门拦下）——
   区分"参数太紧"与"材料里没有可换的币"；
② 真空街段的时长分布——target_empty 合取的 t_empty_s 定档数据
   （step 4 冒烟已暴露 1.5s 起值在真人局误报）。

**输入**：`replay_chain.jsonl`（probe_replay_chain 产物，决策逐帧）+ 各场
pads.jsonl（人驾 lx）。人驾变道事件 = |lx| 上升沿 ≥20000 且持舵 ≥0.25s
（与 probe_tau_steer 同一事件定义，口径不另起）。

**对齐**：±150ms 窗（§〇验收 1 的口径）——人事件时刻 t 取机器 [t−150ms, t+150ms]
的帧集。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_agreement.py [--only 20260921_17]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

_DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
CHAIN = _DATA / "replay_chain.jsonl"
DEMOS = _DATA / "demos"
RISE_HI, RISE_LO, HOLD_MIN_S = 20000, 3000, 0.25
ALIGN_S = 0.150


def human_events(sess_dir: Path) -> list[tuple[float, str]]:
    pads = [json.loads(x) for x in (sess_dir / "pads.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()]
    if not pads:
        return []
    t0 = pads[0]["ts_ns"]
    evs, state, t_rise, dirn = [], 0, 0.0, ""
    for x in pads:
        v, t = x["lx"], (x["ts_ns"] - t0) / 1e9
        if state == 0 and abs(v) > RISE_HI:
            state, t_rise, dirn = 1, t, ("L" if v < 0 else "R")
        elif state == 1 and abs(v) < RISE_LO:
            if t - t_rise >= HOLD_MIN_S:
                evs.append((t_rise, dirn))
            state = 0
    return evs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    by_sess: dict[str, list[dict]] = defaultdict(list)
    with open(CHAIN, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if not a.only or a.only in r["sess"]:
                by_sess[r["sess"]].append(r)

    print(f"{'session':>20s} {'帧':>5s} {'人事件':>5s} {'机器同窗在动':>7s} "
          f"{'被门拦':>6s} {'看不到候选':>6s} {'保守态':>5s} {'空街段P50/P90(max)':>18s}")
    tot = Counter()
    empty_spans: list[float] = []
    for sess, rows in sorted(by_sess.items()):
        rows.sort(key=lambda r: r["ts_ns"])
        t0 = rows[0]["ts_ns"]
        sess_dir = DEMOS / sess
        evs = human_events(sess_dir) if (sess_dir / "pads.jsonl").exists() else []
        # 对齐索引：人事件 → 窗内机器帧
        ts_list = [(r["ts_ns"] - t0) / 1e9 for r in rows]
        import bisect
        cat = Counter()
        for t, dirn in evs:
            lo = bisect.bisect_left(ts_list, t - ALIGN_S)
            hi = bisect.bisect_right(ts_list, t + ALIGN_S)
            window = rows[lo:hi]
            if not window:
                cat["无帧"] += 1
                continue
            states = {w["decision"]["state"] for w in window}
            if "CHANGE" in states or any("select" in w["decision"]["reason"] for w in window):
                cat["在动"] += 1
            elif any(w["decision"]["state"] == "CONSERVE" for w in window):
                cat["保守态"] += 1
            else:
                # hold 细分：看当时机器视野里有什么
                best = max((g for w in window for g in w["groups"]
                            if g["observed_count"] >= 1 and g["conf_min"] > 0),
                           key=lambda g: g["observed_count"], default=None)
                if best is None:
                    near_any = any(w["near"] > 0 or w["far"] > 0 for w in window)
                    cat["看不到候选" if not near_any else "有近物无组"] += 1
                else:
                    cat["有组但不过门"] += 1
        # 空街段：连续 presence=False 的时长（按 dt 累计，非致命语境）
        run = 0.0
        for r in rows:
            if not r["presence"]:
                run += r["dt_s"]
            else:
                if run > 0:
                    empty_spans.append(run)
                run = 0.0
        if run > 0:
            empty_spans.append(run)
        srt = sorted(empty_spans)
        ep = (f"{srt[len(srt)//2]:.1f}/{srt[int(.9*len(srt))]:.1f}({srt[-1]:.1f})"
              if srt else "—")
        print(f"{sess:>20s} {len(rows):>5d} {len(evs):>5d} {cat['在动']:>7d} "
              f"{cat['有组但不过门'] + cat['有近物无组']:>6d} {cat['看不到候选']:>6d} "
              f"{cat['保守态']:>5d} {ep:>18s}")
        tot.update(cat)
        tot["frames"] += len(rows)
        tot["events"] += len(evs)
    print(f"\n合计：人事件 {tot['events']} | 窗内机器 在动 {tot['在动']} / "
          f"被门拦 {tot['有组但不过门'] + tot['有近物无组']} / "
          f"视野无候选 {tot['看不到候选']} / 保守态 {tot['保守态']} / 无帧 {tot['无帧']}")
    if empty_spans:
        srt = sorted(empty_spans)
        over15 = sum(1 for x in srt if x >= 1.5)
        print(f"空街段 n={len(srt)}：P50={srt[len(srt)//2]:.1f}s "
              f"P75={srt[int(.75*len(srt))]:.1f}s P90={srt[int(.9*len(srt))]:.1f}s "
              f"max={srt[-1]:.1f}s；≥1.5s 的 {over15} 段"
              f"（t_empty_s=1.5 会误报这么多段）")


if __name__ == "__main__":
    main()
