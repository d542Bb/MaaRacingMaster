"""当前生产代码离线重放：黄线层 + 路观测在历史帧上的触发率（2026-09-24）。

**为什么有这个文件**：trace 里的 road_offset 语义随 09-22 当天四次提交漂移
（见 [`scan_bnd_trigger.py`](scan_bnd_trigger.py) 版本批表），当前语义（v2 选择器 +
双护栏 + 单侧反推）只有 b4 两场晚间 trace。18~21 点控制场录控互斥无帧，无法重放；
但 09-22 上午 10 场（录制模式，8710 帧，白天光照，含维护者点名弯道段
113809_p2 fid 906~996）帧在。本脚本用**当前生产类原样重放**——import 不复刻
（先例：a 实验 ③ 闭环段），把「当前语义触发率」从 2 场晚间扩到 10 场白天。

**重放链**：帧 → `detect_boundary`（boundary.py，gate0.json 冻结标定）→
`_EgoRoadObserver`（module.py，每场一个实例=阶段生命期，与生产 chain 生命期一致）。
读帧口径 cv2.imread→BGR2RGB（与全部探针一致）。

**判读边界**：重放帧率=录制 15Hz（控制场 30fps），触发率是帧占比不受影响，
连续段换算秒按各场 frames.jsonl 的 ts_ns 中位间隔算。重放给的是"当前代码在
这些帧上会怎么报"，不是当晚生产代码的回放——版本差异归 scan_bnd_trigger。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/replay_bnd_current.py
    python tools/experiments/speedrush_vision/replay_bnd_current.py --session 20260922_113809_p2 --from 906 --to 996
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from maaracing_master.plugins.speedrush.boundary import detect_boundary
from maaracing_master.plugins.speedrush.module import _EgoRoadObserver

APP = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = APP / "demos"


def session_fps(sess: Path) -> float:
    f = sess / "frames.jsonl"
    ts = []
    if f.exists():
        for line in f.open(encoding="utf-8"):
            try:
                ts.append(json.loads(line).get("ts_ns"))
            except json.JSONDecodeError:
                continue
    ts = [t for t in ts if t is not None]
    if len(ts) > 10:
        return 1e9 / float(np.median(np.diff(ts)))
    return 15.0


def replay(sess: Path, lo: int | None, hi: int | None) -> dict:
    frames_dir = sess / "frames"
    paths = sorted(frames_dir.glob("*.jpg"))
    if lo is not None:
        paths = [p for p in paths if lo <= int(p.stem) <= hi]
    obs = _EgoRoadObserver()
    sides = {0: 0, 1: 0, 2: 0}
    none_fids: list[int] = []
    n_valid = 0
    for p in paths:
        fid = int(p.stem)
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        bnd = detect_boundary(rgb)
        sides[bnd.sides] += 1
        n_valid += int(bnd.validity)
        if obs.update(bnd) is None:
            none_fids.append(fid)
    return {"n": len(paths), "sides": sides, "valid": n_valid,
            "none_fids": none_fids}


def segs_of(fids: list[int], min_len: int) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    st: int | None = None
    prev: int | None = None
    for x in fids:
        if st is None:
            st = prev = x
        elif x == prev + 1:
            prev = x
        else:
            if prev - st + 1 >= min_len:
                out.append((st, prev, prev - st + 1))
            st = prev = x
    if st is not None and prev - st + 1 >= min_len:
        out.append((st, prev, prev - st + 1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None, help="只跑该场（默认全部 20260922_*）")
    ap.add_argument("--from", dest="lo", type=int, default=None)
    ap.add_argument("--to", dest="hi", type=int, default=None)
    args = ap.parse_args()

    if args.session:
        sessions = [DEMOS / args.session]
    else:
        sessions = sorted(p for p in DEMOS.glob("20260922_*") if (p / "frames").is_dir())
    tot = {"n": 0, "sides": {0: 0, 1: 0, 2: 0}, "none": 0}
    print("== 当前生产代码重放（detect_boundary + _EgoRoadObserver 原样）==\n")
    print(f"{'场':24s} {'帧':>5s} {'s0':>5s} {'s1':>5s} {'s2':>5s}  {'B 失燃料':>8s}  最长失效段")
    for sess in sessions:
        r = replay(sess, args.lo, args.hi)
        fps = session_fps(sess)
        nb = len(r["none_fids"])
        segs = segs_of(r["none_fids"], min_len=max(1, round(fps)))
        longest = max((ln for _, _, ln in segs), default=0)
        tot["n"] += r["n"]
        for k in tot["sides"]:
            tot["sides"][k] += r["sides"][k]
        tot["none"] += nb
        print(f"{sess.name:24s} {r['n']:5d} {r['sides'][0]:5d} {r['sides'][1]:5d} "
              f"{r['sides'][2]:5d}  {nb:5d}（{nb / max(r['n'], 1):5.1%}）  "
              f"{longest} 帧 ≈ {longest / fps:.1f}s（fps={fps:.1f}）")
    n = max(tot["n"], 1)
    print(f"\n合计 {tot['n']} 帧：sides==0 {tot['sides'][0] / n:.1%}、sides==1 "
          f"{tot['sides'][1] / n:.1%}、sides==2 {tot['sides'][2] / n:.1%}；"
          f"B（road_offset None）{tot['none']}（{tot['none'] / n:.1%}）")


if __name__ == "__main__":
    main()
