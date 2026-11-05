# -*- coding: utf-8 -*-
"""两局综合审计（20261102_110128）：撞车/反打/漏分三问的量化。

- 撞车痕迹：hud_score 掉分事件与低速锁（v 列）；
- 反打：steer 符号翻转风暴（1s 内 ≥4 次）、翻面后目标/层是否翻面；
- 漏分：sel_dbg.opp 里余量两列都为正、却无请求且层 4 的持续段（分侧）。

用法::

    python tools/experiments/world_state/game_audit.py \
        --session 20261102_110128
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path


def load(session: str) -> list[dict]:
    p = (Path(os.environ.get("APPDATA", str(Path.home())))
         / "MaaRacingMaster" / "logs" / session / "trace" / "speedrush.jsonl")
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def ts_of(r: dict) -> float:
    for k in ("ts_mono_ns", "ts_wall_ns", "ts_ns"):
        v = r.get(k)
        if v is not None:
            return v / 1e9
    return 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261102_110128")
    args = ap.parse_args(argv)
    rows = [r for r in load(args.session) if r.get("ts_mono_ns")]
    ts = [ts_of(r) for r in rows]
    print(f"== {args.session}  {len(rows)} 拍  {ts[-1]-ts[0]:.1f}s")

    # ---- 撞车痕迹：hud_score 掉分（血量不落 trace，掉分即扣分事件） ----
    prev = None
    drops = []
    for i, r in enumerate(rows):
        s = r.get("hud_score")
        if s is None:
            continue
        if prev is not None and s < prev[0] - 30:
            drops.append((i, prev[0], s))
        prev = (s, i)
    print(f"hud_score 掉分事件(>30): {len(drops)}  {drops[:10]}")
    vvals = [r.get("v") for r in rows if isinstance(r.get("v"), (int, float))]
    print(f"v 列 min/max: {min(vvals) if vvals else None}/{max(vvals) if vvals else None}")
    low_v = sum(1 for v in vvals if v < 1)
    print(f"v<1 拍数（撞车锁痕迹）: {low_v}")

    # ---- 反打：翻转风暴 + 风暴后目标/层翻面 ----
    flips = [i for i in range(1, len(rows))
             if isinstance(rows[i-1].get("steer_norm"), (int, float))
             and isinstance(rows[i].get("steer_norm"), (int, float))
             and rows[i-1]["steer_norm"] * rows[i]["steer_norm"] < 0]
    seg = []
    for i in flips:
        cnt = sum(1 for j in flips if 0 <= ts[i] - ts[j] <= 1.0)
        if cnt >= 4:
            if seg and ts[i] - ts[seg[-1][1]] < 2.0:
                seg[-1] = (seg[-1][0], i, max(seg[-1][2], cnt))
            else:
                seg.append((i, i, cnt))
    print(f"反打风暴段(1s内≥4翻): {len(seg)}")
    # 真打舵口径：过零且两侧幅度 ≥0.05（零点噪声占过零数 ~8 成，
    # 20261102_125414 复核：0.0 口径 0.90/s 中仅 0.16/s 是 ≥0.05 的真翻转）
    flips_hi = [i for i in flips
                if abs(rows[i - 1]["steer_norm"]) >= 0.05
                and abs(rows[i]["steer_norm"]) >= 0.05]
    seg_hi = []
    for i in flips_hi:
        cnt = sum(1 for j in flips_hi if 0 <= ts[i] - ts[j] <= 1.0)
        if cnt >= 4:
            if seg_hi and ts[i] - ts[seg_hi[-1][1]] < 2.0:
                seg_hi[-1] = (seg_hi[-1][0], i)
            else:
                seg_hi.append((i, i))
    print(f"  真打舵(幅度≥0.05): 翻转 {len(flips_hi)} ({len(flips_hi) / (ts[-1] - ts[0]):.2f}/s)"
          f"  风暴段 {len(seg_hi)}")
    # 风暴后 2s 内目标翻面（>0.5 道）或并线
    tf_after = 0
    for a, b, _ in seg:
        for j in range(b, min(b + 40, len(rows))):
            sd1 = rows[j - 1].get("sel_dbg") or {}
            sd2 = rows[j].get("sel_dbg") or {}
            t1, t2 = sd1.get("target"), sd2.get("target")
            if t1 is not None and t2 is not None and t1 * t2 < 0 \
                    and abs(t1) > 0.5 and abs(t2) > 0.5:
                tf_after += 1
                break
    print(f"  风暴后 2s 内目标翻面: {tf_after}/{len(seg)}")

    # ---- 漏分：余量两列都为正、无请求、层 4（保持），按侧分账 ----
    missed = {"L": 0, "R": 0, "seg": 0, "segs": []}
    run = None
    for i, r in enumerate(rows):
        sd = r.get("sel_dbg") or {}
        opp = sd.get("opp") or {}
        rows_opp = [e for e in (opp.get("car") or []) + (opp.get("rw") or [])
                    if isinstance(e, (list, tuple)) and len(e) >= 2
                    and e[1] is not None]
        good = any(len(e) >= 3 and e[1] is not None and e[1] > 0
                   for e in rows_opp)
        holding = r.get("sel_layer") == 4 and r.get("sel_why") == "center:coarse"
        req = sd.get("req")
        if good and holding and req is None:
            x = next((e[1] for e in rows_opp if len(e) >= 3 and e[1] > 0), 0)
            side = "L" if x < 0 else "R"
            if run is None:
                run = [i, i, side]
            else:
                run[1] = i
                if ts[i] - ts[run[1] - 1] > 1.0:
                    missed["segs"].append(run)
                    run = [i, i, side]
        else:
            if run is not None:
                missed["segs"].append(run)
                run = None
    if run is not None:
        missed["segs"].append(run)
    for a, b, side in missed["segs"]:
        if ts[b] - ts[a] >= 1.0:
            missed["seg"] += 1
            missed[side] += 1
    print(f"漏分持续段(≥1s, 层4保持, 有正余量, 无请求): {missed['seg']} "
          f"（左 {missed['L']} / 右 {missed['R']}）")
    for a, b, side in missed["segs"]:
        if ts[b] - ts[a] >= 1.0:
            print(f"  行{a}-{b}  {ts[b]-ts[a]:.1f}s  {side}侧")

    # ---- 请求与并线漏斗 ----
    vd = Counter((r.get("route") or {}).get("vd") for r in rows)
    why = Counter((r.get("route") or {}).get("why") for r in rows)
    print("route.vd:", dict(vd))
    print("route.why top:", why.most_common(8))
    layers = Counter(r.get("sel_layer") for r in rows)
    print("sel_layer:", dict(layers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
