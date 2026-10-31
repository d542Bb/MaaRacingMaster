# -*- coding: utf-8 -*-
"""车宽口径只读回放：同一批场景，换 width 口径重跑 plan_motion 并对照。

背景（2026-10-30）：`traffic._refresh_width` 用框**中心行**做地面尺分母
（`cy - y_h`），而地面尺的物理分母是**框底行**（`y2 - y_h`）。透视下
分母少了半个框高，车宽被系统性放大约 2 倍（三局 trace 实测中位半宽
1.72m，真车约 0.9m）。后果：`graze_max_width_m=2.2` 把 83~93% 的车判成
「大车」，擦边/超车价值层（L2）几乎全灭。

本探针不动生产，只回答三件事：
1. 每股 trace 里车的宽度分布，以及「大车」占比；
2. 换成框底行分母（按帧内检测框中位系数折算）、或回退标称半宽后，
   计划层 L2/L1 出现多少拍、碰撞代价 `cc>0` 的拍减少多少；
3. 选定候选的横位是否因此从「躲」变成「贴邻道过」。

口径：
  recorded  录制的 half_w_m（现状）
  bottom    half_w_m × 帧内中位系数 (cy-y_h)/(y2-y_h)
  clamped   bottom 结果再夹到 [0.7, 1.25]m，越界回落标称（None）
  nominal   全部 None（计划层用 car_half_lane 标称半宽）

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_plan/probe_width_replay.py \
        <trace.jsonl> [--limit N] [--out summary.json]
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.motion_plan import (  # noqa: E402
    EgoState, PlanGate, plan_motion, shift_scene)  # noqa: E402
from replay_plan import scene_of  # noqa: E402

Y_H = 354.2
GRAZE_MAX_W = 2.2
HALF_CLAMP = (0.7, 1.25)


def load_rows(path: Path) -> list[dict]:
    rows = [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows = [r for r in rows if "type" not in r and "fid" in r]
    rows.sort(key=lambda r: r["fid"])
    return rows


def row_factor(r: dict) -> float:
    """帧内检测框的底行/中心行分母比中位数；无可用框回落 0.5。"""
    hs = {c["id"]: c.get("h") for c in (r.get("car_h") or [])}
    fac = []
    for cp in (r.get("car_pos") or []):
        h = hs.get(cp.get("id"))
        cy = cp.get("cy")
        if not h or cy is None:
            continue
        den = cy - Y_H
        if den < 20.0:
            continue
        f = den / (cy + h / 2.0 - Y_H)
        if 0.2 < f < 1.0:
            fac.append(f)
    return st.median(fac) if fac else 0.5


def apply_policy(scene, policy: str, factor: float):
    if policy == "recorded":
        return scene

    def conv(h):
        if h is None:
            return None
        if policy == "nominal":
            return None
        v = h * factor
        if policy == "clamped":
            if v < HALF_CLAMP[0] or v > HALF_CLAMP[1]:
                return None
        return v

    return replace(scene, objs=tuple(
        replace(o, half_w_m=conv(o.half_w_m)) if o.kind == "car" else o
        for o in scene.objs))


def replay(rows, policy: str, cfg):
    gate = PlanGate(cfg.motion.switch_margin, cfg.motion.switch_dwell_s,
                    cfg.motion.score_epsilon, cfg.motion.switch_geom_margin,
                    cfg.motion.switch_immediate_margin)
    out = []
    last_gate = None
    prev = None
    for r in rows:
        if r.get("state") != "CRUISE" or r.get("scene") is None:
            prev = r
            continue
        scene = scene_of(r["scene"])
        if scene is None:
            prev = r
            continue
        if scene.schema < 2 and r.get("road_offset") is not None:
            scene = shift_scene(
                scene, float(r["road_offset"]) * scene.lane_w_m
                - scene.ego_x_m)
        ca = scene.corridor_age_ms
        scene = replace(scene, stale=ca is not None
                        and ca > cfg.motion.corridor_age_max_s * 1000.0)
        scene = apply_policy(scene, policy, row_factor(r))
        if last_gate is not None:
            gate.restore(*last_gate)
        if r.get("plan_ego") is not None:
            ego = EgoState(*r["plan_ego"])
        else:
            src = prev if prev is not None else r
            ego = EgoState(float(src["executed_lane"]),
                           float(src["v_lat_est"]),
                           float(src["steer_norm"]))
        p = plan_motion(scene, ego, cfg.motion, cfg.selection, cfg.planner,
                        gate, intent_lane=float(r.get("x_target") or 0.0))
        last_gate = (tuple(r["plan_gate"]) if r.get("plan_gate") is not None
                     else gate.snapshot())
        # 通过带是否非空：ego_half + half + sigma + margin <= graze
        ego_half = cfg.selection.ego_half_lane * scene.lane_w_m
        graze = cfg.motion.graze_gap_lane * scene.lane_w_m
        band_cars = 0
        large_cars = 0
        late_skip = 0
        for o in scene.objs:
            if o.kind != "car" or not o.in_corridor:
                continue
            h = o.half_w_m
            if h is not None and h * 2.0 > GRAZE_MAX_W:
                large_cars += 1
            hv = h if h is not None else cfg.selection.car_half_lane \
                * scene.lane_w_m
            lo = ego_half + hv + o.sigma_m + cfg.motion.collision_margin_m
            if lo <= graze and o.timed and o.t_arrive_s is not None \
                    and cfg.motion.hit_min_lead_s <= o.t_arrive_s \
                    <= cfg.motion.horizon_s:
                band_cars += 1
            # late_block 的「已重叠」豁免：0.35s 内到站且横距 ≤ 车身和+余量
            # 的车，规划层不把它当「不许往那侧并」的理由
            if o.timed and o.t_arrive_s is not None \
                    and 0.0 < o.t_arrive_s < cfg.motion.collision_min_lead_s \
                    and abs(o.x_m - scene.ego_x_m) <= lo:
                late_skip += 1
        out.append({
            "fid": r["fid"], "valid": p.valid, "layer": p.layer,
            "cc": p.collision_cost, "val": p.value_total,
            "x": p.x_preview_m, "n_ok": p.n_feasible,
            "band_cars": band_cars, "large_cars": large_cars,
            "late_skip": late_skip,
            "why": p.reason,
        })
        prev = r
    return out


def summarize(name: str, rec: list[dict]) -> dict:
    n = len(rec)
    if not n:
        return {"rows": 0}
    layers = Counter(r["layer"] for r in rec if r["valid"])
    return {
        "rows": n,
        "valid": sum(1 for r in rec if r["valid"]),
        "layers": dict(sorted(layers.items())),
        "cc_pos_rows": sum(1 for r in rec if r["cc"] > 0),
        "band_rows": sum(1 for r in rec if r["band_cars"] > 0),
        "band_cars": sum(r["band_cars"] for r in rec),
        "large_cars": sum(r["large_cars"] for r in rec),
        "late_skip_rows": sum(1 for r in rec if r["late_skip"] > 0),
        "late_skip_cars": sum(r["late_skip"] for r in rec),
        "x_mean_abs": round(st.fmean(abs(r["x"]) for r in rec), 3),
        "why": dict(Counter(r["why"] for r in rec).most_common(6)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_decision()
    rows = load_rows(args.trace)
    if args.limit:
        rows = rows[: args.limit]
    res = {pol: replay(rows, pol, cfg)
           for pol in ("recorded", "bottom", "clamped", "nominal")}
    summ = {pol: summarize(pol, v) for pol, v in res.items()}
    flips = Counter()
    for a, b in zip(res["recorded"], res["bottom"]):
        if not (a["valid"] and b["valid"]):
            continue
        if a["layer"] != b["layer"]:
            flips[f"{a['layer']}->{b['layer']}"] += 1
        if a["cc"] > 0 and b["cc"] == 0:
            flips["cc_cleared"] += 1
        if abs(a["x"] - b["x"]) > 0.5 * 3.5:
            flips["x_shift>0.5lane"] += 1
    out = {"trace": str(args.trace), "summary": summ,
           "recorded_vs_bottom": dict(flips)}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
