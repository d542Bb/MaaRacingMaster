# -*- coding: utf-8 -*-
"""横向行为 A/B 对账（工作台，不入库）。

对两个会话流（基线 / 试验）出同一套横向指标：墙边占比、过目标偏差、
中心穿越、带内停留、墙边仍在向外打杆的比例，以及换道结局。用于
heading_fuse_gain 0→0.5 的效果判断，不替代人工看画面。

口径：
- 线横位统一加 ``lane_fuse.bias`` 映射到深度/路心帧（与 x_target 同系）；
  只用 TRACK 非预测拍。
- 墙边 = |x|>1.4 道（车道 ±1 之外进肩），>1.8 视为压墙。
- 过目标偏差 = |x - x_target|，只在两者都有值时统计。
- 中心穿越 = x 带 0.3 道死区的符号翻转次数 / 分钟。
- 换道结局复用 probe_merge_baseline 的段切分与实动口径。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_lateral_ab.py \
        --baseline 20261025_221703_1 --trial <新会话>
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter

import probe_merge_baseline as B
import probe_lateral_state as P


def row_adj(k):
    if k["x"] is None:
        return None
    return float(k["x"]) + float(k["bias"] or 0.0)


def game_metrics(rows):
    ticks = P.ticks_of(rows)
    xs = [(k["t"], row_adj(k), k["xt"], k["exec"], k["steer"])
          for k in ticks if row_adj(k) is not None]
    if not xs:
        return None
    walls = [x for _t, x, *_ in xs]
    hard = sum(1 for x in walls if abs(x) > 1.8)
    near = [i for i, (_t, x, *_r) in enumerate(xs) if abs(x) > 1.4]
    outward = 0
    for i in near:
        _t, x, _xt, _ex, u = xs[i]
        if u is not None and abs(float(u)) > 0.08 \
                and (float(u) > 0) == (x > 0):
            outward += 1
    dev = [abs(x - xt) for _t, x, xt, _e, _u in xs if xt is not None]
    band = sum(1 for d in dev if d <= 0.35)
    cross, sign = 0, 0
    for _t, x, *_r in xs:
        if abs(x) < 0.3:
            continue
        s = 1 if x > 0 else -1
        if sign and s != sign:
            cross += 1
        sign = s
    dur = max(1e-9, xs[-1][0] - xs[0][0])
    ctrl = [r.get("lat_ctrl") or {} for r in
            (d for d in rows if d.get("type") is None)]
    vh = sum(1 for c in ctrl if c.get("v_head") is not None)
    dv = [abs(float(c["v_ctrl"]) - float(c["v_model"]))
          for c in ctrl if c.get("v_ctrl") is not None
          and c.get("v_model") is not None]
    speeds = [c["speed"] for c in ctrl if c.get("speed") is not None]
    eps = B.merge_episodes(rows)
    real = [e for e in eps if B.outcome(e["vd"], e["why"]) == "arrived"
            and (e.get("moved") or 0.0) >= B.MOVE_TOL_LANE]
    return {
        "n": len(xs),
        "wall": sum(1 for x in walls if abs(x) > 1.4) / len(walls),
        "hard": hard / len(walls),
        "outward": outward / len(near) if near else 0.0,
        "dev_p50": statistics.median(dev) if dev else None,
        "dev_p90": (sorted(dev)[int(len(dev) * 0.9)] if dev else None),
        "in_band": band / len(dev) if dev else 0.0,
        "cross_per_min": cross / (dur / 60.0),
        "eps": len(eps), "arrived": len(real),
        "vh_cov": vh / max(1, len(ctrl)),
        "dv_p50": statistics.median(dv) if dv else None,
        "speed": statistics.median(speeds) if speeds else None,
    }


def agg(games):
    ms = [m for _l, m in games if m is not None]
    if not ms:
        return None
    def w(name, key):
        n = sum(m["n"] for m in ms)
        return sum(m[key] * m["n"] for m in ms) / max(1, n)
    def med(key):
        vals = [m[key] for m in ms if m[key] is not None]
        return statistics.median(vals) if vals else None
    return {
        "n": sum(m["n"] for m in ms),
        "wall": w("wall", "wall"), "hard": w("hard", "hard"),
        "outward": w("outward", "outward"),
        "dev_p50": med("dev_p50"), "dev_p90": med("dev_p90"),
        "in_band": w("in_band", "in_band"),
        "cross_per_min": statistics.median(
            [m["cross_per_min"] for m in ms]),
        "eps": sum(m["eps"] for m in ms),
        "arrived": sum(m["arrived"] for m in ms),
        "vh_cov": med("vh_cov"), "dv_p50": med("dv_p50"),
        "speed": med("speed"),
    }


def show(title, a):
    if a is None:
        print(f"| {title} | - | - | - | - | - | - | - | - | - | - |")
        return
    d50 = "-" if a["dev_p50"] is None else f"{a['dev_p50']:.2f}"
    d90 = "-" if a["dev_p90"] is None else f"{a['dev_p90']:.2f}"
    spd = "-" if a["speed"] is None else f"{a['speed']:.0f}"
    print(f"| {title} | {a['n']} | {a['wall']:.1%} | {a['hard']:.1%} | "
          f"{a['outward']:.0%} | {d50} | {d90} | {a['in_band']:.0%} | "
          f"{a['cross_per_min']:.1f} | {a['arrived']}/{a['eps']} | {spd} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--trial", required=True)
    ap.add_argument("--logs", default=P.DEFAULT_LOGS)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out = {}
    for name, tok in (("baseline", args.baseline), ("trial", args.trial)):
        games = [(label, game_metrics(rows))
                 for label, rows in P.load_session(args.logs, tok)]
        out[name] = (tok, games, agg(games))
    print("# 横向行为 A/B\n")
    print("| 会话 | ticks | 墙边>1.4 | 压墙>1.8 | 墙边仍外打 | "
          "过目标p50 | p90 | 带内 | 穿越/分 | 实动到达 | 速度中位 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for name in ("baseline", "trial"):
        tok, games, a = out[name]
        show(f"{name} `{tok}`", a)
        for label, m in games:
            if m is not None:
                show(f"  {label.split(':')[-1]}", m)
    b, t = out["baseline"][2], out["trial"][2]
    if b and t:
        print("\n## 差值（trial − baseline）\n")
        print(f"- 墙边占比 {t['wall'] - b['wall']:+.1%}，"
              f"压墙 {t['hard'] - b['hard']:+.1%}，"
              f"带内 {t['in_band'] - b['in_band']:+.1%}")
        if b["dev_p50"] is not None and t["dev_p50"] is not None:
            print(f"- 过目标偏差 p50 {t['dev_p50'] - b['dev_p50']:+.2f} 道，"
                  f"p90 {t['dev_p90'] - b['dev_p90']:+.2f} 道")
        print(f"- 穿越/分 {t['cross_per_min'] - b['cross_per_min']:+.1f}，"
              f"实动到达 {t['arrived'] - b['arrived']}"
              f"（{b['arrived']}→{t['arrived']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
