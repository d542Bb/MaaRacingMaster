# -*- coding: utf-8 -*-
"""路线级机会密度回放（C 类实验，只读人工局重建数据，不改生产）。

对每个候选横位目标 x，按 `t_move=0.3+2.0·|Δx|` 的换道时间门，统计未来
HORIZON 秒内够得着的币/奖励期望；墙只做硬约束（零收益），车做安全惩罚，
中心/远场做可开关的弱先验。然后看人工未来横位在候选里排第几。

输入：probe_potential_shadow.py 落盘的 potential_shadow_*.jsonl。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_route_density.py \
        <potential_shadow_p1.jsonl> <potential_shadow_p2.jsonl>
"""
from __future__ import annotations

import bisect
import json
import statistics
import sys
from pathlib import Path

import numpy as np

GRID = np.arange(-4.0, 4.0001, 0.1)
HORIZON_S = 3.0
HUMAN_HORIZON_S = 1.0
CAPTURE_LANE = 1.2          # 机会可达半径（道）：中心能同时够到两侧
CENTER_W = 0.0              # 可选性/中心先验（0=关）
FAR_W = 0.0                 # 远场流向先验（0=关）
CAR_W = 0.8                 # 车安全惩罚
CAR_SIG_LANE = 0.5


def candidate_scores(row: dict, *, capture_lane=CAPTURE_LANE,
                     center_w=CENTER_W, far_w=FAR_W, car_w=CAR_W):
    """候选横位目标 x 的路线分数（越大越好）；None=无可行候选。"""
    ego = row["ego_m"]
    lane_w = row["lane_w"]
    cap = capture_lane * lane_w
    objs = row["objs"]
    far = row.get("cor")
    far_hint = 0.0
    if far and far[3] is not None and (far[4] or 0.0) <= 300.0:
        far_hint = far[3]
    scores = []
    for x in GRID:
        tm = 0.3 + 2.0 * abs(x - ego) / lane_w
        val = 0.0
        for o in objs:
            kind, ox, t = o[0], o[1], o[2]
            if kind not in ("coin", "reward") or not o[6]:
                continue
            if t is None or not (0.0 <= t <= HORIZON_S):
                continue
            if t < tm:                 # 来不及换到该目标
                continue
            if abs(x - ox) > cap:
                continue
            w = (max(1.0, o[3]) if kind == "coin" else 3.0) / (1.0 + 0.2 * t)
            val += w
        if car_w:
            for o in objs:
                if o[0] != "car" or not o[6]:
                    continue
                t = o[2] if o[2] is not None else (
                    o[5] / 40.0 if o[5] is not None else None)
                if t is None or not (0.0 <= t <= HORIZON_S):
                    continue
                sig = CAR_SIG_LANE * lane_w
                val -= car_w * np.exp(-0.5 * ((x - o[1]) / sig) ** 2)
        if center_w:
            val -= center_w * (x / lane_w) ** 2
        if far_w:
            val += far_w * far_hint * (x / lane_w)
        scores.append(val)
    return np.array(scores)


def rank_of(score, target):
    """target 在候选里的百分位（0=最好）。"""
    i = int(np.argmin(np.abs(GRID - target)))
    s = score[i]
    return float(np.sum(score > s) / max(1, len(score) - 1))


def analyze(path: Path, *, capture_lane=CAPTURE_LANE, center_w=CENTER_W,
            far_w=FAR_W, car_w=CAR_W) -> dict:
    rows = [json.loads(l) for l in path.open(encoding="utf-8")]
    by_ts = {r["ts"]: r for r in rows}
    ts_list = sorted(by_ts)
    stats = {"human_future": [], "human_now": [], "sel": [], "pot": [],
             "center": [], "visible_centroid": []}
    top1 = 0
    n = 0
    for r in rows:
        if r["ego_m"] is None or not any(
                o[0] in ("coin", "reward") and o[6] and o[2] is not None
                and 0 <= o[2] <= HORIZON_S for o in r["objs"]):
            continue
        sc = candidate_scores(r, capture_lane=capture_lane,
                              center_w=center_w, far_w=far_w, car_w=car_w)
        if not np.isfinite(sc).any():
            continue
        n += 1
        # 人工未来 1s 横位（同选择帧近似）
        i = bisect.bisect_left(ts_list, r["ts"] + int(HUMAN_HORIZON_S * 1e9))
        fut = None
        for j in range(i, len(ts_list)):
            if by_ts[ts_list[j]]["ego_m"] is not None:
                fut = by_ts[ts_list[j]]["ego_m"]
                break
        if fut is not None:
            stats["human_future"].append(rank_of(sc, fut))
            if rank_of(sc, fut) <= 0.02:
                top1 += 1
        stats["human_now"].append(rank_of(sc, r["ego_m"]))
        if r["sel"]:
            stats["sel"].append(rank_of(sc, r["sel"][2]))
        if r["pot"].get("full") is not None:
            stats["pot"].append(rank_of(sc, r["pot"]["full"]))
        stats["center"].append(rank_of(sc, 0.0))
        # 可见机会质心
        xs = [o[1] for o in r["objs"]
              if o[0] in ("coin", "reward") and o[6] and o[2] is not None
              and 0 <= o[2] <= HORIZON_S]
        if xs:
            stats["visible_centroid"].append(rank_of(sc, statistics.mean(xs)))
    return {"n": n, "top1": top1, "stats": stats}


def report(name, res):
    print(f"\n== {name} ==  有可见机会的拍 {res['n']}")
    for k, v in res["stats"].items():
        if not v:
            continue
        v = sorted(v)
        print(f"  {k:>16}: 中位排名 {statistics.median(v):.2f}"
              f"  前 10% {sum(x <= 0.10 for x in v)/len(v):.0%}"
              f"  前 25% {sum(x <= 0.25 for x in v)/len(v):.0%}")
    if res["n"]:
        print(f"  人工未来 1s 落在前 2%: {res['top1']}/{res['n']}")


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        report(Path(arg).stem, analyze(Path(arg)))
    print("\n-- 消融（第一名=默认口径；只跑第一份输入） --")
    path = Path(argv[0])
    for label, kw in (("默认", {}),
                      ("捕获 0.8 道", {"capture_lane": 0.8}),
                      ("捕获 1.6 道", {"capture_lane": 1.6}),
                      ("中心先验 0.02", {"center_w": 0.02}),
                      ("远场先验 0.05", {"far_w": 0.05}),
                      ("无车惩罚", {"car_w": 0.0})):
        res = analyze(path, **kw)
        hf = sorted(res["stats"]["human_future"])
        print(f"  {label:>12}: 人工未来中位排名 "
              f"{statistics.median(hf):.2f}" if hf else label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
