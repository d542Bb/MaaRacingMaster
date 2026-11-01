# -*- coding: utf-8 -*-
"""两局对照：基线局 vs 新局，逐项列差值（第 5 期验收的一键入口）。

复用同目录两个分析器（baseline_replay / map_support），不重写口径。
输出只做对照，不设通过阈值——阈值与判据写在方案第 5 步，由人按局裁定。

用法::

    python tools/experiments/world_state/acceptance.py \
        --baseline 20261101_113145 --new 20261101_xxxxxx [--new ...] \
        [--json out.json]

旧局没有 `v_ego`/`otab`/`map` 时，对应项显示 `None`，不拿缺列当零。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import baseline_replay as B  # noqa: E402
import map_support as M  # noqa: E402
from speedrush_trace_reader import iter_rows  # noqa: E402


def _path(session: str) -> Path:
    import os
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def analyze(session: str, speed="v_ego", speed_value=41.0) -> dict:
    rows = list(iter_rows(_path(session)))
    if not rows:
        raise SystemExit(f"没有数据行：{session}")
    b = {
        "plan": B._plan(rows),
        "layers": B._layers(rows),
        "merges": B._merges(rows),
        "requests": B._requests(rows),
        "sources": B._source_flips(rows),
        "coins": B._coin_life(rows),
        "car_tracks": B._car_tracks(rows),
        "group_blind": B._group_blind(rows),
        "opps": B._opps(rows),
        "steer": B._steer(rows),
        "map": M.analyze(rows, session, speed, speed_value),
        "has_new_columns": all(
            k in rows[-1] for k in
            ("otab", "map", "open", "anchor", "h_shadow", "trig", "v_ego")),
    }
    return b


def flat(rep: dict) -> dict:
    p, ly, m, r, s = (rep["plan"], rep["layers"], rep["merges"],
                      rep["requests"], rep["steer"])
    mp = rep["map"]
    e = mp["err_by_distance"]
    return {
        "plan.valid_rate": p["valid_rate"],
        "plan.chosen_switches": p["chosen_switches"],
        "plan.layer_switches": p["layer_switches"],
        "plan.no_speed_s": p["no_speed_s"],
        "layers.switches": ly["switches"],
        "merges.count": m["count"],
        "merges.opportunity": m["opportunity"],
        "merges.v_lat_median": m["v_lat_lane_s_median"],
        "merges.overshoot_p90": m["overshoot_lane_p90"],
        "merges.settle_p90_finished": m["settle_err_lane_p90_finished"],
        "requests.segments": r["segments"],
        "requests.life_s_median": r["life_s_median"],
        "requests.conversion": r["conversion"],
        "requests.conversion_any": r["conversion_any"],
        "sources.switch_per_s": rep["sources"]["per_s"],
        "sources.visible_s": rep["sources"]["visible_s"],
        "steer.in_band": s["in_band_abs_mean"],
        "steer.reverse_s": s["reverse_per_s"],
        "coins.life_median": rep["coins"]["life_s_median"],
        "cgroups.blind_p90": rep["group_blind"]["blind_s_p90"],
        "cgroups.blind_over_0.6s": rep["group_blind"]["blind_over_0_6s"],
        "cars.occl_p90": rep["car_tracks"]["occl_max_age_s_p90"],
        "cars.occl_over_0.6s": rep["car_tracks"]["runs_over_0_6s"],
        "cars.blind_s": rep["car_tracks"]["blind_s"],
        "opps.coin_slack_median": rep["opps"]["coin_slack_median"],
        "map.support_median": mp["support_m"]["median"],
        "map.err_0-10m": e["0-10m"]["abs_median"],
        "map.err_10-20m": e["10-20m"]["abs_median"],
        "map.err_20-30m": e["20-30m"]["abs_median"],
        "map.forward_speed": mp["forward_speed"],
        "trace.new_columns": rep["has_new_columns"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", default="20261101_113145")
    ap.add_argument("--new", action="append", default=[])
    ap.add_argument("--json")
    ap.add_argument("--speed", choices=("v_ego", "hud41", "const"),
                    default="v_ego")
    ap.add_argument("--speed-value", type=float, default=41.0)
    ap.add_argument("--gate", action="store_true",
                    help="按方案第 5 期判据逐项判 PASS/FAIL；任一 FAIL 退出 1")
    args = ap.parse_args(argv)
    base = flat(analyze(args.baseline, args.speed, args.speed_value))
    news = {s: flat(analyze(s, args.speed, args.speed_value))
            for s in args.new}
    rep = {"baseline": args.baseline,
           "baseline_metrics": base,
           "new": {s: v for s, v in news.items()}}
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    keys = list(base)
    print(f"{'metric':30s} {'base':>10s}  " + "  ".join(
        f"{s[-6:]:>10s}" for s in news))
    for k in keys:
        if k in ("map.forward_speed", "trace.new_columns"):
            print(f"{k:30s} {str(base[k])[:10]:>10s}  " + "  ".join(
                str(news[s][k])[:10].rjust(10) for s in news))
            continue

        def fmt(v):
            return "None" if v is None else f"{v:.3f}"

        print(f"{k:30s} {fmt(base[k]):>10s}  " + "  ".join(
            fmt(news[s][k]).rjust(10) for s in news))
    if args.gate:
        return _gate(base, news)
    return 0


def _gate(base: dict, news: dict) -> int:
    """方案第 5 期的硬判据；容差只吸收测量噪声，不是放宽目标。"""
    rules = [
        ("plan.valid_rate", ">=", -0.03, "计划有效率不降 3pt"),
        ("plan.no_speed_s", "<=", 1.0, "no_speed 不增 1s"),
        ("steer.in_band", "<=", 0.02, "巡航目标内杆量不增 0.02"),
        ("steer.reverse_s", "<=", 0.15, "反向率不增 0.15/s"),
        ("merges.overshoot_p90", "<=", 0.10, "并线过冲 p90 不增 0.10 道"),
        ("requests.conversion", ">=", 0.0, "请求→并线转化（仅 opportunity）不低于基线"),
    ]
    bad = 0
    for sess, new in news.items():
        fails = []
        for key, op, tol, why in rules:
            b, n = base.get(key), new.get(key)
            if b is None or n is None:
                fails.append(f"{key}: 基线或新局缺值")
                continue
            limit = b + tol
            ok = n <= limit if op == "<=" else n >= limit
            if not ok:
                fails.append(f"{key}: {n:.3f} vs 基线 {b:.3f}（{why}）")
        if not new.get("trace.new_columns"):
            fails.append("trace.new_columns: 会话缺 otab/map/open/anchor/"
                         "h_shadow/trig/v_ego，不是新版本产物")
        speed = str(new.get("map.forward_speed") or "")
        if not speed.startswith("v_ego"):
            fails.append("map.forward_speed: 新局缺精确 v_ego（不接受代理跑验收）")
        if fails:
            bad += 1
            print(f"[FAIL] {sess}")
            for f in fails:
                print("  - " + f)
        else:
            print(f"[PASS] {sess}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
