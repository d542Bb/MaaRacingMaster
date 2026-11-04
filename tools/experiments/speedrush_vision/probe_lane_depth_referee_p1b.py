# -*- coding: utf-8 -*-
"""P1b：等跨度窗口的分工检验 + 选择偏差核查（不改生产，不覆盖 P1 记录）。

**与 P1 的唯一差别**：窗口两端都必须落在**深度新鲜读数所在的那一帧**上，且该帧
恰有一条线观测——两侧跨度因此严格相等，不再用"之前最近的新鲜值"补端点。其余
口径（像素斜率近邻匹配、噪声底、方向定义）与 P1 完全一致。

**为什么还要报选择偏差**：深度新鲜拍只占 27%，等跨度筛选会挑出"深度连续新鲜
且线可跟踪"的路段。若一致率上升，可能是样本构成变了而不是跨度问题，所以把筛选
前后两组在跨长、横移幅度、弯道、贴墙、线数、状态上逐项对照。

**为什么 50% 不是通用基线**：若某一侧方向分布偏斜（比如几乎总在往一个方向漂），
随机猜测的一致率就不等于 50%。这里同时报两侧方向边际，并用**保留时间结构的
循环移位**做置换检验（移位 ≥10 个窗口，超出相邻相关性范围）。

输出：``referee_p1b/`` 逐窗口一行 + stdout 汇总。P1 的原始产物在
``referee/``，本脚本不写它。
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_lane_depth_referee as R          # noqa: E402  冻结口径，只调用

MIN_MOVE_LANE = R.MIN_MOVE_LANE
MIN_MOVE_SLOPE = R.MIN_MOVE_SLOPE
PERM_SHIFTS = 400
MIN_SHIFT = 10


def fresh_points(ticks: list[dict]) -> list[tuple[int, float]]:
    """深度**新鲜**路心序列：保持/遮挡/记忆值一律不算新证据。"""
    out = []
    for r in ticks:
        if r.get("ro_hold") or r.get("dgeo_new") is False:
            continue
        v = r.get("road_offset")
        if isinstance(v, (int, float)):
            out.append((int(r["fid"]), float(v)))
    return sorted(set(out))


def build(ticks: list[dict], have: set[int], session: str) -> list[dict]:
    obs = {o["ofid"]: o for o in R.lane_observations(ticks)
           if o["ofid"] in have and not o["pred"] and o["slopes"]}
    pts = [p for p in fresh_points(ticks) if p[0] in have]
    recs = []
    for (f0, d0), (f1, d1) in zip(pts, pts[1:]):
        o0, o1 = obs.get(f0), obs.get(f1)
        if o0 is None or o1 is None:
            continue                      # 该帧没有线观测：跨度就不等，直接弃
        da = R.match_d(o0["slopes"], o1["slopes"])
        if da is None:
            continue
        dt = ((o1["ts_ns"] or 0) - (o0["ts_ns"] or 0)) / 1e9
        recs.append({"session": session, "fid0": f0, "fid1": f1,
                     "span_fid": f1 - f0, "dt_s": round(dt, 3),
                     "dA": round(da, 4), "dD": round(d1 - d0, 4),
                     "dL": round((o1["x"] or 0) - (o0["x"] or 0), 4),
                     "x": round(float(o1["x"]), 3), "state": o1["state"],
                     "theta": o1["theta"], "n_line": len(o1["slopes"]),
                     "grid_chg": o0["grid"] != o1["grid"]})
    return recs


def agree(rows: list[dict]) -> float:
    return sum(1 for r in rows if -r["dA"] * r["dD"] > 0) / len(rows)


def chance_baseline(rows: list[dict]) -> float:
    """两侧方向独立时的期望一致率（用边际分布算，不假设 50%）。"""
    p_d = sum(1 for r in rows if r["dD"] > 0) / len(rows)
    p_a = sum(1 for r in rows if r["dA"] < 0) / len(rows)
    return p_d * p_a + (1 - p_d) * (1 - p_a)


def perm_p(rows_by_sess: dict[str, list[dict]], obs: float) -> float:
    """循环移位置换：打乱两路的**对应关系**但各自保留时间结构。"""
    hits = tot = 0
    for rows in rows_by_sess.values():
        if len(rows) <= MIN_SHIFT * 2:
            continue
        da = [r["dA"] for r in rows]
        dd = [r["dD"] for r in rows]
        for _ in range(PERM_SHIFTS // max(1, len(rows_by_sess))):
            k = random.choice(list(range(MIN_SHIFT, len(rows) - MIN_SHIFT)))
            a = sum(1 for x, y in zip(da, dd[k:] + dd[:k]) if -x * y > 0) / len(da)
            tot += 1
            hits += int(a >= obs)
    return hits / tot if tot else float("nan")


def profile(tag: str, rows: list[dict]) -> None:
    if not rows:
        print(f"  {tag}: 空")
        return
    med = lambda k: statistics.median(r[k] for r in rows)          # noqa: E731
    print(f"  {tag:10s} n={len(rows):4d} 跨度p50 {med('span_fid'):3.0f}帧 "
          f"|ΔD|p50 {statistics.median(abs(r['dD']) for r in rows):.3f} "
          f"|ΔA|p50 {statistics.median(abs(r['dA']) for r in rows):.3f} "
          f"|x|p50 {statistics.median(abs(r['x']) for r in rows):.2f} "
          f"线数p50 {med('n_line'):.0f} "
          f"改判 {100*sum(1 for r in rows if r['grid_chg'])/len(rows):.0f}% "
          f"TRACK {100*sum(1 for r in rows if r['state']=='TRACK')/len(rows):.0f}%")


def main() -> None:
    root = Path.home() / "AppData/Roaming/MaaRacingMaster"
    ticks_all = R.load_ticks(root / "logs")
    base = root / "debug" / "speedrush"
    out = base / "referee_p1b"
    out.mkdir(parents=True, exist_ok=True)
    random.seed(7)
    per_sess: dict[str, list[dict]] = {}
    for sess in sorted(base.glob("manual_*")):
        fj = sess / "frames.jsonl"
        if not fj.is_file():
            continue
        frames = [json.loads(l) for l in fj.read_text(encoding="utf-8")
                  .splitlines() if l.strip()]
        have = {f["frame_id"] for f in frames}
        name = R.pick_session(ticks_all, [f["frame_id"] for f in frames])
        if name is None:
            print(f"{sess.name}: 无同会话 trace，跳过")
            continue
        recs = build(ticks_all[name], have, sess.name)
        if recs:
            per_sess[sess.name] = recs
        (out / f"p1b_{sess.name}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs),
            encoding="utf-8")
        print(f"{sess.name} ← logs/{name}: 等跨度窗口 {len(recs)}")
    big = {k: [r for r in v if abs(r["dA"]) >= MIN_MOVE_SLOPE
               and abs(r["dD"]) >= MIN_MOVE_LANE] for k, v in per_sess.items()}
    allb = [r for v in big.values() for r in v]
    print(f"\n等跨度窗口合计 {sum(len(v) for v in per_sess.values())}，"
          f"两侧同超噪声底 {len(allb)}")
    if len(allb) < 15:
        print("样本不足，判定：证据不足（等跨度筛选后已无可用样本）")
    else:
        obs = agree(allb)
        cb = chance_baseline(allb)
        print(f"方向一致率 {obs:.0%}  独立零假设下的期望一致率 {cb:.0%}"
              f"（ΔD>0 占 {100*sum(1 for r in allb if r['dD']>0)/len(allb):.0f}%，"
              f"ΔA<0 占 {100*sum(1 for r in allb if r['dA']<0)/len(allb):.0f}%）")
        print(f"置换检验（循环移位，保留时间结构）p = {perm_p(big, obs):.3f}")
        for k, v in sorted(big.items()):
            if len(v) >= 5:
                print(f"  {k}: n={len(v):3d} 一致率 {agree(v):.0%} "
                      f"基线 {chance_baseline(v):.0%}")
        sess = sorted(big)
        dev = [r for k in sess[:R.DEV_SESSIONS] for r in big[k]]
        hold = [r for k in sess[R.DEV_SESSIONS:] for r in big[k]]
        if dev and hold:
            rr = sorted(r["dD"] / -r["dA"] for r in dev)
            r_dev = statistics.median(rr)
            rel = sorted(abs(r["dD"] / -r["dA"] / r_dev - 1) for r in hold)
            print(f"  开发集 s_eff = {1/r_dev:.2f}（深度自用 3.38/1.42=2.38，"
                  f"金标 2.785）→ 留出集比值相对偏差 p50 "
                  f"{statistics.median(rel):.0%} p90 {rel[int(.9*len(rel))]:.0%}")
    print("\n选择偏差对照：")
    profile("全部等跨度", [r for v in per_sess.values() for r in v])
    profile("超噪声底", allb)
    print("wrote", out)


if __name__ == "__main__":
    main()
