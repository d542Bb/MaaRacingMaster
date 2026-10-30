# -*- coding: utf-8 -*-
"""只读回放：恒杆候选 vs 半程反打候选（2026-10-30 画龙实验）。

同一批拍的 scene/plan_ego 喂给两种候选集的 plan_motion（各自独立 gate），
比较：大误差反向打杆占比、杆值换向率、L4 保道段的预测回正/过冲、单拍耗时。
不跑游戏、不改生产配置。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_plan/probe_reverse_candidates.py \
        <trace.jsonl> [--out out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_plan import scene_of  # noqa: E402

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.motion_plan import (  # noqa: E402
    EgoState, PlanGate, plan_motion)


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") or r.get("scene") is None:
            continue
        out.append(r)
    return out


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--restore-gate", action="store_true",
                    help="按 trace 的 plan_gate 逐拍恢复闸门状态（对照产线）")
    args = ap.parse_args()
    cfg = load_decision()
    gate = PlanGate(cfg.motion.switch_margin, cfg.motion.switch_dwell_s,
                    cfg.motion.score_epsilon, cfg.motion.switch_geom_margin,
                    cfg.motion.switch_immediate_margin)
    rows = _rows(args.trace)
    last_gate = None
    times: list[float] = []
    away = tot_big = 0
    l4_damp = l4_move = 0
    l4_settle: list[float] = []
    l4_peak: list[float] = []
    u_hist: Counter = Counter()
    us_seq: list[float] = []
    cmd_sign_switch = 0
    prev_u = None
    center_miss = 0
    n = 0
    for r in rows:
        if r.get("state") != "CRUISE" or r.get("plan_ego") is None:
            continue
        scene = scene_of(r["scene"])
        if scene is None:
            continue
        n += 1
        ego = EgoState(*r["plan_ego"])
        if args.restore_gate and last_gate is not None:
            gate.restore(*last_gate)
        t0 = time.perf_counter()
        p = plan_motion(scene, ego, cfg.motion, cfg.selection, cfg.planner, gate)
        last_gate = (r.get("plan_gate") if r.get("plan_gate") is not None
                     else gate.snapshot())
        times.append((time.perf_counter() - t0) * 1000.0)
        if not p.valid:
            continue
        u = p.u_feedforward
        u_hist[round(u, 2)] += 1
        us_seq.append(u)
        if prev_u is not None and _sign(u) != 0 and _sign(prev_u) != 0 \
                and _sign(u) != _sign(prev_u):
            cmd_sign_switch += 1
        prev_u = u
        tgt = r.get("x_target")
        exe = r.get("executed_lane")
        if tgt is not None and exe is not None:
            err = tgt - exe
            if abs(err) > 0.5 and abs(u) > 0.1:
                tot_big += 1
                if u * err < 0:
                    away += 1
        if p.layer == 4 and p.chosen >= 0 and p.cand_x:
            center_m = scene.road.center_at(0.0) or 0.0
            xs = p.cand_x[p.chosen]
            x0 = xs[0]
            if abs(x0 - center_m) < 0.4 and abs(ego.v_lane_s) > 0.3:
                l4_move += 1
                if u * ego.v_lane_s < 0:
                    l4_damp += 1
            if abs(x0 - center_m) > 0.3:
                center_miss += 1
                l4_settle.append(abs(xs[-1] - center_m) / scene.lane_w_m)
                peak = max(xs, key=lambda x: abs(x - center_m))
                l4_peak.append(abs(peak - center_m) / scene.lane_w_m)
    segs = []
    cur = None
    for u in us_seq:
        if cur is None or abs(u - cur[0]) > 0.05:
            if cur is not None:
                segs.append(cur[1])
            cur = [u, 1]
        else:
            cur[1] += 1
    if cur is not None:
        segs.append(cur[1])
    dur = (rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9 if rows else 0.0
    out = {
        "rows": n,
        "cand_per_tick": len(p.cand_x) if n else 0,
        "away_share": (away / tot_big) if tot_big else None,
        "away_n": [away, tot_big],
        "cmd_sign_switch_per_min": (cmd_sign_switch / dur * 60) if dur else None,
        "l4_damp_share": (l4_damp / l4_move) if l4_move else None,
        "l4_damp_n": [l4_damp, l4_move],
        "l4_center_miss_ticks": center_miss,
        "l4_settle_lane_p50": _pct(l4_settle, 0.5),
        "l4_peak_lane_p50": _pct(l4_peak, 0.5),
        "u_hist": dict(sorted(u_hist.items())),
        "ms_p50": _pct(times, 0.5),
        "ms_p95": _pct(times, 0.95),
        "ms_p99": _pct(times, 0.99),
        "u_segs": len(segs),
        "u_seg_med": _pct([float(x) for x in segs], 0.5),
        "u_seg_short_share": (sum(1 for x in segs if x <= 2) / len(segs))
        if segs else None,
        "duration_s": round(dur, 1),
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
