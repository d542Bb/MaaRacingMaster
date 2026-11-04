# -*- coding: utf-8 -*-
"""固定门 vs 时间锥：旧局上的请求资格反事实（第 3 期 3e 的离线部分）。

只回答一件事：**旧 1.2/1.5 道固定横距门挡住了多少「按时间锥其实来得及、
且是相邻 ±1 道」的机会**。输入是旧局 `sel_dbg`（机会横位/到站/自车/承诺道），
规则按当前选择层复刻（时间锥 + 相邻请求），不 import 业务代码。

不覆盖：长币线远端时间与命中拍请求贯通需要完整的组账/命中上下文，旧局
无法复现；这两项以新局为准。输出里的“潜在并线”只是同 trace 里 1s 内出现
merge_start 的上界，不是因果转化率。
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

REPO = HERE.parents[2]
POLICY = REPO / "maaracing_master" / "plugins" / "speedrush" / \
    "resources" / "policy" / "decision.json"
OLD_COIN_TOL = 1.2       # 2026-11-01 删除前的固定门（历史口径，只用于反事实）
OLD_REWARD_TOL = 1.5
LANE_W = 3.5
GAP_S = 0.5


def _policy():
    d = json.loads(POLICY.read_text(encoding="utf-8"))
    s, p = d["selection"], d["planner"]
    return {"horizon": s["horizon_s"], "reaction": s["reaction_s"],
            "margin": s["margin_s"], "aligned": s["aligned_lane"],
            "ego_half": s["ego_half_lane"],
            "car_half": s["car_half_lane"],
            "k_lat": p["k_lat_lane_s"], "tau": p["tau_heading_s"],
            "head_max": p["heading_max_rad"]}


def _move_time(dx_lane, c):
    e = abs(dx_lane)
    if e <= c["aligned"]:
        return 0.0
    a = 1.0 - math.exp(-0.02 / c["tau"])
    psi, t = 0.0, 0.0
    for _ in range(500):
        des = math.atan(c["k_lat"] * e * LANE_W / 45.0)
        des = max(-c["head_max"], min(c["head_max"], des))
        psi += a * (des - psi)
        e -= (45.0 / LANE_W) * math.tan(psi) * 0.02
        t += 0.02
        if e <= c["aligned"]:
            return t
    return math.inf


def _slack(dx_lane, t, c):
    if t is None or not math.isfinite(t) or t <= 0:
        return None
    need = c["reaction"] if abs(dx_lane) <= c["aligned"] else \
        _move_time(dx_lane, c) + c["reaction"] + c["margin"]
    return t - need


def _segments(points, gap=GAP_S):
    out, cur, prev = [], [], None
    for fid, t in points:
        if cur and prev is not None and t - prev > gap:
            out.append(cur)
            cur = []
        cur.append((fid, t))
        prev = t
    if cur:
        out.append(cur)
    return out


def _blocked(cars, target, t, c):
    half = (c["car_half"] + c["ego_half"]) * 1.0
    for e in cars or []:
        if not e or len(e) < 2 or e[1] is None:
            continue
        if e[1] > t + c["margin"] or e[1] < -0.2:
            continue
        if abs(e[0] - target) < half:
            return True
    return False


def _entry(kind, x, t, ego, commit, c, cars):
    """→ (旧门会丢?, 新规则会发请求?)。x/ego/commit 均为道。"""
    if t is None or not math.isfinite(t) or not (0.0 < t <= c["horizon"]):
        return False, False
    dx = x - ego
    old_blocked = abs(dx) > (OLD_COIN_TOL if kind == "coin" else OLD_REWARD_TOL)
    aim = x
    if kind == "coin" and abs(dx) > c["ego_half"]:
        aim = x - math.copysign(c["ego_half"], dx)
    if _blocked(cars, aim, t, c):
        return old_blocked, False
    if commit is None:
        # 无承诺道：本道/相邻一道直接拽，不产生请求
        return old_blocked, False
    if abs(aim - commit) <= 0.5:
        return old_blocked, False
    k = round(aim - commit)
    req = k if abs(k) == 1 else None
    if req is None:
        return old_blocked, False
    return old_blocked, (_slack(aim - ego, t, c) or -1) >= 0


def analyze(rows):
    c = _policy()
    old_ticks, new_ticks, gate_ticks = [], [], []
    old_segs, new_segs, gate_segs = [], [], []
    merge_ts = [r["ts_ns"] / 1e9 for r in rows
                if (r.get("route") or {}).get("vd") == "merge_start"]
    cur_old = cur_new = cur_gate = None
    for row in rows:
        dbg = row.get("sel_dbg") or {}
        if not dbg or dbg.get("ego") is None:
            cur_old = cur_new = cur_gate = None
            continue
        ego = dbg["ego"]
        c0 = dbg.get("c0") or 0.0
        route = row.get("route") or {}
        if route.get("ph") == "merging":
            cur_old = cur_new = cur_gate = None
            continue
        ref = route.get("ref")
        commit = None if ref is None else ref + c0
        old = dbg.get("req") is not None
        new = False
        blocked = False
        for kind, key in (("reward", "rw"), ("coin", "coin")):
            for e in dbg.get(key) or []:
                if not e or len(e) < 2:
                    continue
                ob, nr = _entry(kind, e[0], e[1], ego, commit, c,
                                dbg.get("car"))
                new = new or nr
                blocked = blocked or (ob and nr)
        point = (row["fid"], row["ts_ns"] / 1e9)
        if old:
            old_ticks.append(point)
            if cur_old is None:
                cur_old = []
                old_segs.append(cur_old)
            cur_old.append(point)
        else:
            cur_old = None
        if new:
            new_ticks.append(point)
            if cur_new is None:
                cur_new = []
                new_segs.append(cur_new)
            cur_new.append(point)
        else:
            cur_new = None
        if blocked:
            gate_ticks.append(point)
            if cur_gate is None:
                cur_gate = []
                gate_segs.append(cur_gate)
            cur_gate.append(point)
        else:
            cur_gate = None
    potential = 0
    for seg in new_segs:
        end = seg[-1][1]
        if any(0.0 <= t - end <= 1.0 for t in merge_ts):
            potential += 1
    return {
        "rules": {"horizon_s": c["horizon"], "old_coin_tol": OLD_COIN_TOL,
                  "old_reward_tol": OLD_REWARD_TOL},
        "old_request_ticks": len(old_ticks),
        "old_request_segments": len(old_segs),
        "cone_request_ticks": len(new_ticks),
        "cone_request_segments": len(new_segs),
        "gate_blocked_feasible_ticks": len(gate_ticks),
        "gate_blocked_feasible_segments": len(gate_segs),
        "cone_only_ticks": sum(1 for f, t in new_ticks
                               if not any(f2 == f for f2, _ in old_ticks)),
        "potential_merge_within_1s": potential,
        "note": "gate_blocked=旧固定门会丢、新规则按时间锥+相邻请求会发；"
                "cone_only 未计层优先级与 request 确认门，是上界。potential 只是"
                "同 trace 1s 内出现 merge_start 的上界，非因果转化率；远端时间/"
                "命中贯通未计。",
    }


def _path(session):
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261101_113145")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rep = analyze(list(iter_rows(_path(args.session))))
    rep["session"] = args.session
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
