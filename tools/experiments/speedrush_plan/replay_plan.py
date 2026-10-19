# -*- coding: utf-8 -*-
"""离线计划回放：读带 `scene/plan` 列的控制 trace，重跑 plan_motion 并对照。

用途（motion_plan A2/A3）：
- 确定性：同一 Scene/Ego 逐拍重跑，与录制计划逐拍对齐；
- 覆盖率：CRUISE 且有场景的拍里 `plan.valid` 比例；
- 层分布、金币层占比、预测碰撞全灭拍、预览抖动、单拍耗时。

gate 状态按 trace 顺序回放（与在线同构）；输出 JSON 摘要。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_plan/replay_plan.py \
        <trace.jsonl> [--out summary.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.motion_plan import (
    EgoState, PlanGate, Scene, SceneObj, plan_motion,
    shift_scene)  # noqa: E402
from maaracing_master.plugins.speedrush.selection import (  # noqa: E402
    RoadBand, RoadFrame)  # noqa: E402


def load_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows.sort(key=lambda r: r["fid"])
    return rows


def scene_of(d: dict) -> Scene | None:
    if not d:
        return None
    rd = d.get("road") or {}
    bands = tuple(RoadBand(z0, z1, l, r, src=src, age_ms=age, conf=conf)
                  for z0, z1, l, r, src, age, conf in rd.get("bands") or ())
    road = RoadFrame(bands, rd.get("w"), rd.get("l"), rd.get("r"), True)
    objs = tuple(SceneObj(kind=x[0], x_m=x[1], t_arrive_s=x[2],
                          count=x[3], sigma_m=x[4], age_ms=x[5],
                          src=x[6], z_m=x[7])
                 for x in d.get("objs") or ())
    return Scene(d.get("schema", 1), d["fid"], d["ego_m"], road, objs,
                 d["lane_w"], d["frame_age_ms"], d.get("corridor_age_ms"),
                 bool(d.get("stale")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_decision()
    gate = PlanGate(cfg.motion.switch_margin, cfg.motion.switch_dwell_s,
                    cfg.motion.score_epsilon, cfg.motion.switch_geom_margin)
    rows = load_rows(args.trace)
    layers = Counter()
    reasons = Counter()
    n_scene = n_valid = n_ok_cmp = n_bad_cmp = 0
    prev_x = None
    prev_ts = None
    churn = 0
    prev_intent = None
    intent_churn = 0
    times = []
    prev = None
    last_gate = None
    for r in rows:
        if r.get("state") != "CRUISE" or r.get("scene") is None:
            prev = r
            continue
        scene = scene_of(r["scene"])
        if not scene:
            prev = r
            continue
        # schema 1 的旧 trace 场景还在走廊带帧：按录制 road_offset 平移到
        # 规划器路心帧，口径与新代码一致（schema 2 已在线平移）。
        if scene.schema < 2 and r.get("road_offset") is not None:
            scene = shift_scene(
                scene, float(r["road_offset"]) * scene.lane_w_m
                - scene.ego_x_m)
        # stale 用当前配置重算（录制时的阈值可能已改）
        ca = scene.corridor_age_ms
        scene = replace(scene, stale=ca is not None
                        and ca > cfg.motion.corridor_age_max_s * 1000.0)
        n_scene += 1
        if last_gate is not None:
            gate.restore(*last_gate)
        if r.get("plan_ego") is not None:
            ego = EgoState(*r["plan_ego"])
        else:
            src = prev if prev is not None else r
            ego = EgoState(float(src["executed_lane"]),
                           float(src["v_lat_est"]),
                           float(src["steer_norm"]))
        t0 = time.perf_counter()
        p = plan_motion(scene, ego, cfg.motion, cfg.selection, cfg.planner, gate)
        times.append((time.perf_counter() - t0) * 1000.0)
        if r.get("plan_gate") is not None:
            last_gate = tuple(r["plan_gate"])
        else:
            last_gate = gate.snapshot()
        n_valid += int(p.valid)
        layers[p.layer] += 1
        reasons[p.reason] += 1
        rec = r.get("plan")
        if rec is not None:
            same = (p.valid == rec["valid"]
                    and p.layer == rec.get("layer", p.layer)
                    and abs(p.x_preview_m - rec["x"]) <= 1e-3
                    and abs(p.u_feedforward - rec["u"]) <= 1e-3)
            n_ok_cmp += int(same)
            n_bad_cmp += int(not same)
        gap_s = (r["ts_ns"] - prev_ts) / 1e9 if prev_ts is not None else None
        adjacent = gap_s is not None and gap_s <= 0.1
        if adjacent and abs(p.x_preview_m - prev_x) > 0.2:
            churn += 1
        prev_x = p.x_preview_m
        intent = p.x_preview_lane - ego.x_lane
        if adjacent and abs(intent - prev_intent) > 0.2:
            intent_churn += 1
        prev_intent = intent
        prev_ts = r["ts_ns"]
        prev = r
    dur = (rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9 if rows else 0.0
    times.sort()
    summary = {
        "rows": len(rows), "scene_rows": n_scene,
        "valid_rate": (n_valid / n_scene) if n_scene else None,
        "layers": dict(sorted(layers.items())),
        "reasons": dict(reasons),
        "replay_match": (n_ok_cmp / (n_ok_cmp + n_bad_cmp))
        if (n_ok_cmp + n_bad_cmp) else None,
        "churn_0p2_per_s": (churn / dur) if dur > 0 else None,
        "intent_churn_0p2_per_s": (intent_churn / dur) if dur > 0 else None,
        "p99_ms": times[int(0.99 * (len(times) - 1))] if times else None,
        "duration_s": round(dur, 1),
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
