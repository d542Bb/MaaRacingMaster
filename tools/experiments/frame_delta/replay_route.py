# -*- coding: utf-8 -*-
"""2488 类事件的只读路线回放：先复现生产，再在换纪处分叉。

**只读**：不碰硬件/日志写；import 路线纯函数 `tick_route` 仅用于回放。
**固定轨迹**：每拍输入取 trace 里的 coarse 候选表与 ego，不重放车辆动力学；
只能回答「路线会怎么决策」，不能回答得分/安全是否更好。

## 口径

1. 输入：trace 每拍的 `coarse.cand`（lane/free_m/width_margin/blocker/
   unknown_car）与 `coarse.ego` 重建 `CoarseReport`；`dt` 取同拍。
2. 初始状态：merge_start 拍的生产状态（`ref/tgt/phase/epoch`、
   `lane_from`=前一拍 ref）；时钟归零，`v_ref`=该拍 ego。
3. 生产回放：epoch 取 trace 的 `route.ep`，逐拍与生产 `ph/ref/vd` 对齐；
   对不上即报第一处，不静默继续。
4. 反事实回放：同一初始状态、同一输入；epoch 只认 **c0 跳变**
   （|Δc0|>0.5，换纪触发 ①），忽略 executed 单跳（触发 ②）。清空点之后的
   占用撤销/到达/超时/重建规则**全部照旧**，不豁免。
5. 输出：第一次分歧拍、承诺存续时长、反事实结局（finish/timeout/cancel）、
   路线级目标（`ref*lane_w + c0`）与生产目标的差、剩余预算。
   选择器目标/转向指令不在本回放范围（需要选择层输入，trace 没存）。

用法：
    .venv/Scripts/python.exe tools/experiments/frame_delta/replay_route.py \
        --run-id 20261028_213246 --fid 2488 [--root DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.coarse import (  # noqa: E402
    CoarseLane, CoarseReport)
from maaracing_master.plugins.speedrush.route import (  # noqa: E402
    PHASE_MERGING, RouteParams, RouteState, tick_route)

LW = 3.38
PARAMS = RouteParams(
    switch_gain_m=4.0,
    proxy_speed_mps=45.0,
    urgent_confirm_ticks=3,
    opportunity_confirm_ticks=6,
    cancel_unsafe_ticks=5,
    cancel_cooldown_s=2.0,
    finish_tol_lane=0.3,
    finish_v_tol_lane_s=0.4,
    merge_max_s=4.0,
    soft_car=False)


def load_run(root: Path, run_id: str) -> tuple[Path, list[dict]]:
    for p in sorted(root.glob("*/trace/speedrush.jsonl")):
        rows = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                if '"type"' in line:
                    continue
                r = json.loads(line)
                if r.get("run_id") == run_id:
                    rows.append(r)
        if rows:
            return p, rows
    raise SystemExit(f"没找到 run_id={run_id}")


def report_of(r: dict) -> CoarseReport | None:
    co = r.get("coarse") or {}
    lanes = []
    for c in co.get("cand") or []:
        try:
            lanes.append(CoarseLane(
                lane=float(c[0]),
                free_m=float(c[1]),
                width_margin_m=None if c[2] is None else float(c[2]),
                blocker=c[3],
                unknown_car=bool(c[4]) if len(c) > 4 else False))
        except (TypeError, ValueError, IndexError):
            return None
    if not lanes or co.get("ego") is None:
        return None
    return CoarseReport(ego_lane=float(co["ego"]), lane=float(co["ego"]),
                        direction=0, free_m=0.0, reason="", lanes=tuple(lanes))


def rt(r: dict) -> dict:
    return r.get("route") or {}


def c0(r: dict) -> float | None:
    v = (r.get("sel_dbg") or {}).get("c0")
    return None if v is None else float(v)


def find_events(rows: list[dict], fid: int) -> tuple[int, int, int]:
    reb = next(i for i, r in enumerate(rows)
               if r.get("fid") == fid and (r.get("lat_ctrl") or {}).get("rebase"))
    ms = reb
    while ms >= 0 and rt(rows[ms]).get("vd") != "merge_start":
        ms -= 1
    if ms < 0:
        raise SystemExit("重基拍之前没有 merge_start")
    ep = rt(rows[reb]).get("ep")
    fork = next((i for i in range(reb + 1, len(rows))
                 if rt(rows[i]).get("ep") is not None and ep is not None
                 and rt(rows[i]).get("ep") > ep), len(rows))
    return ms, reb, fork


def initial_state(rows: list[dict], ms: int, rep: CoarseReport) -> RouteState:
    src = (rt(rows[ms - 1]).get("ref") if ms > 0 else None)
    return RouteState(
        lane_ref=float(rt(rows[ms])["ref"]),
        lane_from=float(src if src is not None else 0.0),
        lane_target=(None if rt(rows[ms]).get("tgt") is None
                     else float(rt(rows[ms])["tgt"])),
        phase=PHASE_MERGING,
        decided_t=0.0,
        elapsed_s=0.0,
        cooldown_left_s=0.0,
        ban_lane=None,
        unsafe_ticks=0,
        blocked_ticks=0,
        epoch=int(rt(rows[ms]).get("ep") or 0),
        v_ref=rep.ego_lane,
        merge_started_t=0.0,
        v_est_lane_s=0.0)


def replay(rows: list[dict], ms: int, state: RouteState,
           epoch_at) -> list[dict]:
    """从 merge_start 的下一拍开始推进；返回逐拍 (idx, state, verdict)。"""
    out = []
    stage = rows[ms].get("stage_id")
    for q in range(ms + 1, len(rows)):
        if rows[q].get("stage_id") != stage:
            break
        rep = report_of(rows[q])
        if rep is None:
            break
        dt = float(rows[q].get("dt") or 0.05)
        st, vd = tick_route(state, rep, PARAMS, dt_s=dt, epoch=epoch_at(q),
                            request=None, arrival=None,
                            reject_shift_arrival=False)
        out.append({"idx": q, "fid": rows[q].get("fid"), "state": st,
                    "vd": vd, "ego": rep.ego_lane})
        state = st
        if st.elapsed_s > PARAMS.merge_max_s + 5.0:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--fid", type=int, required=True, help="重基拍 fid")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    root = (Path(args.root) if args.root else
            Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "logs")
    path, rows = load_run(root, args.run_id)
    ms, reb, fork = find_events(rows, args.fid)
    rep0 = report_of(rows[ms])
    if rep0 is None:
        raise SystemExit("merge_start 拍没有可用的 coarse 报告")
    state0 = initial_state(rows, ms, rep0)
    print(f"文件 {path}")
    print(f"run {args.run_id}  重基 fid{args.fid}  merge_start fid"
          f"{rows[ms].get('fid')}  分叉 fid{rows[fork].get('fid')}"
          if fork < len(rows) else "（未找到分叉）")
    print(f"源道 {state0.lane_from:g} → 目标 {state0.lane_target}  "
          f"初始 epoch {state0.epoch}")

    # 生产回放：epoch 取 trace
    def ep_trace(q: int):
        return rt(rows[q]).get("ep")

    prod = replay(rows, ms, state0, ep_trace)
    # 验证：从 merge_start 下一拍到分叉前一拍，逐拍与 trace 对齐
    validated = [x for x in prod if x["idx"] < fork]
    mism = []
    for x in validated:
        q = x["idx"]
        act = rt(rows[q])
        want = (act.get("ph"), act.get("ref"), act.get("tgt"), act.get("vd"))
        got = (x["vd"].phase, x["vd"].lane_ref, x["vd"].lane_target,
               x["vd"].verdict)
        if (got[0] != want[0] or got[1] != want[1]
                or got[3] != want[3]):
            mism.append((q, rows[q].get("fid"), want, got))
    print(f"\n生产回放验证（merge_start+1 → 分叉前）：{len(validated)} 拍中 "
          f"首处不一致 {len(mism)} 处")
    if mism:
        for q, fid, want, got in mism[:5]:
            print(f"  fid{fid}: 生产 {want}  回放 {got}")
        print("  → 初始状态或输入重建不可信，反事实分支不采用。")
        return

    # 反事实：epoch 只认 c0 跳变
    cf_ep = [state0.epoch]

    def ep_cf(q: int):
        prev = c0(rows[q - 1]) if q > 0 else None
        cur = c0(rows[q])
        if prev is not None and cur is not None and abs(cur - prev) > 0.5:
            cf_ep[0] += 1
        return cf_ep[0]

    cf = replay(rows, ms, state0, ep_cf)
    # 第一次分歧（路线级目标 ref*lane_w+c0）
    div = None
    for x in cf:
        q = x["idx"]
        if q < fork:
            continue
        prod_t = None if rt(rows[q]).get("ref") is None else (
            float(rt(rows[q])["ref"]) * LW
            + (c0(rows[q]) or 0.0) * LW)
        cf_t = (x["vd"].lane_ref * LW + (c0(rows[q]) or 0.0) * LW
                if x["vd"].lane_ref is not None else None)
        if prod_t is not None and cf_t is not None and abs(prod_t - cf_t) > 0.05:
            div = (q, rows[q].get("fid"), prod_t, cf_t)
            break
    # 反事实结局
    end = next((x for x in cf if x["idx"] >= fork
                and x["vd"].phase != PHASE_MERGING), None)
    # 生产在分叉后第一次把 ref 落到原目标道（可能是 rebuild，不是 finish）
    prod_same = next((x for x in cf if x["idx"] >= fork
                      and rt(rows[x["idx"]]).get("ref") is not None
                      and state0.lane_target is not None
                      and abs(float(rt(rows[x["idx"]])["ref"])
                              - state0.lane_target) < 0.25), None)
    print("\n分叉后逐拍（生产 vs 反事实）：")
    print("| fid | ego | 生产 ph/ref/vd | 生产 x_target | 反事实 ph/ref/vd | "
          "反事实已用 s |")
    print("|---:|---:|---|---:|---|---:|")
    for x in cf:
        q = x["idx"]
        if q < fork:
            continue
        if q > (end["idx"] + 3 if end else fork + 12):
            break
        a = rt(rows[q])
        print(f"| {rows[q].get('fid')} | {x['ego']:+.2f} | "
              f"{a.get('ph')}/{a.get('ref')}/{a.get('vd')} | "
              f"{rows[q].get('x_target')} | "
              f"{x['vd'].phase}/{x['vd'].lane_ref}/{x['vd'].verdict} | "
              f"{x['state'].elapsed_s:.2f} |")
    if div:
        print(f"\n首次路线目标分歧：fid{div[1]} 生产 {div[2]:+.2f} m vs "
              f"反事实 {div[3]:+.2f} m")
    else:
        print("\n分叉后没有路线目标分歧。")
    # 实际消费目标（trace 的 x_target）何时离开原目标道
    fork_el = next(x["state"].elapsed_s for x in cf if x["idx"] >= fork)
    xt_div = next((x for x in cf if x["idx"] >= fork
                   and rows[x["idx"]].get("x_target") is not None
                   and state0.lane_target is not None
                   and abs(float(rows[x["idx"]]["x_target"])
                           - state0.lane_target) > 0.2), None)
    if xt_div is not None:
        print(f"首次实际控制目标（x_target）离开原目标道：fid"
              f"{rows[xt_div['idx']].get('fid')}"
              f"（x_target={rows[xt_div['idx']].get('x_target'):+.2f}），"
              f"距分叉 {xt_div['state'].elapsed_s - fork_el:.2f} s")
    if end is None:
        print("反事实在回放窗口内没有结束（未到 finish/timeout/cancel）。")
    else:
        q = end["idx"]
        dist = (None if state0.lane_target is None else
                abs(state0.lane_target - end["ego"]))
        print(f"反事实结局：fid{rows[q].get('fid')} "
              f"{end['vd'].verdict}（{end['vd'].why}），"
              f"并线已用 {end['state'].elapsed_s:.2f} s，"
              f"距原目标道 {dist if dist is None else round(dist, 2)} 道。")
    if end is not None and prod_same is not None:
        pf = rows[prod_same["idx"]].get("fid")
        ef = rows[end["idx"]].get("fid")
        same = abs(state0.lane_target - end["ego"]) <= 0.35
        print(f"完成对比：生产在 fid{pf} "
              f"（{rt(rows[prod_same['idx']]).get('vd')}）把 ref 落到同一"
              f"目标道；反事实在 fid{ef} finish"
              + ("——反事实不更早，且终点相同。" if same else
                 "——终点不同，需看反事实的落点。"))
    budget = rows[reb].get("intent_deadline_s")
    print(f"分叉时剩余预算：{budget} s；固定轨迹回放，不评估得分/安全。")
    print("限制：选择器目标与转向指令未回放（trace 没有选择层输入）；"
          "反事实只走到路线层。")


if __name__ == "__main__":
    main()
