# -*- coding: utf-8 -*-
"""机会层只读复算：中心层的时间里，多少是「没机会」，多少是「被门槛挡掉」。

**只读**：只读 trace 与 policy/calibration JSON，不写生产数据。
**复算口径**：用 trace 每拍记录的选择层输入（`sel_dbg` 的 ego/c0/左右缘/
rw/car/coin，加 `route` 的承诺态与 `coarse.lane`）重跑纯函数 `select()`，
先与生产记录对账（层号/原因/请求），对账通过后再跑两份反事实：

- 反事实 A（free）：不承诺、不喂承诺道——机会层回到「跨线直追」的旧口径。
- 反事实 B（gate）：不承诺、但喂承诺道——越线目标只发请求的口径。

用途是分账，不是判错：`free` 层为 1/3 而生产为 4，只说明「与路线门/确认
门槛有关」，不说明保留机会能得分。得分/安全不在本探针范围。

用法：
    .venv/Scripts/python.exe tools/experiments/opportunity_layer/probe_opportunity.py
    ... --root DIR --csv out.csv --selftest
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

# 选择层是纯函数（无状态、无副作用）；复算必须与生产同一份规则，否则对账
# 无法区分「输入不全」和「规则抄错」。例外口径同 frame_delta/replay_route.py。
from maaracing_master.plugins.speedrush.selection import (  # noqa: E402
    Obj, RoadBand, RoadFrame, SelectionParams, select)

POLICY = ROOT / "maaracing_master/plugins/speedrush/resources/policy/decision.json"
CALIB = ROOT / "maaracing_master/plugins/speedrush/resources/calibration/gate0.json"


def load_params() -> tuple[SelectionParams, float, dict]:
    d = json.loads(POLICY.read_text(encoding="utf-8"))
    g = json.loads(CALIB.read_text(encoding="utf-8"))
    s, pl = d["selection"], d["planner"]
    p = SelectionParams(
        lane_w_m=g["lane_w_m"],
        horizon_s=s["horizon_s"], max_waypoints=s["max_waypoints"],
        graze_gap_lane=s["graze_gap_lane"],
        center_band_lane=s["center_band_lane"],
        opportunity_tol_lane=s["opportunity_tol_lane"],
        coin_pick_tol_lane=s["coin_pick_tol_lane"],
        coin_hold_s=s["coin_hold_s"],
        k_lat_lane_s=pl["k_lat_lane_s"], tau_heading_s=pl["tau_heading_s"],
        heading_max_rad=pl["heading_max_rad"],
        reaction_s=s["reaction_s"], margin_s=s["margin_s"],
        aligned_lane=s["aligned_lane"],
        reward_aligned_lane=s["reward_aligned_lane"],
        wall_margin_lane=s["wall_margin_lane"],
        center_lookahead_m=s["center_lookahead_m"],
        switch_dwell_s=s["switch_dwell_s"],
        reward_tol_lane=s["reward_tol_lane"],
        car_half_lane=s["car_half_lane"],
        ego_half_lane=s["ego_half_lane"],
        lane_clearance_lane=s["lane_clearance_lane"])
    return p, g["lane_w_m"], d


def _objs(rows, kind: str, lw: float) -> list[Obj]:
    return [Obj(kind, r[0] * lw, r[1]) for r in rows or ()]


def _road(dbg: dict, lw: float) -> RoadFrame:
    left = None if dbg.get("left") is None else dbg["left"] * lw
    right = None if dbg.get("right") is None else dbg["right"] * lw
    bands: tuple[RoadBand, ...] = ()
    if dbg.get("c0") is not None:
        # 单带只为让 center_at 返回当拍中心；L1~L3 不用带表（只用左右缘）。
        c = dbg["c0"] * lw
        bands = (RoadBand(-200.0, 200.0, c - 0.5, c + 0.5),)
    wide = None if left is None or right is None else right - left
    return RoadFrame(bands=bands, width_m=wide, left_m=left, right_m=right,
                     ok=True)


def _inputs(row: dict, lw: float, coarse_control: bool,
            route_control: bool) -> dict | None:
    dbg = row.get("sel_dbg")
    if not dbg:
        return None
    route = row.get("route") or {}
    coarse = row.get("coarse") or {}
    scene = row.get("scene") or {}
    committed = route.get("ph") == "merging"
    # 生产 `_lane_for_route`：路线闸开时选择器读的永远是承诺参考道（与相位
    # 无关），闸关才退回粗层自选道；无证据/陈旧/未初始化 → None。
    if not coarse_control or not coarse or coarse.get("why") == "no_road" \
            or scene.get("stale"):
        coarse_lane = None
    elif route_control:
        coarse_lane = (None if route.get("vd") == "needs_init"
                       or route.get("ref") is None else route.get("ref"))
    else:
        coarse_lane = coarse.get("lane")
    c0 = dbg.get("c0")
    commit_x = None
    if not committed and route.get("ref") is not None and c0 is not None:
        commit_x = route["ref"] * lw + c0 * lw
    return {
        "ego": dbg["ego"] * lw,
        "rw": _objs(dbg.get("rw"), "reward", lw),
        "car": _objs(dbg.get("car"), "car", lw),
        "coin": _objs(dbg.get("coin"), "coin", lw),
        "road": _road(dbg, lw),
        "coarse": coarse_lane,
        "committed": committed,
        "commit_x": commit_x,
        "route": route,
        "req": dbg.get("req"),
        "layer": row.get("sel_layer"),
        "why": row.get("sel_why"),
        "target": dbg.get("target"),
    }


def _run(p: SelectionParams, x: dict, *, committed: bool,
         commit_x: float | None):
    return select(x["road"], x["rw"], x["car"], x["coin"], x["ego"], p,
                  coarse_lane=x["coarse"], committed=committed,
                  commit_x_m=commit_x)


def _visible(objs, ego: float, p: SelectionParams, tol_lane: float):
    """短视界内、横向在 tol 道以内的可见机会（不判可达）。"""
    out = []
    for o in objs:
        if o.t is None or not (0.0 < o.t <= p.horizon_s):
            continue
        d = abs(o.x - ego) / p.lane_w_m
        if d <= tol_lane:
            out.append((o, d))
    return out


class Acc:
    def __init__(self) -> None:
        self.ticks = 0
        self.t = 0.0
        self.layer_t = [0.0] * 5
        self.layer_n = [0] * 5
        self.committed_t = 0.0
        self.l4_commit_t = 0.0
        self.l4_commit_opp_t = 0.0
        self.l4_free_t = 0.0
        self.l4_want_t = 0.0
        self.l4_want_req_t = 0.0
        self.l4_want_noreq_t = 0.0
        self.l4_pass_t = 0.0
        self.l4_none_t = 0.0
        self.near_coin_t = 0.0
        self.near_rw_t = 0.0
        self.none_empty_t = 0.0
        self.none_srcgap_t = 0.0
        self.gap_noflow_t = 0.0
        self.gap_conf_t = 0.0
        self.gap_nogroup_t = 0.0
        self.gap_bonus_t = 0.0
        self.gap_misc_t = 0.0
        self.none_window_t = 0.0
        self.none_far_t = 0.0
        self.none_unreach_t = 0.0
        self.hold_t = 0.0
        self.req_t = 0.0
        self.ep_n = 0
        self.ep_merge = 0
        self.ep_fail_why: Counter = Counter()
        self.merge_dur: list[float] = []
        self.fail_dur: list[float] = []
        self.why_t: Counter = Counter()
        self.free_why_t: Counter = Counter()
        self.noreq_lane_t: Counter = Counter()
        self.val_ok = 0
        self.val_hold = 0
        self.val_bad = 0
        self.val_req_bad = 0
        self.bad_examples: list[dict] = []


def _bump(acc: Acc, dt: float, row: dict, x: dict, free,
          conf_floor: float) -> None:
    p = row["_p"]
    acc.ticks += 1
    acc.t += dt
    layer = x["layer"] if x["layer"] in (1, 2, 3, 4) else 0
    acc.layer_t[layer] += dt
    acc.layer_n[layer] += 1
    if x["why"] == "coin_hold":
        acc.hold_t += dt
    acc.why_t[x["why"]] += dt
    if x["committed"]:
        acc.committed_t += dt
        if layer == 4:
            acc.l4_commit_t += dt
            see = (_visible(x["rw"], x["ego"], p, p.reward_tol_lane)
                   or _visible(x["coin"], x["ego"], p, p.coin_pick_tol_lane))
            if see:
                acc.l4_commit_opp_t += dt
        return
    if layer != 4:
        return
    acc.l4_free_t += dt
    if free.layer in (1, 3):
        acc.l4_want_t += dt
        acc.free_why_t[free.why] += dt
        if x["req"] is not None:
            acc.l4_want_req_t += dt
        else:
            acc.l4_want_noreq_t += dt
            cx = x["commit_x"]
            k = (None if cx is None
                 else round((free.target_x_m - cx) / p.lane_w_m))
            acc.noreq_lane_t[k] += dt
    elif free.layer == 2:
        acc.l4_pass_t += dt
    else:
        acc.l4_none_t += dt
        win_c = [o for o in x["coin"]
                 if o.t is not None and 0.0 < o.t <= p.horizon_s]
        win_r = [o for o in x["rw"]
                 if o.t is not None and 0.0 < o.t <= p.horizon_s]
        dc = [abs(o.x - x["ego"]) / p.lane_w_m for o in win_c]
        dr = [abs(o.x - x["ego"]) / p.lane_w_m for o in win_r]
        near_c = [d for d in dc
                  if p.coin_pick_tol_lane < d <= p.coin_pick_tol_lane + 1.0]
        near_r = [d for d in dr
                  if p.reward_tol_lane < d <= p.reward_tol_lane + 1.0]
        if (any(d <= p.coin_pick_tol_lane for d in dc)
                or any(d <= p.reward_tol_lane for d in dr)):
            acc.none_unreach_t += dt       # 横向够得着：被挡/来不及
        elif near_c or near_r:
            acc.near_coin_t += dt if near_c else 0.0
            acc.near_rw_t += dt if near_r else 0.0
        elif win_c or win_r:
            acc.none_far_t += dt           # 在视界内但横向太远
        elif x["coin"] or x["rw"]:
            acc.none_window_t += dt        # 有目标但没有到站时间/在视界外
        elif (row.get("raw_coins") or 0) > 0 or (row.get("raw_bonuses") or 0) > 0:
            acc.none_srcgap_t += dt        # 画面看见，没进选择层源
            cg = row.get("cgroups") or []
            raw_cars = row.get("raw_cars") or 0
            if (row.get("raw_bonuses") or 0) > 0 and not x["rw"]:
                acc.gap_bonus_t += dt
            elif not cg:
                acc.gap_nogroup_t += dt
            elif raw_cars == 0:
                # 无旁车 → 无地面流场 → 金币 t_miss 算不出来，整组被丢
                acc.gap_noflow_t += dt
            elif all((g.get("cm") or 0.0) < conf_floor for g in cg):
                acc.gap_conf_t += dt
            else:
                acc.gap_misc_t += dt
        else:
            acc.none_empty_t += dt         # 空街


def _close_ep(acc: Acc, ep: dict) -> None:
    acc.ep_n += 1
    if ep["merged"]:
        acc.ep_merge += 1
        acc.merge_dur.append(ep["dur"])
        return
    acc.fail_dur.append(ep["dur"])
    whys = ep["whys"]
    if whys:
        acc.ep_fail_why[whys.most_common(1)[0][0]] += 1
    else:
        acc.ep_fail_why["<无拒绝码>"] += 1


def _tick(acc: Acc, row: dict, p: SelectionParams, lw: float,
          coarse_control: bool, route_control: bool, conf_floor: float,
          gst: dict, csv_rows: list | None) -> None:
    x = _inputs(row, lw, coarse_control, route_control)
    if x is None:
        return
    dt = float(row.get("dt") or 0.0)
    if dt <= 0.0:
        return
    act = _run(p, x, committed=x["committed"], commit_x=x["commit_x"])
    free = _run(p, x, committed=False, commit_x=None)
    if x["why"] == "coin_hold":
        acc.val_hold += 1
    elif act.layer == x["layer"] and act.why == x["why"]:
        acc.val_ok += 1
    else:
        acc.val_bad += 1
        if len(acc.bad_examples) < 8:
            acc.bad_examples.append({
                "fid": row.get("fid"), "logged": [x["layer"], x["why"]],
                "replay": [act.layer, act.why], "ego": x["ego"],
                "left": x["road"].left_m, "right": x["road"].right_m})
    if (act.lane_request or None) != (x["req"] or None):
        acc.val_req_bad += 1
    row["_p"] = p
    _bump(acc, dt, row, x, free, conf_floor)
    # 请求按连续段计：本拍路线读的是**上一拍**选择器发出的请求。
    route = x["route"] or {}
    vd, why = route.get("vd"), route.get("why") or "none"
    ep = gst["ep"]
    if gst["prev"] is not None:
        acc.req_t += dt
        if ep is None:
            ep = {"dur": 0.0, "merged": False, "whys": Counter(),
                  "fid": row.get("fid")}
            gst["ep"] = ep
        ep["dur"] += dt
        if vd == "merge_start" and why == "opportunity":
            ep["merged"] = True
        elif not ep["merged"] and why not in ("merging", "no_request"):
            ep["whys"][why] += dt
    elif ep is not None:
        _close_ep(acc, ep)
        gst["ep"] = None
    gst["prev"] = x["req"]
    if csv_rows is not None:
        csv_rows.append([
            row.get("token"), row.get("stage_id"), row.get("fid"),
            round(dt, 4), x["layer"], x["why"], x["req"],
            int(x["committed"]), route.get("ph"), route.get("why"),
            free.layer, free.why, free.lane_request,
            round(x["ego"] / lw, 3)])


def _pct(a: float, b: float) -> str:
    return "—" if b <= 0 else f"{100.0 * a / b:4.1f}%"


def _median(xs: list[float]) -> float:
    ys = sorted(xs)
    n = len(ys)
    return ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])


def print_report(groups: dict, total: Acc) -> None:
    print("== 复算对账（层号/原因）==")
    n = total.val_ok + total.val_hold + total.val_bad
    print(f"  有效拍 {n}；一致 {total.val_ok}（{_pct(total.val_ok, n)}）；"
          f"金币保持 {total.val_hold}（{_pct(total.val_hold, n)}）；"
          f"不一致 {total.val_bad}（{_pct(total.val_bad, n)}）")
    print(f"  请求列不一致 {total.val_req_bad} 拍"
          f"（{_pct(total.val_req_bad, n)}）")
    for e in total.bad_examples:
        print(f"    例 fid={e['fid']} 生产={e['logged']} 复算={e['replay']}"
              f" ego={e['ego']:.2f}m")

    print("\n== 层时间占比（全部有效拍）==")
    for k in (1, 2, 3, 4):
        name = {1: "L1 奖励车", 2: "L2 擦边/让位", 3: "L3 金币",
                4: "L4 回中/承诺"}[k]
        print(f"  {name:<12} {total.layer_t[k]:8.1f}s  "
              f"{_pct(total.layer_t[k], total.t)}  "
              f"({total.layer_n[k]} 拍)")
    print(f"  其中金币保持 coin_hold {total.hold_t:6.1f}s "
          f"（{_pct(total.hold_t, total.t)}）")

    print("\n== 中心层（L4）时间的去向 ==")
    base = total.layer_t[4]
    print(f"  L4 合计              {base:8.1f}s")
    print(f"  ├ 承诺期（合同压制） {total.l4_commit_t:8.1f}s  "
          f"{_pct(total.l4_commit_t, base)}")
    print(f"  │  其中可见机会     {total.l4_commit_opp_t:8.1f}s  "
          f"{_pct(total.l4_commit_opp_t, base)}")
    print(f"  └ 非承诺            {total.l4_free_t:8.1f}s  "
          f"{_pct(total.l4_free_t, base)}")
    print(f"      ├ 可起意(机会)  {total.l4_want_t:8.1f}s  "
          f"{_pct(total.l4_want_t, base)}")
    print(f"      │   ├ 有请求    {total.l4_want_req_t:8.1f}s  "
          f"{_pct(total.l4_want_req_t, base)}")
    print(f"      │   └ 无请求    {total.l4_want_noreq_t:8.1f}s  "
          f"{_pct(total.l4_want_noreq_t, base)}")
    if total.noreq_lane_t:
        ks = "、".join(f"k={k}:{t:.1f}s" for k, t in
                       total.noreq_lane_t.most_common())
        print(f"      │     无请求的道差：{ks}")
    print(f"      ├ 仅安全擦边    {total.l4_pass_t:8.1f}s  "
          f"{_pct(total.l4_pass_t, base)}")
    print(f"      └ 无机会          {total.l4_none_t:8.1f}s  "
          f"{_pct(total.l4_none_t, base)}")
    print(f"          ├ 空街          {total.none_empty_t:8.1f}s  "
          f"{_pct(total.none_empty_t, base)}")
    print(f"          ├ 看见未进源    {total.none_srcgap_t:8.1f}s  "
          f"{_pct(total.none_srcgap_t, base)}")
    print(f"          │   ├ 无旁车(无流场) {total.gap_noflow_t:7.1f}s / "
          f"无分组 {total.gap_nogroup_t:.1f}s / 低置信 "
          f"{total.gap_conf_t:.1f}s / 奖励车 {total.gap_bonus_t:.1f}s / "
          f"其他 {total.gap_misc_t:.1f}s")
    print(f"          ├ 无到站/视界外 {total.none_window_t:8.1f}s  "
          f"{_pct(total.none_window_t, base)}")
    near_t = total.near_coin_t + total.near_rw_t
    print(f"          ├ 横向太远      {total.none_far_t + near_t:8.1f}s  "
          f"{_pct(total.none_far_t + near_t, base)}"
          f"（一道内 {near_t:.1f}s / 更远 {total.none_far_t:.1f}s）")
    print(f"          └ 容差内没选上  {total.none_unreach_t:8.1f}s  "
          f"{_pct(total.none_unreach_t, base)}")

    print("\n== 机会请求（连续段 = 一次机会换道意图） ==")
    fail = total.ep_n - total.ep_merge
    print(f"  请求在场 {total.req_t:6.1f}s；连续段 {total.ep_n} 个："
          f"起并 {total.ep_merge}，未起并 {fail}")
    if total.merge_dur:
        print(f"  起并段时长 中位 {_median(total.merge_dur):.2f}s"
              f"（{min(total.merge_dur):.2f}~{max(total.merge_dur):.2f}）")
    if total.fail_dur:
        print(f"  未起并段时长 中位 {_median(total.fail_dur):.2f}s"
              f"（{min(total.fail_dur):.2f}~{max(total.fail_dur):.2f}）")
    if total.ep_fail_why:
        print("  未起并的主因（按段数）：")
        for why, n in total.ep_fail_why.most_common():
            print(f"    {why:<22} {n:4d} 段")

    print("\n== 每局 ==")
    print("  token/stage          tick    时间    L1     L2     L3     L4"
          "   L4承诺  可起意  无机会")
    for key, a in groups.items():
        tok, st = key
        print(f"  {tok}/{st:<3} {a.ticks:7d} {a.t:7.1f}"
              f" {_pct(a.layer_t[1], a.t):>6} {_pct(a.layer_t[2], a.t):>6}"
              f" {_pct(a.layer_t[3], a.t):>6} {_pct(a.layer_t[4], a.t):>6}"
              f" {_pct(a.l4_commit_t, a.layer_t[4]):>7}"
              f" {_pct(a.l4_want_t, a.layer_t[4]):>7}"
              f" {_pct(a.l4_none_t, a.layer_t[4]):>7}")


def selftest() -> None:
    p, lw, _ = load_params()
    base = {"ego": 0.0, "c0": 0.0, "left": -2.0, "right": 2.0, "req": None,
            "rw": [], "car": [], "coin": [], "band": 0.3, "look": 0.0}
    # 1) 相邻道奖励车：无门直追 L1；有承诺道则只发请求且生产仍回中
    d = dict(base, rw=[[1.0, 1.0]])
    x = _inputs({"sel_dbg": d, "route": {"ph": "keep", "ref": 0.0},
                 "coarse": {"lane": 0.0}}, lw, True, True)
    assert x["committed"] is False
    free = _run(p, x, committed=False, commit_x=None)
    gated = _run(p, x, committed=False, commit_x=x["commit_x"])
    assert free.layer == 1, free
    assert gated.layer == 4 and gated.lane_request == 1.0, gated
    # 2) 承诺期：同一辆车在合同里不改变层
    x2 = _inputs({"sel_dbg": d, "route": {"ph": "merging", "ref": 0.0},
                  "coarse": {"lane": 2.0}}, lw, True, True)
    assert x2["committed"] is True
    assert _run(p, x2, committed=True, commit_x=x2["commit_x"]).layer == 4
    # 3) 金币在容差外：不进入机会层
    d3 = dict(base, coin=[[1.6, 1.0]])
    x3 = _inputs({"sel_dbg": d3, "route": {"ph": "keep", "ref": 0.0},
                  "coarse": {"lane": 0.0}}, lw, True, True)
    assert _run(p, x3, committed=False, commit_x=None).layer == 4
    # 4) 两道路径不存在的邻道请求：k=±2 不发请求
    d4 = dict(base, rw=[[2.0, 1.0]])
    x4 = _inputs({"sel_dbg": d4, "route": {"ph": "keep", "ref": 0.0},
                  "coarse": {"lane": 0.0}}, lw, True, True)
    g4 = _run(p, x4, committed=False, commit_x=x4["commit_x"])
    assert g4.layer == 4 and g4.lane_request is None, g4
    print("selftest ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="trace 根（默认 %%APPDATA%%/MaaRacingMaster/logs）")
    ap.add_argument("--run", default=None, help="只分析某个 token")
    ap.add_argument("--csv", default=None, help="逐拍导出路径")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    root = Path(args.root) if args.root else (
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs")
    if not root.is_dir():
        raise SystemExit(f"logs 根目录不存在：{root}")
    p, lw, cfg = load_params()
    conf_floor = float(cfg["scoring"]["conf_floor"])
    coarse_control = bool(cfg["coarse"]["control"])
    route_control = bool(cfg["route"]["control"])
    files = sorted(root.glob("*/trace/speedrush.jsonl"))
    if args.run:
        files = [f for f in files if f.parent.parent.name == args.run]
    if not files:
        raise SystemExit("没有匹配的 trace")
    total = Acc()
    gst: dict[tuple[str, int], dict] = {}
    total_st = {"prev": None, "ep": None}
    csv_rows: list | None = [] if args.csv else None
    for f in files:
        token = f.parent.parent.name
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") is not None or not row.get("sel_dbg"):
                    continue
                row["token"] = token
                key = (token, int(row.get("stage_id") or 0))
                st = gst.setdefault(key, {"acc": Acc(), "prev": None,
                                          "ep": None})
                _tick(st["acc"], row, p, lw, coarse_control, route_control,
                      conf_floor, st, csv_rows)
                _tick(total, row, p, lw, coarse_control, route_control,
                      conf_floor, total_st, None)
        for st in gst.values():
            if st["ep"] is not None:
                _close_ep(st["acc"], st["ep"])
                st["ep"] = None
            st["prev"] = None
    if total_st["ep"] is not None:
        _close_ep(total, total_st["ep"])
        total_st["ep"] = None
    groups = {k: v["acc"] for k, v in gst.items()}
    print_report(groups, total)
    if args.csv and csv_rows:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["token", "stage", "fid", "dt", "layer", "why",
                        "req", "committed", "route_ph", "route_why",
                        "free_layer", "free_why", "free_req", "ego_lane"])
            w.writerows(csv_rows)
        print(f"\n逐拍导出：{args.csv}（{len(csv_rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
