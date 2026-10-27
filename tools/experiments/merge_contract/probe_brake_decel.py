# -*- coding: utf-8 -*-
"""刹停预算减速 A/B 回放（工作台，不入库）。

**背景**：打杆反向的 50~67% 落在刹停预算触发的那一拍；预算按
``brake_decel_lane_s2=0.8``（人工实测 p50 1.3，取保守低端）预测停止点，
越过目标带远侧缘即满杆反打。0.8 意味着"提前很多就反打"。

**本探针**：同一份 trace 只改这一个参数（0.8/1.1/1.3/1.6），逐拍重放
LateralPlanner，回答：

  1. 触发率与杆值反向次数是否随减速上调而下降；
  2. 变道末端的**过冲**是否变大（越过目标后的最大超出）；
  3. **到点率**是否下降（提前反打导致的欠冲/停不到位）；
  4. 回放贴合实录的程度（发散大时只看相对）、深度残差、瞬移。

稳态误差 = 目标在过去 0.5s 内变化 ≤0.08 道时 |exec−target|（停不到/过冲都会
变大）；跑宽 = |exec|>1.4 道（贴边逃生的门槛）的拍数与最大 |exec|；瞬移与
深度残差照旧。事件式过冲/到点在目标 slew 下样本太少，不再采用。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_brake_decel.py \
        --session 20261027_110753_1
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from probe_anchor_replay import (  # noqa: E402
    DEFAULT_LOGS, HOLD_SRC, TICK_HZ, _decision, _plan, load_games)
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.planner import LateralPlanner  # noqa: E402

HZ = TICK_HZ
OVER_WIN = int(0.6 * HZ)       # 过冲观察窗
COMMIT_WIN = int(1.5 * HZ)     # 到点观察窗


def replay(g, base, decel) -> dict:
    p = dataclasses.replace(base.planner, brake_decel_lane_s2=decel)
    pl = LateralPlanner(p)
    lane_w = 3.38
    out = dict(ticks=0, fidelity=[], residual=[], trig=0, runs=[], rev=0,
               tv=0.0, tele=0, max_dt=0.0, blind=0,
               steady=[], wide=0, absmax=0.0, tgt=[])
    prev_exec = None
    prev_u = None
    tgt_hist = []
    for i, row in enumerate(g):
        sc = row.get("scene") or {}
        lane_w = float(sc.get("lane_w") or lane_w)
        dt = float(row.get("dt") or (1.0 / HZ))
        fid = int(row.get("fid") or (i + 1))
        ro = row.get("road_offset")
        src = row.get("ro_source") or "none"
        hold = src in HOLD_SRC
        new = bool(row.get("dgeo_new")) and not hold and ro is not None
        ln = row.get("lane_obs") or {}
        lane_x = ln.get("x") if isinstance(ln, dict) else None
        lane_ok = (isinstance(ln, dict) and ln.get("state") == "TRACK"
                   and int(ln.get("n_locked") or 0) >= 1
                   and lane_x is not None)
        pl.update(_decision(row, fid), dt, fid, road_offset=ro,
                  road_offset_new=new, road_offset_hold=hold,
                  lane_x=lane_x, lane_ok=lane_ok,
                  lane_relock=(ln.get("state") == "RELOCK"
                               if isinstance(ln, dict) else False),
                  lane_theta=ln.get("theta") if isinstance(ln, dict) else None,
                  lane_theta_ok=lane_ok,
                  lane_fid=ln.get("det_fid") if isinstance(ln, dict) else None,
                  lane_age_ms=(ln.get("det_age_ms")
                               if isinstance(ln, dict) else None),
                  plan=_plan(row, lane_w))
        ex = pl.state.executed_lane
        u = pl.last_steer_raw
        xt = row.get("x_target")
        out["ticks"] += 1
        if prev_exec is not None:
            d = abs(ex - prev_exec)
            out["max_dt"] = max(out["max_dt"], d)
            if d > 0.6:
                out["tele"] += 1
        prev_exec = ex
        rec = row.get("executed_lane")
        if isinstance(rec, (int, float)):
            out["fidelity"].append(abs(ex - rec))
        if ro is not None and not hold:
            out["residual"].append(abs(ro - (pl._road_anchor or 0.0) - ex))
        if isinstance(xt, (int, float)):
            if abs(xt - ex) > 1.0 and abs(u) < 0.2:
                out["blind"] += 1
            if pl._brake_overshoot(xt):
                out["trig"] += 1
            out["absmax"] = max(out["absmax"], abs(ex))
            if abs(ex) > 1.4:
                out["wide"] += 1
            # 近年 0.5s 内目标是否稳定（slew 结束后才计稳态误差）
            tgt_hist.append((i, xt))
            while tgt_hist and i - tgt_hist[0][0] > int(0.5 * HZ):
                tgt_hist.pop(0)
            if len(tgt_hist) >= int(0.5 * HZ) - 1:
                vals = [v for _, v in tgt_hist]
                if max(vals) - min(vals) <= 0.08:
                    out["steady"].append(abs(ex - xt))
        if prev_u is not None:
            out["tv"] += abs(u - prev_u)
            if u * prev_u < 0 and abs(u - prev_u) > 0.15:
                out["rev"] += 1
        prev_u = u
    return out


def stats(xs):
    xs = sorted(xs)
    if not xs:
        return (0.0, 0.0, 0)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    return (q(0.5), q(0.9), len(xs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--decels", default="0.8,1.1,1.3,1.6")
    args = ap.parse_args()

    base = load_decision()
    games = load_games(args.logs, args.session)
    decels = [float(x) for x in args.decels.split(",")]
    print("session %s games=%d  decels=%s" % (
        args.session, len(games), decels))

    agg = {d: dict(fid=[], res=[], trig=0, rev=0, tv=0.0, tele=0, blind=0,
                   steady=[], wide=0, absmax=0.0, ticks=0) for d in decels}
    for gi, g in enumerate(games):
        print("\n-- game%d (%d 拍) --" % (gi + 1, len(g)))
        for d in decels:
            r = replay(g, base, d)
            a = agg[d]
            a["fid"].extend(r["fidelity"])
            a["res"].extend(r["residual"])
            a["trig"] += r["trig"]
            a["rev"] += r["rev"]
            a["tv"] += r["tv"]
            a["tele"] += r["tele"]
            a["blind"] += r["blind"]
            a["steady"].extend(r["steady"])
            a["wide"] += r["wide"]
            a["absmax"] = max(a["absmax"], r["absmax"])
            a["ticks"] += r["ticks"]
            f = stats(r["fidelity"])
            q = stats(r["residual"])
            st = stats(r["steady"])
            print("  decel %.1f 触发 %3d(%.0f%%) 反 %3d | 贴合 %.2f/%.2f "
                  "残差 %.2f/%.2f | 稳态错 %.2f/%.2f | 跑宽 %d max|exec| %.2f" % (
                      d, r["trig"], 100.0 * r["trig"] / max(r["ticks"], 1),
                      r["rev"], f[0], f[1], q[0], q[1], st[0], st[1],
                      r["wide"], r["absmax"]))
    print("\n== 汇总（%d 局 %d 拍）==" % (len(games), agg[decels[0]]["ticks"]))
    print("  decel  触发率  反向  Σ|Δu|  瞬移 僵直 | 贴合 p50/p90  残差 p50/p90 "
          "| 稳态错 p50/p90 | 跑宽拍 max|exec|")
    for d in decels:
        a = agg[d]
        f = stats(a["fid"])
        q = stats(a["res"])
        st = stats(a["steady"])
        print("  %.1f   %5.1f%% %4d %6.1f %4d %4d | %.2f/%.2f  %.2f/%.2f | "
              "%.2f/%.2f | %4d %.2f" % (
                  d, 100.0 * a["trig"] / max(a["ticks"], 1), a["rev"], a["tv"],
                  a["tele"], a["blind"], f[0], f[1], q[0], q[1],
                  st[0], st[1], a["wide"], a["absmax"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
