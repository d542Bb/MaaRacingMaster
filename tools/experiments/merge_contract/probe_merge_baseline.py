# -*- coding: utf-8 -*-
"""横向控制行为契约——基线复算脚本（工作台，不入库）。

用法：
  .venv\\Scripts\\python.exe docs/plan/merge_baseline.py
  .venv\\Scripts\\python.exe docs/plan/merge_baseline.py --data <debug/speedrush 目录>
  .venv\\Scripts\\python.exe docs/plan/merge_baseline.py --runs a,b,c

口径：
- 换道段 = route.phase 连续为 merging 的拍；结束拍取离开 merging 的那一拍。
- 成功只认 (verdict=finish, why=arrived)；merge_timeout 单列，不算成功。
- cancel/rebuild 分别统计；能定位目标道的，按 coarse.cand 的 blocker 再分
  car / space / unknown_car / 缺失。
- 时长分两套：到达段（arrived）与全部段（all），不混用。
- 计划/PD 一致性只在 plan.valid 且 maneuver=true 的拍上算；PD 为按
  x_target/executed/v_lat_est 复算的机动 PD（0.7/0.2，前瞻 0.25s，上限 0.85）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

DEFAULT_DATA = os.environ.get(
    "SPEEDRUSH_DEBUG",
    r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/speedrush",
)
DEFAULT_RUNS = [
    "20261025_162933_p1",
    "20261025_163017_p2",
    "20261025_164658_p1",
    "20261025_164743_p2",
]


def load(run, data_dir):
    path = os.path.join(data_dir, run, f"trace_{run}.jsonl")
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def pd_maneuver(x_target, executed, v):
    e = x_target - (executed + v * 0.25)
    if abs(e) < 0.05:
        e = 0.0
    return max(-0.85, min(0.85, 0.7 * e - 0.2 * v))


def outcome(vd, why):
    if vd == "finish":
        return "arrived" if why == "arrived" else "timeout"
    if vd == "timeout":
        return "timeout"
    if vd == "cancel":
        return "cancel"
    if vd == "rebuild":
        return "rebuild"
    return "unfinished"


def blocker_at(row, lane):
    """优先读路线 verdict 的 blk（阶段 0 新列）；旧 trace 回退到 coarse.cand。

    blk 由路线层在结束拍写入，语义=目标/参考道的粗层 blocker；coarse.cand
    只用于补自由空间，旧 trace 则完全回退。
    """
    blk = (row.get("route") or {}).get("blk")
    for c in (row.get("coarse") or {}).get("cand") or []:
        if lane is not None and abs(c[0] - lane) < 1e-6:
            return (blk if blk is not None else c[3]), c[1]
    return (blk if blk is not None else "missing"), None


def merge_episodes(rows):
    eps, cur = [], None
    for r in rows:
        ro = r.get("route") or {}
        if ro.get("ph") == "merging":
            if cur is None:
                cur = {"ticks": 0, "target": ro.get("tgt")}
            cur["ticks"] += 1
        elif cur is not None:
            cur["vd"], cur["why"] = ro.get("vd"), ro.get("why")
            cur["blocker"], cur["free"] = blocker_at(r, cur["target"])
            eps.append(cur)
            cur = None
    if cur is not None:
        cur.update(vd=None, why=None, blocker=None, free=None)
        eps.append(cur)
    return eps


def _blank():
    return {"ticks": 0, "plan_valid": 0, "maneuver": 0, "both": 0,
            "layer4": 0, "unc": 0, "fd": [], "ratio": [], "same": 0,
            "sign_n": 0}


def _finish(st):
    n = st["plan_valid"]
    return {
        "ticks": st["ticks"],
        "plan_valid": n,
        "maneuver": st["maneuver"],
        "both": st["both"],
        "layer4_share": (st["layer4"] / n if n else 0.0),
        "unc_share": (st["unc"] / n if n else 0.0),
        "fd_p90": (sorted(st["fd"])[int(len(st["fd"]) * 0.9)]
                   if st["fd"] else None),
        "plan_pd_ratio_p50": (statistics.median(st["ratio"])
                              if st["ratio"] else None),
        "sign_agree": (st["same"] / st["sign_n"] if st["sign_n"] else None),
        "sign_n": st["sign_n"],
    }


def authority_stats(rows):
    out = {"all": _blank(), "merging": _blank(), "keep": _blank()}
    for r in rows:
        p = r.get("plan")
        m = bool(r.get("maneuver"))
        ph = "merging" if (r.get("route") or {}).get("ph") == "merging" else "keep"
        for st in (out["all"], out[ph]):
            st["ticks"] += 1
            st["maneuver"] += m
            if not (p and p.get("valid")):
                continue
            st["plan_valid"] += 1
            st["layer4"] += p.get("layer") == 4
            st["unc"] += bool(p.get("unc"))
            lw = (r.get("scene") or {}).get("lane_w") or 3.38
            st["fd"].append(abs(p.get("fd") or 0.0) / lw)
            if not m:
                continue
            st["both"] += 1
            xt, ex = r.get("x_target"), r.get("executed_lane")
            v = r.get("v_lat_est") or 0.0
            if xt is None or ex is None:
                continue
            a = pd_maneuver(xt, ex, v)
            b = p.get("u") or 0.0
            if abs(a) > 0.05:
                st["ratio"].append(abs(b) / abs(a))
            if abs(a) > 0.05 and abs(b) > 0.05:
                st["sign_n"] += 1
                st["same"] += (a > 0) == (b > 0)
    return {k: _finish(v) for k, v in out.items()}


def fusion_stats(rows):
    eps, cur = [], None
    for i, r in enumerate(rows):
        fresh = (r.get("road_offset") is not None
                 and r.get("dgeo_new") and not r.get("ro_hold"))
        if not fresh:
            if cur is None:
                cur = {"n": 0, "trusted": 0}
            cur["n"] += 1
            if (r.get("lane_fuse") or {}).get("trusted"):
                cur["trusted"] += 1
        else:
            if cur is not None and cur["n"] >= 3:
                cur["depth"] = r["road_offset"]
                cur["exec"] = rows[i - 1].get("executed_lane")
                eps.append(cur)
            cur = None
    out = {}
    for name, sel in (("trusted", lambda e: e["trusted"] >= e["n"] * 0.5),
                      ("off", lambda e: e["trusted"] < e["n"] * 0.5)):
        es = [e for e in eps if sel(e) and e.get("exec") is not None]
        errs = [abs(e["exec"] - e["depth"]) for e in es]
        out[name] = {
            "n": len(es),
            "err_p50": statistics.median(errs) if errs else None,
            "err_p90": (sorted(errs)[int(len(errs) * 0.9)] if errs else None),
        }
    return out


def route_vs_coarse(rows):
    same = diff = 0
    for r in rows:
        c = r.get("coarse") or {}
        ref = (r.get("route") or {}).get("ref")
        lane = c.get("lane")
        if lane is None or ref is None:
            continue
        if abs(lane - ref) < 1e-6:
            same += 1
        else:
            diff += 1
    return {"same": same, "diff": diff}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--runs", default=",".join(DEFAULT_RUNS))
    args = ap.parse_args()
    runs = [r.strip() for r in args.runs.split(",") if r.strip()]
    if not os.path.isdir(args.data):
        print(f"数据目录不存在：{args.data}\n用 --data 指向 debug/speedrush 目录。")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    all_eps, blocked = [], Counter()
    agg = defaultdict(list)
    print("# 横向控制基线（trace 口径）\n")
    print("| 局 | ticks | 换道段 | arrived | timeout | cancel | rebuild | "
          "未结束 | 到达时长(拍) | 全部时长(拍) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for run in runs:
        rows = load(run, args.data)
        eps = merge_episodes(rows)
        all_eps.extend(eps)
        c = Counter(outcome(e["vd"], e["why"]) for e in eps)
        ad = [e["ticks"] for e in eps if outcome(e["vd"], e["why"]) == "arrived"]
        al = [e["ticks"] for e in eps]
        for e in eps:
            if e["vd"] in ("cancel", "rebuild"):
                blocked[(e["vd"], e["why"], e["blocker"])] += 1
        print(f"| {run} | {len(rows)} | {len(eps)} | {c['arrived']} | "
              f"{c['timeout']} | {c['cancel']} | {c['rebuild']} | "
              f"{c['unfinished']} | "
              f"{'p50 %.0f / max %d' % (statistics.median(ad), max(ad)) if ad else '-'} | "
              f"p50 {statistics.median(al):.0f} / max {max(al)} |")
        a = authority_stats(rows)
        agg["authority"].append(a)
        agg["fusion"].append(fusion_stats(rows))
        agg["route"].append(route_vs_coarse(rows))
        print(f"  - 计划有效 {a['all']['plan_valid']} 拍，"
              f"机动+计划 {a['all']['both']} 拍，"
              f"layer4 占计划 {a['all']['layer4_share']:.0%}，"
              f"计划不确定 {a['all']['unc_share']:.0%}，"
              f"帧差 p90 {a['all']['fd_p90']:.2f} 道")
        print(f"  - 并线拍 {a['merging']['ticks']}：plan/PD 杆值比 p50 "
              f"{a['merging']['plan_pd_ratio_p50']:.2f}，"
              f"方向一致 {a['merging']['sign_agree']:.0%}"
              f"（n={a['merging']['sign_n']}）")

    c = Counter(outcome(e["vd"], e["why"]) for e in all_eps)
    n = len(all_eps)
    print("\n## 汇总\n")
    print(f"- 换道段 {n}：arrived {c['arrived']} ({c['arrived']/n:.1%})，"
          f"timeout {c['timeout']}，cancel {c['cancel']}，"
          f"rebuild {c['rebuild']}，未结束 {c['unfinished']}")
    print(f"- 结束原因：{dict(Counter((e['vd'], e['why']) for e in all_eps))}")
    print(f"- 撤销/作废的目标道 blocker：{dict(blocked)}")
    print(f"- 其中 car 相关："
          f"{sum(v for (_vd, _why, b), v in blocked.items() if b == 'car')}，"
          f"unknown_car："
          f"{sum(v for (_vd, _why, b), v in blocked.items() if b == 'unknown_car')}，"
          f"missing："
          f"{sum(v for (_vd, _why, b), v in blocked.items() if b == 'missing')}")
    for key in ("trusted", "off"):
        vals = [a[key] for a in agg["fusion"]]
        print(f"- 线融合 {key}：n={sum(v['n'] for v in vals)}，"
              f"保持段末误差 p50 "
              f"{statistics.median([v['err_p50'] for v in vals if v['err_p50'] is not None]):.2f} 道，"
              f"p90 "
              f"{statistics.median([v['err_p90'] for v in vals if v['err_p90'] is not None]):.2f} 道")
    print(f"- 路线参考道 vs 粗层选道："
          f"same {sum(a['same'] for a in agg['route'])} / "
          f"diff {sum(a['diff'] for a in agg['route'])}")
    for ph in ("all", "merging"):
        vals = [a[ph] for a in agg["authority"]]
        sn = sum(v["sign_n"] for v in vals)
        print(f"- {ph} 计划/PD 方向一致样本 n={sn}，一致率 "
              f"{sum((v['sign_agree'] or 0) * v['sign_n'] for v in vals) / max(1, sn):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
