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


def analyze(session: str) -> dict:
    rows = list(iter_rows(_path(session)))
    if not rows:
        raise SystemExit(f"没有数据行：{session}")
    b = {
        "plan": B._plan(rows),
        "layers": B._layers(rows),
        "merges": B._merges(rows),
        "requests": B._requests(rows),
        "coins": B._coin_life(rows),
        "opps": B._opps(rows),
        "steer": B._steer(rows),
        "map": M.analyze(rows, session),
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
        "merges.v_lat_median": m["v_lat_lane_s_median"],
        "merges.overshoot_p90": m["overshoot_lane_p90"],
        "merges.settle_p90_finished": m["settle_err_lane_p90_finished"],
        "requests.segments": r["segments"],
        "requests.conversion": r["conversion"],
        "steer.in_band": s["in_band_abs_mean"],
        "steer.reverse_s": s["reverse_per_s"],
        "coins.life_median": rep["coins"]["life_s_median"],
        "opps.coin_slack_median": rep["opps"]["coin_slack_median"],
        "map.support_median": mp["support_m"]["median"],
        "map.err_0-10m": e["0-10m"]["abs_median"],
        "map.err_10-20m": e["10-20m"]["abs_median"],
        "map.err_20-30m": e["20-30m"]["abs_median"],
        "map.forward_speed": mp["forward_speed"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", default="20261101_113145")
    ap.add_argument("--new", action="append", default=[])
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    base = flat(analyze(args.baseline))
    news = {s: flat(analyze(s)) for s in args.new}
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
        if k == "map.forward_speed":
            print(f"{k:30s} {str(base[k])[:10]:>10s}  " + "  ".join(
                str(news[s][k])[:10].rjust(10) for s in news))
            continue

        def fmt(v):
            return "None" if v is None else f"{v:.3f}"

        print(f"{k:30s} {fmt(base[k]):>10s}  " + "  ".join(
            fmt(news[s][k]).rjust(10) for s in news))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
