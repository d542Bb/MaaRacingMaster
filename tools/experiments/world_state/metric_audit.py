# -*- coding: utf-8 -*-
"""门禁指标审计：这些数到底在量行为，还是在量「开得稳不稳」。

用户质疑（2026-11-01）：`no_speed` 的来源是「标线横移 ÷ tanψ」，直线巡航时
横移和朝角都小、估不出速度，于是越稳越容易记成 no_speed——用它做「不增 1s」
的闸门，可能是在惩罚好行为。本脚本不改生产，只把三个控制类指标拆开看：

1. **no_speed 的场景构成**：并线中 / 正前方 30 m 内有同车道车 / 有活跃请求 /
   其余（纯巡航）。最后一类是「速度不参与决策」的部分，`no_speed` 闸门若被
   它主导，就不是行为退化的证据。
2. **no_speed 是否伴随更差的车道保持**：keep 态实测道与承诺道的偏差、杆值
   饱和占比、反向率，在 no_speed 拍与其余拍之间对照。
3. **杆量与反向率是否被赛道带偏**：按路面弯曲需求（前 18 m 路心相对近端的
   横移率）分桶看巡航杆量；反向率同时按「转向活跃秒」归一，避免直道多的局
   分母虚大。
4. **并线过冲的分段原始值**：p90 在 1~3 段时就是最大值，先把每段列出来。

用法::

    python tools/experiments/world_state/metric_audit.py [--sessions ...] [--json out.json]

不给会话时审计 logs 下全部带 `plan` 列的局（按阶段号回落自动分场）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402

LANE_W = 3.5
LEAD_NEAR_M = 30.0        # 「正前方有车」的近距门（秒级反应空间）
LEAD_LANE_M = 0.55 * LANE_W
REV_DEADBAND = 0.05


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _r(x, nd=3):
    return None if x is None or not math.isfinite(x) else round(x, nd)


def _dt(row):
    d = row.get("dt")
    return float(d) if d else 0.05


def _ts(row):
    return row["ts_ns"] / 1e9


def _games(rows):
    """阶段号回落即新的一场；同一会话的多场计数类指标不可混算。"""
    cuts, prev = [], None
    for i, r in enumerate(rows):
        s = r.get("stage_id")
        if prev is not None and s is not None and s < prev:
            cuts.append(i)
        if s is not None:
            prev = s
    out, i0 = [], 0
    for i in cuts + [len(rows)]:
        if i > i0:
            out.append(rows[i0:i])
        i0 = i
    return out


def _curve_demand(row):
    """路面弯曲需求（m/m）：前 15~18 m 路心相对 3~6 m 路心的横移率。

    相机固定在车头，直道的路心随距离不变；弯道越急，远端路心偏得越多。
    只用左右边界都存在的带；带内取 (l+r)/2。"""
    road = (row.get("scene") or {}).get("road") or {}
    near = far = None
    for b in road.get("bands") or ():
        if len(b) < 4 or b[2] is None or b[3] is None:
            continue
        d0 = float(b[0])
        c = 0.5 * (float(b[2]) + float(b[3]))
        if 3.0 <= d0 < 6.0:
            near = c if near is None else 0.5 * (near + c)
        elif 15.0 <= d0 < 18.0:
            far = c if far is None else 0.5 * (far + c)
    if near is None or far is None:
        return None
    return abs(far - near) / 12.0


def _lead_gap(row):
    """正前方同车道最近车的纵向距离（m）；无车或无法判定时 None。

    新局走对象表 `otab`（x_lane/x_m + z_m），旧局退到 `mcars`（x + z 米制）。
    横向门取半道：只算「在这条道上」的车，邻道车不构成必须用速度预测的理由。"""
    ego_m = (row.get("scene") or {}).get("ego_m")
    best = None
    otab = row.get("otab") or {}
    tracks = otab.get("t") or ()
    for t in tracks:
        if len(t) < 5 or t[0] != "car":
            continue
        if len(t) > 6 and not t[6]:     # 只信本拍可见的位置
            continue
        z = t[4]
        if z is None or z <= 0.0 or z > LEAD_NEAR_M:
            continue
        x = t[3]
        if x is None and t[2] is not None:
            x = t[2] * LANE_W
        if x is None or ego_m is None:
            continue
        if abs(x - ego_m) <= LEAD_LANE_M and (best is None or z < best):
            best = z
    if best is not None:
        return best
    if ego_m is None:
        return None
    for m in row.get("mcars") or ():
        x, z = m.get("x"), m.get("z")
        if x is None or z is None or z <= 0.0 or z > LEAD_NEAR_M:
            continue
        if abs(x - ego_m) <= LEAD_LANE_M and (best is None or z < best):
            best = z
    return best


def _is_ns(row):
    return ((row.get("plan") or {}).get("reason") == "plan:no_speed")


def _ctx(row):
    """no_speed 拍按「速度是否参与决策」分级；重叠时取更该用速度的一类。"""
    rt = row.get("route") or {}
    if rt.get("ph") == "merging":
        return "merge"
    if _lead_gap(row) is not None:
        return "lead"
    if (row.get("sel_dbg") or {}).get("req") is not None:
        return "request"
    return "cruise"


def _rev(rows):
    """杆值反号次数（越过死区才算），分母另给活跃秒。

    并线本身的回正会反号一次，和「巡航里左右摇」不是一回事；分开计数，
    否则并线多的局反向率必然更高。"""
    n = m = 0
    last = None
    active_s = active_m_s = 0.0
    for r in rows:
        merging = (r.get("route") or {}).get("ph") == "merging"
        s = r.get("steer_norm") or 0.0
        if abs(s) >= REV_DEADBAND:
            if merging:
                active_m_s += _dt(r)
            else:
                active_s += _dt(r)
        if abs(s) < REV_DEADBAND:
            continue
        sign = 1 if s > 0 else -1
        if last is not None and sign != last:
            if merging:
                m += 1
            else:
                n += 1
        last = sign
    return n, active_s, m, active_m_s


def _overshoots(rows):
    """并线段内过冲（道），逐段列出；口径同 baseline_replay._merges。"""
    segs, cur = [], None
    for i, r in enumerate(rows):
        if (r.get("route") or {}).get("ph") == "merging":
            if cur is None:
                cur = {"i0": i, "i1": i}
            cur["i1"] = i
        elif cur:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    out = []
    for s in segs:
        rs = rows[s["i0"]:s["i1"] + 1]
        why = (rs[0].get("route") or {}).get("why")
        ex = [r.get("executed_lane") for r in rs
              if r.get("executed_lane") is not None]
        ref = next((r["route"].get("ref") for r in rs
                    if (r.get("route") or {}).get("ref") is not None), None)
        if len(ex) < 2 or ref is None or _dt(rs[0]) <= 0:
            out.append({"why": why, "overs": None})
            continue
        d = sum(_dt(r) for r in rs[1:])
        direction = 1.0 if ref > ex[0] else -1.0
        if abs(ref - ex[0]) < 0.05 or d <= 0:
            out.append({"why": why, "overs": None})
            continue
        vals = [(x - ref) * direction for x in ex]
        if not any(v >= -0.02 for v in vals):
            out.append({"why": why, "overs": None})
            continue
        out.append({"why": why, "overs": _r(max(0.0, max(vals))),
                    "dur_s": _r(d, 2),
                    "v_lat": _r(abs(ex[-1] - ex[0]) / d, 3)})
    return out


def audit(rows):
    n = len(rows)
    dur = sum(_dt(r) for r in rows)
    ns = [r for r in rows if _is_ns(r)]
    ns_s = sum(_dt(r) for r in ns)
    ctx = {}
    for r in ns:
        k = _ctx(r)
        ctx[k] = ctx.get(k, 0.0) + _dt(r)

    # 弯曲需求分桶：no_speed 占比 + 巡航杆量
    buckets = {"<0.05": (0.0, 0.05), "0.05-0.10": (0.05, 0.10),
               ">=0.10": (0.10, 1e9)}
    bstat = {k: {"ticks": 0, "ns": 0, "in_band": []} for k in buckets}
    demands, in_band, sat = [], [], 0
    err_ns, err_ok = [], []
    for r in rows:
        c = _curve_demand(r)
        if c is not None:
            demands.append(c)
        reason = str(r.get("reason") or "")
        band = reason.startswith("sel:center") and r.get("maneuver") is False
        s = abs(r.get("steer_norm") or 0.0)
        if band:
            in_band.append(s)
        if s >= 0.9:
            sat += 1
        if c is not None:
            for k, (lo, hi) in buckets.items():
                if lo <= c < hi:
                    b = bstat[k]
                    b["ticks"] += 1
                    b["ns"] += 1 if _is_ns(r) else 0
                    if band:
                        b["in_band"].append(s)
                    break
        rt = r.get("route") or {}
        co = r.get("coarse") or {}
        if rt.get("ph") == "keep" and co.get("ego") is not None \
                and rt.get("ref") is not None:
            e = abs(co["ego"] - rt["ref"])
            (err_ns if _is_ns(r) else err_ok).append(e)

    rev, active_s, rev_m, active_m_s = _rev(rows)
    merge_s = request_s = 0.0
    merge_ns_s = request_ns_s = 0.0
    plan_ok = plan_n = plan_ok_m = plan_n_m = 0
    for r in rows:
        dt = _dt(r)
        merging = (r.get("route") or {}).get("ph") == "merging"
        req = (r.get("sel_dbg") or {}).get("req") is not None
        if merging:
            merge_s += dt
            if _is_ns(r):
                merge_ns_s += dt
        if req:
            request_s += dt
            if _is_ns(r):
                request_ns_s += dt
        if r.get("plan") is not None:
            plan_n += 1
            ok = bool((r["plan"] or {}).get("valid"))
            plan_ok += ok
            if merging:
                plan_n_m += 1
                plan_ok_m += ok
    need_ns_s = ctx.get("merge", 0.0) + ctx.get("lead", 0.0) \
        + ctx.get("request", 0.0)
    return {
        "ticks": n,
        "dur_s": _r(dur, 1),
        "no_speed_s": _r(ns_s, 1),
        "no_speed_frac": _r(ns_s / dur, 3) if dur > 0 else None,
        "need_ns_s": _r(need_ns_s, 1),
        "ns_ctx_s": {k: _r(v, 1) for k, v in sorted(ctx.items())},
        "ns_cruise_frac": _r(ctx.get("cruise", 0.0) / ns_s, 3)
        if ns_s > 0 else None,
        "merge_s": _r(merge_s, 1),
        "merge_ns_frac": _r(merge_ns_s / merge_s, 3) if merge_s > 0 else None,
        "request_ns_frac": _r(request_ns_s / request_s, 3)
        if request_s > 0 else None,
        "plan_valid_rate": _r(plan_ok / plan_n, 3) if plan_n else None,
        "plan_valid_rate_merge": _r(plan_ok_m / plan_n_m, 3)
        if plan_n_m else None,
        "curve_demand_p50": _r(_pct(demands, 50), 3),
        "curve_demand_p90": _r(_pct(demands, 90), 3),
        "curve_buckets": {
            k: {"ticks": v["ticks"], "no_speed": _r(v["ns"] / v["ticks"], 3)
                if v["ticks"] else None,
                "in_band": _r(sum(v["in_band"]) / len(v["in_band"]), 4)
                if v["in_band"] else None}
            for k, v in bstat.items()},
        "in_band": _r(sum(in_band) / len(in_band), 4) if in_band else None,
        "steer_sat_share": _r(sat / n, 3) if n else None,
        "keep_err_ns_p50": _r(_pct(err_ns, 50), 3),
        "keep_err_ns_frac_gt_0_5": _r(sum(1 for e in err_ns if e > 0.5)
                                      / len(err_ns), 3) if err_ns else None,
        "keep_err_ok_p50": _r(_pct(err_ok, 50), 3),
        "keep_err_ok_frac_gt_0_5": _r(sum(1 for e in err_ok if e > 0.5)
                                      / len(err_ok), 3) if err_ok else None,
        "reverse_per_s": _r(rev / dur, 3) if dur > 0 else None,
        "reverse_per_active_s": _r(rev / active_s, 3) if active_s > 0 else None,
        "reverse_merge": rev_m,
        "reverse_merge_per_active_s": _r(rev_m / active_m_s, 3)
        if active_m_s > 0 else None,
        "merges": _overshoots(rows),
    }


def _path(session):
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def _all_sessions():
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    out = []
    for d in sorted(base.iterdir()):
        p = d / "trace" / "speedrush.jsonl"
        if d.is_dir() and p.exists():
            out.append(d.name)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", nargs="*")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    sessions = args.sessions or _all_sessions()
    rep = {}
    hdr = ("session", "dur_s", "ns_s", "need", "crz%", "mrg%", "pvl_m",
           "curve", "in_b", "rev_a", "rev_m", "err_ns", "err_ok")
    print("{:22s} {:>6s} {:>6s} {:>5s} {:>5s} {:>5s} {:>6s} {:>6s} "
          "{:>5s} {:>5s} {:>5s} {:>6s} {:>6s}".format(*hdr))
    for s in sessions:
        p = _path(s)
        if not p.exists():
            continue
        rows = list(iter_rows(p))
        games = [g for g in _games(rows) if any(r.get("plan") for r in g)]
        for i, g in enumerate(games, 1):
            a = audit(g)
            a["merges"] = [m for m in a["merges"]]
            key = s if len(games) == 1 else f"{s}#{i}"
            rep[key] = a
            print("{:22s} {:6.1f} {:6.1f} {:5.1f} {:5.2f} {:5.2f} {:6.3f} "
                  "{:6.3f} {:5.3f} {:5.2f} {:5.2f} {:6.3f} {:6.3f}".format(
                      key, a["dur_s"], a["no_speed_s"],
                      a["need_ns_s"] or 0.0,
                      a["ns_cruise_frac"] or 0.0,
                      a["merge_ns_frac"] or 0.0,
                      a["plan_valid_rate_merge"] or 0.0,
                      a["curve_demand_p50"] or 0.0,
                      a["in_band"] or 0.0,
                      a["reverse_per_active_s"] or 0.0,
                      a["reverse_merge_per_active_s"] or 0.0,
                      a["keep_err_ns_p50"] or 0.0,
                      a["keep_err_ok_p50"] or 0.0))
    if args.json:
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
