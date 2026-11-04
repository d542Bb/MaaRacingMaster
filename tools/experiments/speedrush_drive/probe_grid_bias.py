"""栅格/墙走廊路心偏差探针（2026-10-18 方案阶段 3）。

**要回答的问题**：栅格路心（ro_grid）与走廊/找边路心（ro_off / 墙间中心 /
深度边对中心）是否存在系统性偏差。若某来源 |中位偏差| > 0.2 道，说明栅格
一票否决可能在拿一个偏了的路心判墙——先归因，不新增 fudge 配置。

**口径**（全部换算为「自车相对路心」偏移，右正）：
- region：`ro_off`（master 供数主人，直接读）
- grid：`ro_grid`
- wall：`-wc_c`（wc_c 是墙间中心的车道坐标）
- edge：`-((dgeo_left + dgeo_right) / 2)`
偏差 = 来源 − region。样本只取双方都有值的拍。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_grid_bias.py
    ... [--root <debug/speedrush>] [--json]

输出：逐会话偏差中位/P90/符号一致率 + 是否需要标定核查（|中位|>0.2 道）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st
import sys
from pathlib import Path

_DEFAULT_ROOT = (Path(os.environ.get("APPDATA", "."))
                 / "MaaRacingMaster" / "debug" / "speedrush")
FLAG_LANE = 0.2


def _f(x):
    return None if x is None else round(float(x), 3)


def _stats(diffs: list[float]) -> dict:
    if not diffs:
        return {"n": 0, "median": None, "abs_p90": None, "sign_agree": None}
    med = st.median(diffs)
    abs_sorted = sorted(abs(d) for d in diffs)
    k = min(len(abs_sorted) - 1, int(round(0.9 * (len(abs_sorted) - 1))))
    same = sum(1 for d in diffs if (d >= 0) == (med >= 0))
    return {"n": len(diffs), "median": _f(med),
            "abs_p90": _f(abs_sorted[k]),
            "sign_agree": _f(same / len(diffs))}


def analyze(rows: list[dict]) -> dict:
    grid, wall, edge = [], [], []
    for r in rows:
        ro = r.get("ro_off")
        if ro is None:
            continue
        g = r.get("ro_grid")
        if g is not None:
            grid.append(g - ro)
        wc = r.get("wlc_c")
        if wc is None:
            wc = r.get("wc_c")          # 旧 trace 兼容
        if wc is not None:
            wall.append(-wc - ro)
        dl, dr = r.get("dgeo_left"), r.get("dgeo_right")
        if dl is not None and dr is not None:
            edge.append(-(dl + dr) / 2.0 - ro)
    out = {"n_rows": len(rows),
           "region_present_pct": _f(100.0 * sum(
               1 for r in rows if r.get("ro_off") is not None) / max(1, len(rows)))}
    for name, diffs in (("grid_minus_region", grid),
                        ("wall_minus_region", wall),
                        ("edge_minus_region", edge)):
        s = _stats(diffs)
        s["needs_calibration"] = bool(
            s["median"] is not None and abs(s["median"]) > FLAG_LANE)
        out[name] = s
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    sessions = sorted(p for p in args.root.glob("*")
                      if p.is_dir() and re.fullmatch(r"\d{8}_\d{6}(_p\d+)?", p.name)
                      and list(p.glob("trace_*.jsonl")))
    if not sessions:
        print(f"没有找到 trace_*.jsonl：{args.root}")
        return 1
    report = {}
    flagged = False
    for sess in sessions:
        rows = []
        for f in sorted(sess.glob("trace_*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        if len(rows) < 50:            # 合成/夹具会话不是对局样本
            continue
        m = analyze(rows)
        report[sess.name] = m
        print(f"\n[{sess.name}] rows={m['n_rows']} "
              f"region={m['region_present_pct']}%")
        for k in ("grid_minus_region", "wall_minus_region", "edge_minus_region"):
            s = m[k]
            print(f"  {k:22s} n={s['n']:4d} median={s['median']} "
                  f"|p90|={s['abs_p90']} same={s['sign_agree']} "
                  f"calib={'YES' if s['needs_calibration'] else 'no'}")
            flagged = flagged or s["needs_calibration"]

    print("\n结论：" + ("存在 |中位偏差|>0.2 道的来源，进入标定核查"
                        if flagged else
                        "各来源中位偏差均在 ±0.2 道内，不做几何改动"))
    if args.json:
        out = args.root / "probe_grid_bias.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"JSON → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
