# -*- coding: utf-8 -*-
"""尺度修正只读影子：选择层在真实米制下会怎么变。

**只读**：只读 trace 与 policy/calibration，不跑游戏、不改生产。

口径：
- 生产输入（trace 的 ``sel_dbg``）都在旧控制道里（``lane_w=3.38``，深度
  米制偏大 ``k``）；真实米制 = 旧道 ×3.38/k，真实道宽 3.5 m。
- 校正影子把每个**位置量**（ego/左右缘/c0/车/币/奖励车）乘
  ``f = 3.38/(k·3.5)`` 后按 3.5 m 送入同一个纯函数 ``select``；车道**序号**
  （route.ref / coarse.lane / 请求道数）不缩放。
- 变体：V0=生产口径（对账用），V1=仅尺度校正（参数不动），V2=尺度校正
  + 横移运动学按真实米制折回（a/vmax ×f，保持物理加减速不变）。
- 计划失效：trace 的 ``plan.fd`` 是旧米制，校正后 ``fd/k``；统计
  ``|fd|>1.0`` 的拍占比变化。
- 擦边几何：0.65 道在旧/新尺度下的真实米数。
- 线间距：旧 ΔL 与锁定线残差乘 f 后的分布（只做预期，不重跑跟踪器）。

用法：``.venv/Scripts/python.exe probe_scale_shadow.py [--root LOGS]``
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.selection import (  # noqa: E402
    Obj, RoadBand, RoadFrame, SelectionParams, select)

POLICY = ROOT / "maaracing_master/plugins/speedrush/resources/policy/decision.json"
CALIB = ROOT / "maaracing_master/plugins/speedrush/resources/calibration/gate0.json"
LW_OLD = 3.38
LW_NEW = 3.5
K_MAIN = 1.12
# a_x 车横位尺已是画线真尺度（录制帧最近画线像素距离与 3.5 m 道吻合），
# 换尺度时不缩放；道路/自车/左右缘来自深度旧网格，才乘 f。
CAR_SCALE = 1.0


def load_params(lw: float, kin_scale: float = 1.0):
    d = json.loads(POLICY.read_text(encoding="utf-8"))
    s, pl = d["selection"], d["planner"]
    return SelectionParams(
        lane_w_m=lw, horizon_s=s["horizon_s"],
        max_waypoints=s["max_waypoints"], graze_gap_lane=s["graze_gap_lane"],
        center_band_lane=s["center_band_lane"],
        opportunity_tol_lane=s["opportunity_tol_lane"],
        coin_pick_tol_lane=s["coin_pick_tol_lane"],
        coin_hold_s=s["coin_hold_s"],
        a_lat_lane_s2=pl["a_lat_gain"] * kin_scale,
        v_lat_max_lane_s=pl["v_lat_max"] * kin_scale,
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


def _objs(rows, kind: str, lw: float, f: float) -> list[Obj]:
    return [Obj(kind, r[0] * f * lw, r[1]) for r in rows or ()]


def _road(dbg: dict, lw: float, f: float) -> RoadFrame:
    left = None if dbg.get("left") is None else dbg["left"] * f * lw
    right = None if dbg.get("right") is None else dbg["right"] * f * lw
    bands: tuple[RoadBand, ...] = ()
    if dbg.get("c0") is not None:
        c = dbg["c0"] * f * lw
        bands = (RoadBand(-200.0, 200.0, c - 0.5, c + 0.5),)
    wide = None if left is None or right is None else right - left
    return RoadFrame(bands=bands, width_m=wide, left_m=left, right_m=right,
                     ok=True)


def _inputs(row: dict, lw: float, f_road: float, f_obj: float,
            coarse_control: bool, route_control: bool) -> dict | None:
    """f_road：道路/自车（旧深度网格 → 真米）；f_obj：车/币/奖励车。

    相机帧核对：``a_x`` 车横位尺已落在画线真尺度（最近画线像素距离
    与 3.5 m 道吻合，旧网格解释系统性偏大），故车/币默认 f_obj=1.0；
    道路中心/自车/左右缘来自深度，f_road=3.38/(k·3.5)。
    """
    dbg = row.get("sel_dbg")
    if not dbg:
        return None
    route = row.get("route") or {}
    coarse = row.get("coarse") or {}
    scene = row.get("scene") or {}
    committed = route.get("ph") == "merging"
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
        # ref 是车道序号（不缩放）；c0 是道路中心位置量（旧网格 → 真米）。
        commit_x = route["ref"] * lw + c0 * f_road * lw
    return {
        "ego": dbg["ego"] * f_road * lw,
        "rw": _objs(dbg.get("rw"), "reward", lw, f_obj),
        "car": _objs(dbg.get("car"), "car", lw, f_obj),
        "coin": _objs(dbg.get("coin"), "coin", lw, f_obj),
        "road": _road(dbg, lw, f_road),
        "coarse": coarse_lane,
        "committed": committed,
        "commit_x": commit_x,
    }


def _run(p: SelectionParams, x: dict):
    return select(x["road"], x["rw"], x["car"], x["coin"], x["ego"], p,
                  coarse_lane=x["coarse"], committed=x["committed"],
                  commit_x_m=x["commit_x"])


def _lane(target_x_m: float, lw: float) -> float:
    return target_x_m / lw


def _q(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    a = int(k)
    b = min(a + 1, len(xs) - 1)
    return xs[a] + (xs[b] - xs[a]) * (k - a)


def _f(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


class S:
    def __init__(self) -> None:
        self.ticks = 0
        self.val_ok = 0
        self.val_why = 0
        self.val_bad = 0
        self.layer_chg = 0
        self.why_chg = 0
        self.req_chg = 0
        self.req_pairs: Counter = Counter()
        self.layer_from_to: Counter = Counter()
        self.why_chg_pairs: Counter = Counter()
        self.fd_n = 0
        self.fd_bad_old = 0
        self.fd_bad_new = 0
        self.fs_n = 0
        self.fs_bad_old = 0
        self.fs_bad_new = 0
        self.v2_layer_chg = 0
        self.v2_tgt_chg = 0
        self.pass_ticks = 0
        self.dl: list[float] = []
        self.resid: list[float] = []
        self.dt = 0.0
        self.t = 0.0


def _tick(s: S, row: dict, p0: SelectionParams, p1: SelectionParams,
          p2: SelectionParams, f: float, coarse_control: bool,
          route_control: bool) -> None:
    x0 = _inputs(row, LW_OLD, 1.0, 1.0, coarse_control, route_control)
    x1 = _inputs(row, LW_NEW, f, CAR_SCALE, coarse_control, route_control)
    if not x0 or not x1:
        return
    r0, r1, r2 = _run(p0, x0), _run(p1, x1), _run(p2, x1)
    s.ticks += 1
    dt = row.get("dt") or 0.0
    s.dt += dt
    s.t += dt
    # 对账：V0 与生产记录
    layer_prod = row.get("sel_layer")
    why_prod = row.get("sel_why")
    if r0.layer == layer_prod:
        s.val_ok += 1
        if r0.why == why_prod:
            s.val_why += 1
    else:
        s.val_bad += 1
    tgt1 = _lane(r1.target_x_m, LW_NEW)
    if r1.layer != r0.layer:
        s.layer_chg += 1
        s.layer_from_to[(r0.layer, r1.layer)] += 1
    if r1.why != r0.why:
        s.why_chg += 1
        s.why_chg_pairs[(r0.why, r1.why)] += 1
    req0 = None if r0.lane_request is None else round(r0.lane_request, 3)
    req1 = None if r1.lane_request is None else round(r1.lane_request, 3)
    if req0 != req1:
        s.req_chg += 1
        s.req_pairs[(req0, req1)] += 1
    if r2.layer != r1.layer:
        s.v2_layer_chg += 1
    if abs(_lane(r2.target_x_m, LW_NEW) - tgt1) > 0.05:
        s.v2_tgt_chg += 1
    if r1.layer == 2:
        s.pass_ticks += 1
    # 计划失效：旧米制 vs 校正米制
    pl = row.get("plan") or {}
    fd = pl.get("fd")
    if isinstance(fd, (int, float)):
        s.fd_n += 1
        bad_old = abs(fd) > 1.0
        bad_new = abs(fd) / K_MAIN > 1.0
        s.fd_bad_old += bad_old
        s.fd_bad_new += bad_new
        if pl.get("reason") == "plan:frame_shift":
            s.fs_n += 1
            s.fs_bad_old += bad_old
            s.fs_bad_new += bad_new
    # 线间距/残差（只做预期）
    lo = row.get("lane_obs") or {}
    near = [q for q in (lo.get("lines") or [])
            if q.get("y0", 0) >= 550 and q.get("L") is not None]
    near.sort(key=lambda q: q["L"])
    for a, b in zip(near, near[1:]):
        dl = b["L"] - a["L"]
        if 0.7 < dl < 1.6:
            s.dl.append(dl)
    for q in (lo.get("lines") or []):
        if q.get("accepted") and q.get("resid") is not None:
            s.resid.append(abs(q["resid"]))


def _report(name: str, s: S) -> None:
    n = max(1, s.ticks)
    print(f"\n== {name} ==  拍 {s.ticks}  用时 {s.t:.1f}s")
    print(f"  对账：层同 {100 * s.val_ok / n:.1f}%（其中 why 同 "
          f"{100 * s.val_why / n:.1f}%）")
    print(f"  尺度校正 V1：层变 {s.layer_chg}（{100 * s.layer_chg / n:.1f}%）"
          f" why 变 {s.why_chg}（{100 * s.why_chg / n:.1f}%）"
          f" 请求变 {s.req_chg}")
    if s.req_pairs:
        print(f"    请求变化分布 {s.req_pairs.most_common(8)}")
    print(f"  V2（运动学折回）：层变 {s.v2_layer_chg} 目标变 {s.v2_tgt_chg}")
    if s.fd_n:
        print(f"  计划失效（近似 fd/k）|fd|>1.0m：旧 "
              f"{100 * s.fd_bad_old / s.fd_n:.2f}% → 校正 "
              f"{100 * s.fd_bad_new / s.fd_n:.2f}%（n={s.fd_n}）")
    if s.fs_n:
        print(f"    frame_shift 拍：旧 {100 * s.fs_bad_old / s.fs_n:.2f}%"
              f" → 校正 {100 * s.fs_bad_new / s.fs_n:.2f}%（n={s.fs_n}）")
    print(f"  擦边拍 {s.pass_ticks}；0.65 道真实米数 旧 "
          f"{_f(0.65 * LW_OLD / K_MAIN, 3)} → 新 {_f(0.65 * LW_NEW, 3)}；"
          f" 车边余量 旧 {_f((0.65 - 0.27) * LW_OLD / K_MAIN, 3)} → 新 "
          f"{_f((0.65 - 0.27) * LW_NEW, 3)}")
    if s.dl:
        print(f"  相邻线间距：旧 中位 {_f(st.median(s.dl), 3)} → 校正后 "
              f"{_f(st.median(s.dl) * LW_OLD / (K_MAIN * LW_NEW), 3)}")
    if s.resid:
        print(f"  锁定线 |resid|：旧 中位 {_f(st.median(s.resid), 3)} / "
              f"p90 {_f(_q(s.resid, .9), 3)} → 校正后 "
              f"{_f(st.median(s.resid) * LW_OLD / (K_MAIN * LW_NEW), 3)} / "
              f"{_f(_q(s.resid, .9) * LW_OLD / (K_MAIN * LW_NEW), 3)}")
    if s.layer_from_to:
        top = s.layer_from_to.most_common(8)
        print(f"    层变分布 {top}")


def main() -> int:
    global K_MAIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--run", default=None)
    ap.add_argument("--k", type=float, default=K_MAIN,
                    help="深度尺度因子（默认 1.12；工坊上界约 1.16）")
    args = ap.parse_args()
    K_MAIN = float(args.k)
    root = Path(args.root) if args.root else (
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs")
    d = json.loads(POLICY.read_text(encoding="utf-8"))
    coarse_control = bool(d["coarse"]["control"])
    route_control = bool(d["route"]["control"])
    p0 = load_params(LW_OLD)
    p1 = load_params(LW_NEW)
    p2 = load_params(LW_NEW, kin_scale=LW_OLD / (K_MAIN * LW_NEW))
    f = LW_OLD / (K_MAIN * LW_NEW)
    files = sorted(root.glob("*/trace/speedrush.jsonl"))
    if args.run:
        files = [q for q in files if q.parent.parent.name == args.run]
    if not files:
        raise SystemExit("没有匹配的 trace")
    total = S()
    print(f"logs 根：{root}")
    print(f"k={K_MAIN}  f={f:.4f}  V2 运动学 ×{f:.4f}")
    for q in files:
        tok = q.parent.parent.name
        s = S()
        with q.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") is not None or not row.get("sel_dbg"):
                    continue
                _tick(s, row, p0, p1, p2, f, coarse_control, route_control)
                _tick(total, row, p0, p1, p2, f, coarse_control, route_control)
        if s.ticks:
            _report(tok, s)
    _report("汇总", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
