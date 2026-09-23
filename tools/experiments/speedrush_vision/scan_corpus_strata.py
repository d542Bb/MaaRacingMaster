"""语料分层普查：用生产侧 trace 信号回答"这一格素材到底有没有、在哪几段"（2026-09-23）。

**为什么有这个文件**：重录规格的第一版是拿 141 帧（一个正常场次 + 8 个撞墙场次）设计的，
而 `control_traces/` 里有 18 场、13300 行带 `bnd_sides`/`car_views`/`road_offset`/
`steer_norm` 的现成场景标签。先普查存量，再决定要不要录——顺序反了就是拿录制时间
去喂一个本可以离线回答的问题。

**判据来源与局限（如实声明）**：分层用的是**生产侧自己的读数**，不是真值。
`car_views==0` 读作"无前车"，但旧检测器召回不足（本目录已实测：141 帧中 54 帧空框、
正前主车常缺）⇒ 格 1 的比例是**上界**。`bnd_sides` 同理由边界层自报。
本命令的用途是**筛候选段**，不是给结论；筛出的段仍要目检或金标确认。

**连续段口径**：trace 行按 fid 步长中位 ≈2 采样（约 10Hz），故"≥6 行"≈ 12 帧 ≈ 0.6s；
`--min-rows` 可调。帧号范围直接给到 `demos/<session>/frames/%06d.jpg` 可取。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/scan_corpus_strata.py
    python tools/experiments/speedrush_vision/scan_corpus_strata.py --min-rows 30
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

import numpy as np

APP = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
TRACES = APP / "control_traces"

# 分层判据：全部只用 trace 已有列，不引入新观测量
STRATA = {
    "1 直道·双侧·无前车": lambda r: (r.get("bnd_sides") == 2 and r.get("car_views") == 0
                                and abs(r.get("steer_norm") or 9) < 0.15
                                and (r.get("bnd_residual") if r.get("bnd_residual") is not None else 9) < 1.5),
    "2 稳定跟车": lambda r: (r.get("bnd_sides") == 2 and (r.get("car_views") or 0) > 0),
    "3 弯道·打舵": lambda r: abs(r.get("steer_norm") or 0) > 0.35,
    "4 贴墙·横向偏移大": lambda r: (r.get("road_offset") is not None
                              and abs(r["road_offset"]) > 1.2),
    "5 单侧黄线可见": lambda r: r.get("bnd_sides") == 1,
    "6 双侧且 vp_x 有值": lambda r: (r.get("bnd_sides") == 2 and r.get("bnd_vp_x") is not None),
}


def load():
    out = {}
    for f in sorted(TRACES.glob("trace_*.jsonl")):
        rows = []
        for line in f.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "bnd_sides" in r:
                rows.append(r)
        if rows:
            out[f.name[len("trace_"):-len(".jsonl")]] = rows
    return out


def runs(rows, pred, min_rows):
    res, st = [], None
    for i, r in enumerate(rows):
        if pred(r):
            st = i if st is None else st
        elif st is not None:
            if i - st >= min_rows:
                res.append((rows[st]["fid"], rows[i - 1]["fid"], i - st))
            st = None
    if st is not None and len(rows) - st >= min_rows:
        res.append((rows[st]["fid"], rows[-1]["fid"], len(rows) - st))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rows", type=int, default=6, help="连续段最少 trace 行数")
    ap.add_argument("--top", type=int, default=6, help="每格打印最长的前 N 段")
    args = ap.parse_args()
    sess = load()
    n = sum(len(v) for v in sess.values())
    step = np.median([np.median(np.diff([r["fid"] for r in v])) for v in sess.values() if len(v) > 10])
    print(f"== 语料分层普查（{len(sess)} 场、{n} 行 trace、fid 步长中位 {step:.0f}；"
          f"连续段 ≥{args.min_rows} 行）==")
    print("判据是生产侧自报读数，非真值；car_views==0 受旧检测器召回限制 ⇒ 格 1 为上界。\n")
    for name, pred in STRATA.items():
        tot = sum(1 for v in sess.values() for r in v if pred(r))
        found = [(s, a, b, ln) for s, v in sess.items() for a, b, ln in runs(v, pred, args.min_rows)]
        found.sort(key=lambda x: -x[3])
        print(f"{name:20s} 行 {tot:5d}（{tot/max(n,1):5.1%}）  候选段 {len(found):3d} 个"
              f"  最长 {found[0][3] if found else 0} 行")
        for s, a, b, ln in found[:args.top]:
            print(f"      demos/{s}/frames  fid {a}~{b}（{ln} 行 ≈ {ln*step/20:.1f}s 画面时间）")
        print()


if __name__ == "__main__":
    main()
