#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""speedrush 录制节拍探针：把"帧率"拆成三个数，并定位瓶颈在哪一侧。

**要回答什么**：录制出来的实际帧率是多少、**尾延迟**多长、以及**瓶颈在源、在写盘、还是在主循环**——
三者修法完全不同（换采集方式 / 修队列 / 改循环结构），所以必须判开而不是看平均 fps。

**判据（本探针的核心，来自一次实测定案 2026-09-18）**：只看平均 fps 会漏掉全部信息——
九场实测有效 15.3 fps，而帧间隔 P95 是 **200 ms**、且慢间隔**每 8 帧必来一次**。三个量各指一处：

| 量 | 含义 | 怎么读 |
|---|---|---|
| `dt` 分布 + **P95** | 有效观测能力 | P95 是"能不能采到一个短事件"的真判据，平均 fps 不是 |
| `frame_id` 增量率 | **源**产帧的速率 | `frame_id` 是 WGC 回调**接受**的帧号；若它推进 ≈40/s 而记录只有 15/s，则**丢在采集侧**（中间帧被"最新帧槽"覆盖，不进队列） |
| `age_ms` | **写盘/队列**是否积压 | 帧到回调时的新鲜度；若慢间隔处 age 变大 = 积压；若一样小 = 帧本来就新鲜，**无责** |
| `frames_dropped`（meta） | 队列是否真的满过 | 为 0 说明"入队但队满"这条路径没发生 |
| 「慢间隔间距」的众数 | **主循环**的哪个周期在拖 | 若众数 = N 帧、而 N×(1/fps) ≈ 某个循环周期常量，就是那个复查在热路径上 |

**实测基线（九场 / 5586 个帧间隔，`20260916_*`~`20260917_*`）**：15.1~15.3 fps；中位 49.9 ms；
P95 200 ms；100~300 ms 占 12.6%；慢间隔间距**众数 8 帧**（704 个里 688 个）＝
`DRIVE_ANCHOR_CHECK_S = 0.5` s；`frame_id` 速率 ≈40/s（正常 dt≈50 ms 增 2、慢间隔 dt≈200 ms 增 7~8）；
`age_ms` 中位 13.3 / 最大 33.2；`frames_dropped` 全 0；帧图 154 KB（九场中位；15/20/30 fps = 138/184/277 MB/分钟）。
**结论**：瓶颈是主循环里每次 0.5 s 的锚点复查（单次 130~250 ms），源与写盘均无责。详见
`maaracing_master/plugins/speedrush/CODE_WIKI.md` §2.1。

用法：
    python tools/experiments/speedrush_gating/probe_frame_pace.py            # 全部会话汇总
    python tools/experiments/speedrush_gating/probe_frame_pace.py --session 20260917_220750_p2
    python tools/experiments/speedrush_gating/probe_frame_pace.py --report   # 附带分歧会话明细
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import numpy as np

DEFAULT_DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
                 / "demos")
SLOW_MS = 100.0            # "慢间隔"阈值：超过一个标称 50 ms 周期两倍即算迟到


def load(sess: Path):
    rows = [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]
    meta_path = sess / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return rows, meta


def analyze(sess: Path) -> dict:
    rows, meta = load(sess)
    t = np.array([r["ts_ns"] for r in rows], dtype=float) / 1e9
    fid = np.array([r["frame_id"] for r in rows], dtype=float)
    age = np.array([r.get("age_ms", np.nan) for r in rows], dtype=float)
    dt = np.diff(t) * 1000.0
    did = np.diff(fid)
    slow = dt > SLOW_MS
    slow_idx = np.where(slow)[0]
    gaps = np.diff(slow_idx) if len(slow_idx) > 1 else np.array([0])
    # 源速率与"是否在丢源帧"：`frame_id` 增量 = 这段间隔里源**接受**了几帧。
    # 用**全量**间隔算——早先只在"慢间隔"上取中位，慢间隔一少样本就只剩几个、数会乱跳
    # （修复后慢间隔从 98 降到 5，该列立刻失真，故改为全量）。
    src_rate = float(np.mean(did) / np.mean(dt) * 1000.0) if len(did) else 0.0
    src_per_rec = float(np.mean(did)) if len(did) else 0.0
    return {
        "name": sess.name, "frames": len(rows), "span": t[-1] - t[0],
        "fps": len(rows) / max(t[-1] - t[0], 1e-9),
        "dt_med": float(np.median(dt)), "dt_p95": float(np.percentile(dt, 95)),
        "dt_max": float(dt.max()),
        "frac": {  # 各档占比
            "<=40": float((dt <= 40).mean()), "40-60": float(((dt > 40) & (dt <= 60)).mean()),
            "60-100": float(((dt > 60) & (dt <= 100)).mean()),
            "100-300": float(((dt > 100) & (dt <= 300)).mean()),
            ">300": float((dt > 300).mean()),
        },
        "slow_n": int(slow.sum()), "slow_gap_mode": (Counter(gaps.tolist()).most_common(1)[0]
                                                    if len(gaps) else (0, 0)),
        "src_rate": float(src_rate), "src_per_rec": float(src_per_rec),
        "age_med": float(np.nanmedian(age)), "age_max": float(np.nanmax(age)),
        "age_slow": float(np.nanmedian(age[1:][slow])) if slow.sum() else float("nan"),
        "age_fast": float(np.nanmedian(age[1:][~slow])) if (~slow).sum() else float("nan"),
        "dropped": meta.get("frames_dropped"), "jpeg_kb": None,
    }


def jpeg_size(sess: Path) -> float | None:
    files = sorted((sess / "frames").glob("*.jpg"))
    if not files:
        return None
    return float(np.median([f.stat().st_size for f in files])) / 1024.0


def report_one(r: dict) -> None:
    f = r["frac"]
    print(f"\n--- {r['name']} ---")
    print(f"  帧 {r['frames']}  跨度 {r['span']:.1f}s  有效 {r['fps']:.2f} fps")
    print(f"  dt 中位 {r['dt_med']:.1f} ms  **P95 {r['dt_p95']:.1f} ms**  最大 {r['dt_max']:.0f} ms")
    print(f"  分档 ≤40 {f['<=40']:.0%} · 40-60 {f['40-60']:.0%} · 60-100 {f['60-100']:.0%}"
          f" · **100-300 {f['100-300']:.0%}** · >300 {f['>300']:.1%}")
    print(f"  慢间隔 {r['slow_n']} 次  相邻慢间隔间距众数 **{r['slow_gap_mode'][0]} 帧**"
          f"（{r['slow_gap_mode'][1]} 次）→ {r['slow_gap_mode'][0] / max(r['fps'], 1e-9):.2f}s"
          f" 周期")
    print(f"  源速率（frame_id 推进）{r['src_rate']:.0f} 帧/秒；"
          f"**每记录 1 帧之间源接受了 {r['src_per_rec']:.2f} 帧**（1.00 = 源帧无遗漏）")
    print(f"  age_ms 中位 {r['age_med']:.1f} 最大 {r['age_max']:.1f}"
          f"；慢间隔处 {r['age_slow']:.1f} vs 快间隔处 {r['age_fast']:.1f}（一样小=无积压）")
    print(f"  frames_dropped={r['dropped']}" + (f"  帧图 {r['jpeg_kb']:.0f} KB" if r["jpeg_kb"] else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demos", default=str(DEFAULT_DEMOS))
    ap.add_argument("--session", default=None)
    ap.add_argument("--report", action="store_true", help="逐会话明细")
    args = ap.parse_args()
    d = Path(args.demos)
    sessions = ([d / args.session] if args.session
                else sorted(p for p in d.iterdir() if (p / "frames.jsonl").exists()))
    rs = []
    for s in sessions:
        r = analyze(s)
        r["jpeg_kb"] = jpeg_size(s)
        rs.append(r)
        if args.report or args.session:
            report_one(r)
    if len(rs) > 1:
        print(f"\n===== 汇总 {len(rs)} 场 =====")
        print(f"  有效帧率 {min(x['fps'] for x in rs):.2f}~{max(x['fps'] for x in rs):.2f} fps"
              f"（中位 {np.median([x['fps'] for x in rs]):.2f}）")
        print(f"  P95 间隔 {min(x['dt_p95'] for x in rs):.0f}~{max(x['dt_p95'] for x in rs):.0f} ms")
        print(f"  慢间隔间距众数 {[x['slow_gap_mode'][0] for x in rs]}")
        print(f"  源速率 {[round(x['src_rate']) for x in rs]} 帧/秒；"
              f"每记录 1 帧之间源接受 {[round(x['src_per_rec'], 2) for x in rs]} 帧")
        print(f"  frames_dropped {[x['dropped'] for x in rs]}")
        kb = [x["jpeg_kb"] for x in rs if x["jpeg_kb"]]
        if kb:
            print(f"  帧图 {np.median(kb):.0f} KB → 15/20/30 fps = "
                  f"{np.median(kb) * 15 * 60 / 1000:.0f}/{np.median(kb) * 20 * 60 / 1000:.0f}/"
                  f"{np.median(kb) * 30 * 60 / 1000:.0f} MB/分钟")


if __name__ == "__main__":
    main()