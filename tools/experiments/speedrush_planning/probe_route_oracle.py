# -*- coding: utf-8 -*-
"""路线机会上限对比（C 类实验，只读人工局回放数据，不改生产）。

回答两个问题：
1. 固定场景下，不同人工局在同一 (stage, timer) 看到的机会是否一致——
   决定「离线机会地图」能不能当 oracle。
2. 仅用当前帧可见机会的路线估计，距离「未来 3 秒机会全知」还有多远。

oracle 只在离线评估里用；在线估计器只吃当前帧 objs，不吃地图、不吃
stage 编号、不吃任何坐标查表。

输入：probe_potential_shadow.py 落盘的 potential_shadow_*.jsonl。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_route_oracle.py \
        --consistency --oracle \
        manual_20261016_224328_p1 manual_20261018_224404_p1 ...
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import statistics
from pathlib import Path

import numpy as np

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"

GRID = np.arange(-4.0, 4.0001, 0.1)
HORIZON_S = 3.0
CAPTURE_LANE = 1.2
T_MOVE_BASE_S = 0.3
T_MOVE_PER_LANE_S = 2.0
COIN_W = 1.0
REWARD_W = 3.0
CLOCK_TOL_S = 0.75       # 跨局对齐容差
X_TOL_M = 1.0


# ---------- 赛道时钟 ----------

def load_clock(sess: str):
    """返回 ts -> (stage, timer) 的插值函数；timer 随时间递减。"""
    rows = []
    for line in (DEMOS / sess / "hud.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        f = r.get("fields") or {}
        stage = (f.get("stage") or {}).get("value")
        timer = (f.get("timer") or {}).get("value")
        if stage is None or timer is None:
            continue
        rows.append((r["ts_ns"], int(stage), float(timer)))
    rows.sort()
    ts = [r[0] for r in rows]

    def clock(frame_ts: int):
        if not rows:
            return None
        i = bisect.bisect_left(ts, frame_ts)
        if i <= 0:
            return rows[0][1], rows[0][2]
        if i >= len(rows):
            return rows[-1][1], rows[-1][2]
        a, b = rows[i - 1], rows[i]
        if a[1] != b[1]:
            return (a if frame_ts - a[0] <= b[0] - frame_ts else b)[1:]
        if b[0] == a[0]:
            return a[1], a[2]
        f = (frame_ts - a[0]) / (b[0] - a[0])
        return a[1], a[2] + f * (b[2] - a[2])

    return clock


# ---------- 机会点 ----------

def opportunity_points(rows, clock, *, horizon=HORIZON_S):
    """把每帧可见的币/奖励投影成赛道时钟上的点：(clock, stage, x, w)。"""
    pts = []
    for r in rows:
        if r["ego_m"] is None or r["lane_w"] is None:
            continue
        c = clock(r["ts"])
        if c is None:
            continue
        stage, timer = c
        for o in r["objs"]:
            kind, ox, t, _, _, _, in_corr, _ = o
            if kind not in ("coin", "reward") or not in_corr:
                continue
            if t is None or not (0.0 <= t <= horizon):
                continue
            w = REWARD_W if kind == "reward" else COIN_W
            # timer 递减：到达时刻的 timer = 当前 timer - 到达秒
            pts.append((stage, timer - t, float(ox), w))
    return pts


def dedup(pts, *, dt=0.6, dx=0.6):
    """同一机会在多帧重复出现：按 (stage, 时钟, 横位) 网格合并，权重取最大。"""
    best = {}
    for stage, t, x, w in pts:
        key = (stage, int(np.floor(t / dt)), int(np.floor(x / dx)))
        if key not in best or w > best[key][3]:
            best[key] = (stage, t, x, w)
    return list(best.values())


def score_grid(points, x_now, lane_w, *, capture_lane=CAPTURE_LANE,
               horizon=HORIZON_S):
    """候选横位的机会密度分；points=(t_rel, x, w)。"""
    if not points or lane_w <= 0:
        return np.zeros(GRID.shape)
    arr = np.array(points, dtype=float)          # (n,3)
    t = arr[:, 0][None, :]
    xo = arr[:, 1][None, :]
    w = arr[:, 2][None, :]
    x = GRID[:, None]
    tm = T_MOVE_BASE_S + T_MOVE_PER_LANE_S * np.abs(x - x_now) / lane_w
    cap = capture_lane * lane_w
    ok = (t >= tm) & (np.abs(x - xo) <= cap) & (t <= horizon)
    return (np.where(ok, w / (1.0 + 0.2 * t), 0.0)).sum(axis=1)


# ---------- 一致性 ----------

def consistency(sessions, clocks, rows_by_sess):
    """两两局在同一 (stage, 时钟) 区域的点匹配率。"""
    clouds = {}
    for s in sessions:
        pts = dedup(opportunity_points(rows_by_sess[s], clocks[s]))
        clouds[s] = pts
        print(f"  {s}: {len(pts)} 个机会点")
    print("\n两两匹配（时钟差 <0.8s 且横位差 <1m 视为同一机会）:")
    pairs = []
    for i, a in enumerate(sessions):
        for b in sessions[i + 1:]:
            # 只比重叠时钟区间
            a_clk = [p[1] for p in clouds[a]]
            b_clk = [p[1] for p in clouds[b]]
            if not a_clk or not b_clk:
                continue
            lo = max(min(a_clk), min(b_clk))
            hi = min(max(a_clk), max(b_clk))
            if hi - lo < 5.0:
                continue
            A = [p for p in clouds[a] if lo <= p[1] <= hi]
            B = [p for p in clouds[b] if lo <= p[1] <= hi]
            if not A or not B:
                continue

            def hit(P, Q):
                n = 0
                for s0, t0, x0, _ in P:
                    if any(abs(t0 - t1) <= CLOCK_TOL_S and abs(x0 - x1) <= X_TOL_M
                           for s1, t1, x1, _ in Q):
                        n += 1
                return n / len(P)

            ra, rb = hit(A, B), hit(B, A)
            pairs.append((ra + rb) / 2)
            print(f"  {a} × {b}: 重叠 {hi - lo:.0f}s，A 命中 {ra:.0%}，"
                  f"B 命中 {rb:.0%}（A={len(A)} B={len(B)}）")
    if pairs:
        print(f"  平均往返命中 {statistics.mean(pairs):.0%}，中位 {statistics.median(pairs):.0%}")
    return pairs


# ---------- 上限对比 ----------

def _bucket(t, x):
    return (int(t / 0.5), int(np.floor(x / 0.5)))


def future_points(rows, clk, i, horizon):
    """第 i 帧起未来 horizon 秒内出现过的机会点 (t_rel, x, w)。"""
    stage, timer = clk[i]
    pts = []
    for j in range(i, len(rows)):
        cj = clk[j]
        if cj is None or cj[0] != stage:
            break
        dt = timer - cj[1]
        if dt > horizon:
            break
        for o in rows[j]["objs"]:
            kind, ox, t, _, _, _, in_corr, _ = o
            if kind not in ("coin", "reward") or not in_corr:
                continue
            if t is None or t < 0.0:
                continue
            t_rel = dt + t
            if 0.0 <= t_rel <= horizon:
                pts.append((t_rel, float(ox),
                            REWARD_W if kind == "reward" else COIN_W))
    merged = {}
    for a, b, w in pts:
        merged[_bucket(a, b)] = (a, b, w)
    return list(merged.values())


EST_WINDOWS = (0.0, 1.0, 2.0, 3.0)


def oracle_compare(sessions, clocks, rows_by_sess, *, horizon=HORIZON_S,
                   center_w=0.02, windows=EST_WINDOWS, capture=CAPTURE_LANE):
    """未来 horizon 秒机会 = 信息上限；不同信息窗口的估计器与之对比。"""
    keys = ([f"est{w:.0f}" for w in windows]
            + [f"est{w:.0f}+c" for w in windows]
            + ["sel", "center"])
    stats = {k: [] for k in keys}
    loss = {k: [] for k in keys}
    dist = {k: [] for k in keys}
    per = {s: {k: [] for k in keys} for s in sessions}
    n_used = 0
    for sess in sessions:
        rows = rows_by_sess[sess]
        clock = clocks[sess]
        clk = [clock(r["ts"]) for r in rows]
        for i, r in enumerate(rows):
            if r["ego_m"] is None or r["lane_w"] is None or clk[i] is None:
                continue
            oracle_pts = future_points(rows, clk, i, horizon)
            if sum(1 for p in oracle_pts if p[0] > 0.5) < 2:
                continue        # 未来机会太少，不构成有区分度的判断
            oracle = score_grid(oracle_pts, r["ego_m"], r["lane_w"],
                                capture_lane=capture, horizon=horizon)
            if oracle.max() - oracle.min() < 0.5:
                continue
            best = oracle.max()
            targets = {"sel": None if r["sel"] is None else float(r["sel"][2]),
                       "center": 0.0}
            # 在线估计器在当前帧就能看到的机会（t≤horizon）
            now_pts = [(float(o[2]), float(o[1]),
                        REWARD_W if o[0] == "reward" else COIN_W)
                       for o in r["objs"]
                       if o[0] in ("coin", "reward") and o[6]
                       and o[2] is not None and 0.0 <= o[2] <= horizon]
            for w in windows:
                if w >= horizon:
                    pts = oracle_pts
                else:
                    merged = {_bucket(t, x): (t, x, wt)
                              for t, x, wt in now_pts + [
                                  p for p in oracle_pts if p[0] <= w]}
                    pts = list(merged.values())
                sc = score_grid(pts, r["ego_m"], r["lane_w"],
                                capture_lane=capture, horizon=horizon)
                targets[f"est{w:.0f}"] = float(GRID[int(np.argmax(sc))])
                cen = sc - center_w * (GRID / r["lane_w"]) ** 2
                targets[f"est{w:.0f}+c"] = float(GRID[int(np.argmax(cen))])
            n_used += 1
            opt = float(GRID[int(np.argmax(oracle))])
            for k, x in targets.items():
                if x is None:
                    continue
                val = oracle[int(np.argmin(np.abs(GRID - x)))]
                stats[k].append(float(np.sum(oracle > val) / (len(oracle) - 1)))
                lk = 0.0 if best <= 1e-9 else (best - val) / best
                loss[k].append(lk)
                per[sess][k].append(lk)
                dist[k].append(abs(x - opt))
    print(f"\n有区分度的拍 {n_used}")
    print(f"{'对象':>8} {'中位排名':>8} {'前25%':>6} {'平均分数损失':>10} {'离上限横距':>10}")
    for k in keys:
        v = stats[k]
        if not v:
            continue
        print(f"{k:>8} {statistics.median(v):>8.2f}"
              f" {sum(x <= 0.25 for x in v)/len(v):>6.0%}"
              f" {statistics.mean(loss[k]):>10.0%}"
              f" {statistics.median(dist[k]):>9.2f}m")
    print("\n分局平均分数损失:")
    print(f"{'局':>28} " + " ".join(f"{k:>7}" for k in keys))
    for s, d in per.items():
        if not d[keys[0]]:
            continue
        print(f"{s:>28} " + " ".join(
            f"{statistics.mean(d[k]):>6.0%}" if d[k] else "     -" for k in keys))


def sweep(sessions, clocks, rows_by_sess, *, horizon=HORIZON_S,
          capture=CAPTURE_LANE, windows=EST_WINDOWS,
          center_ws=(0.0, 0.005, 0.01, 0.02, 0.05, 0.1)):
    """信息窗口 × 中心权重 的损失网格，检查阈值是否只在一组参数下成立。"""
    acc = {(w, c): [] for w in windows for c in center_ws}
    for sess in sessions:
        rows = rows_by_sess[sess]
        clk = [clocks[sess](r["ts"]) for r in rows]
        for i, r in enumerate(rows):
            if r["ego_m"] is None or r["lane_w"] is None or clk[i] is None:
                continue
            oracle_pts = future_points(rows, clk, i, horizon)
            if sum(1 for p in oracle_pts if p[0] > 0.5) < 2:
                continue
            oracle = score_grid(oracle_pts, r["ego_m"], r["lane_w"],
                                capture_lane=capture, horizon=horizon)
            if oracle.max() - oracle.min() < 0.5:
                continue
            best = oracle.max()
            now_pts = [(float(o[2]), float(o[1]),
                        REWARD_W if o[0] == "reward" else COIN_W)
                       for o in r["objs"]
                       if o[0] in ("coin", "reward") and o[6]
                       and o[2] is not None and 0.0 <= o[2] <= horizon]
            for w in windows:
                if w >= horizon:
                    pts = oracle_pts
                else:
                    merged = {_bucket(t, x): (t, x, wt)
                              for t, x, wt in now_pts + [
                                  p for p in oracle_pts if p[0] <= w]}
                    pts = list(merged.values())
                base = score_grid(pts, r["ego_m"], r["lane_w"],
                                  capture_lane=capture, horizon=horizon)
                for cw in center_ws:
                    sc = base - cw * (GRID / r["lane_w"]) ** 2
                    x = float(GRID[int(np.argmax(sc))])
                    val = oracle[int(np.argmin(np.abs(GRID - x)))]
                    acc[(w, cw)].append((best - val) / best)
    print(f"\n平均分数损失（行=信息窗口秒，列=中心权重）")
    print(f"{'窗口':>6} " + " ".join(f"{c:>6.3f}" for c in center_ws))
    for w in windows:
        line = []
        for cw in center_ws:
            v = acc[(w, cw)]
            line.append(f"{statistics.mean(v):>6.0%}" if v else "     -")
        print(f"{w:>6.0f} " + " ".join(line))


def load_sessions(names):
    rows, clocks = {}, {}
    for s in names:
        p = DATA / f"potential_shadow_{s}.jsonl"
        if not p.exists():
            print(f"跳过（缺回放数据）: {s}")
            continue
        rows[s] = [json.loads(l) for l in p.open(encoding="utf-8")]
        clocks[s] = load_clock(s)
    return rows, clocks


def main(argv=None):
    ap = argparse.ArgumentParser(description="路线机会上限对比")
    ap.add_argument("sessions", nargs="*")
    ap.add_argument("--consistency", action="store_true")
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--horizon", type=float, default=HORIZON_S)
    ap.add_argument("--capture", type=float, default=CAPTURE_LANE)
    ap.add_argument("--center-w", type=float, default=0.02)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args(argv)
    names = args.sessions or sorted(
        p.name[len("potential_shadow_"):-len(".jsonl")]
        for p in DATA.glob("potential_shadow_*.jsonl"))
    rows, clocks = load_sessions(names)
    sessions = [s for s in names if s in rows]
    if not sessions:
        print("没有可用的回放数据")
        return 2
    print(f"载入 {len(sessions)} 局: {', '.join(sessions)}")
    if args.consistency or not args.oracle:
        print("\n== 固定赛道机会地图：跨局一致性 ==")
        consistency(sessions, clocks, rows)
    if args.oracle or not args.consistency:
        print(f"\n== 信息窗口 vs 未来 {args.horizon:.0f}s 机会上限 ==")
        oracle_compare(sessions, clocks, rows, horizon=args.horizon,
                       center_w=args.center_w, capture=args.capture)
    if args.sweep:
        print("\n== 参数敏感性 ==")
        sweep(sessions, clocks, rows, horizon=args.horizon,
              capture=args.capture)
        for h in (2.0, 4.0):
            if abs(h - args.horizon) < 1e-6:
                continue
            print(f"\n-- oracle 窗口 {h:.0f}s --")
            oracle_compare(sessions, clocks, rows, horizon=h,
                           center_w=args.center_w, capture=args.capture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
