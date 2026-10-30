# -*- coding: utf-8 -*-
"""路线层时间门回放：同一批粗层报告，比较「见车就并」与「到站时间到线才并」。

背景（2026-10-30）：粗层净空只说明 24m 观察窗内有无车；`urgent` 用
`free_m / 45` 代理时间起意，窗内任何车都 ≤0.53s＝永远紧急。90s 自动局
21 次并线里 11 次车还在 1s 以外。本探针用 trace 的 `coarse`（原始逐道报告）、
`sel_dbg.req`（同拍换道请求）、`scene`（算车道到站时间）逐拍重放
`tick_route`，对照两种口径的并线次数与结局分布。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_plan/probe_route_ttc.py \
        <trace.jsonl> [--out out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from maaracing_master.plugins.speedrush.coarse import (  # noqa: E402
    CoarseLane, CoarseReport)
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.route import (  # noqa: E402
    RouteParams, tick_route)


def route_params(cfg) -> RouteParams:
    r, c, s = cfg.route, cfg.coarse, cfg.selection
    return RouteParams(
        switch_gain_m=c.switch_margin_m,
        proxy_speed_mps=c.range_speed_mps,
        urgent_confirm_ticks=r.urgent_confirm_ticks,
        opportunity_confirm_ticks=r.opportunity_confirm_ticks,
        cancel_unsafe_ticks=r.cancel_unsafe_ticks,
        cancel_cooldown_s=r.cancel_cooldown_s,
        finish_tol_lane=s.center_band_lane,
        finish_v_tol_lane_s=r.finish_v_tol_lane_s,
        merge_max_s=r.merge_max_s,
        soft_car=r.soft_car,
        urgent_ttc_s=r.urgent_ttc_s,
        urgent_blind_m=r.urgent_blind_m,
        merge_safe_ttc_s=r.merge_safe_ttc_s)


def load_rows(path: Path) -> list[dict]:
    rows = [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows = [r for r in rows if "type" not in r and "fid" in r]
    rows.sort(key=lambda r: r["fid"])
    return rows


def report_of(d: dict | None) -> CoarseReport | None:
    if not d or d.get("why") == "no_road" or d.get("cand") is None:
        return None
    lanes = tuple(CoarseLane(lane=k, free_m=free, width_margin_m=wm,
                             blocker=blk, unknown_car=bool(unk))
                  for k, free, wm, blk, unk in d["cand"])
    return CoarseReport(ego_lane=d.get("ego", 0.0), lane=d.get("lane", 0.0),
                        direction=d.get("dir", 0), free_m=d.get("free", 0.0),
                        reason=d.get("why", "keep"), lanes=lanes)


def lane_ttc_of(scene: dict | None, c0: float | None,
                horizon_s: float) -> dict[int, float]:
    out: dict[int, float] = {}
    if not scene or c0 is None:
        return out
    lw = scene.get("lane_w") or 3.5
    for o in scene.get("objs") or ():
        if o[0] != "car" or o[2] is None or not (0.0 < o[2] <= horizon_s):
            continue
        k = int(round((o[1] - c0) / lw))
        t = out.get(k)
        if t is None or o[2] < t:
            out[k] = o[2]
    return out


def run(rows: list[dict], p, horizon_s: float, use_ttc: bool):
    state = None
    verdicts = []
    merges = []
    for r in rows:
        rep = report_of(r.get("coarse"))
        scene = r.get("scene")
        c0 = None
        if scene is not None:
            bands = (scene.get("road") or {}).get("bands") or ()
            for b in bands:
                if b[0] <= 0.0 <= b[1] and b[2] is not None and b[3] is not None:
                    c0 = 0.5 * (b[2] + b[3])
                    break
        ttc = lane_ttc_of(scene, c0, horizon_s) if use_ttc else None
        req = (r.get("sel_dbg") or {}).get("req")
        epoch = (r.get("route") or {}).get("ep", 0)
        state, v = tick_route(state, rep, p, dt_s=float(r.get("dt") or 0.05),
                              epoch=epoch, request=req, lane_ttc=ttc)
        verdicts.append(v.verdict)
        if v.verdict == "merge_start":
            merges.append({"fid": r["fid"], "why": v.why, "ref": v.lane_ref,
                           "ttc": ttc})
    return verdicts, merges


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_decision()
    p = route_params(cfg)
    rows = load_rows(args.trace)
    horizon = cfg.motion.horizon_s
    out = {"trace": str(args.trace)}
    for name, use in (("legacy", False), ("ttc", True)):
        vs, merges = run(rows, p, horizon, use)
        out[name] = {
            "verdicts": dict(Counter(vs).most_common(10)),
            "merges": len(merges),
            "merge_why": dict(Counter(m["why"] for m in merges)),
            "merge_ref": dict(Counter(str(m["ref"]) for m in merges)),
        }
    # 新旧并线的拍号对照（哪些被时间门拦下）
    _, old = run(rows, p, horizon, False)
    _, new = run(rows, p, horizon, True)
    old_f = {m["fid"] for m in old}
    new_f = {m["fid"] for m in new}
    out["suppressed_fids"] = sorted(old_f - new_f)
    out["added_fids"] = sorted(new_f - old_f)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
