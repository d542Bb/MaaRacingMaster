# -*- coding: utf-8 -*-
"""道级收益离线反事实（2026-10-12，只读）：「按道选线」vs 实际直行的历史分差。

问（用户设计裁决 2026-10-11：人的玩法=中场提前变道卡位，决策单位是道不是车）：
如果每拍都把车换到「未来 ~2s 收益最高的道」，比实际走的道多拿多少分？
本脚本在既有 control trace 上离线重放，行为面零改动——纯仪器验证设计假设，
为计划稿 V1 改写（道级收益评分，逐车仪器降级为输入）供数。

模型（粗粒度，上界估计——不含变道动力学 0.73s 与执行滞后）：
- 道格 = 4 道路心的整道栅格（centers ±0.5/±1.5，道，右正，原点=路中心）。
  车/币的账本横向是 ego 系 A1 读数，映射路心系 xr = x + executed_lane
  （executed 是 planner 重锚后的路心系自车位，CODE_WIKI §12 极性契约）。
- 车 t_meet **全量离线重算**（world_model.t_meet_car_s，逐 trace 用账本
  v2 fid_hz 中位；v1 trace 按 (fid,ts) 差分中位估）——账本 t_meet 只给
  过门车落列（band 拒的多数车没有），占用时刻必须全量才有意义。
  rel≤0（pacing/远离）且 cy<v_ego 的车=持续占用整窗。
- 每道收益 = 可收币值 × (1 − 遮挡占比)；道内有到站 <2s 的车即被占用
  （占用至该车到站），到站 <0.8s 的道判"热"（不可收币；若恰是自车所在道
  = 碰撞拍）。币值 = 30×枚数×conf（RULES §4.2 镜像），t_miss>4s 的远币
  打 0.3 折（方向信息仍在，当期吃不到）。
- 自车实际道 = 最近道心；反事实最优道 = argmax 收益。

口径与边界（如实声明）：
- 账本只在 _select 拍落行（冷却/CONSERVE/ABORT 无账），覆盖率逐局报告；
  trace 尾部异常退出丢尾巴（本批 185122_p2 段已知缺 9322 之后）。
- 道格对 ro 系统偏置（挂账 P50 +0.41）敏感——若最优道分布退化成单道
  （每局同一道碾压性获胜），先查道格错位：本探针落币/车路心系分布与
  executed+road_offset 漂移两列诊断，此时「分差」数字不可信。
- 输出走 stdout（重定向即落盘），不写文件。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_lane_counterfactual.py \
        "%APPDATA%\\MaaRacingMaster\\data\\speedrush\\control_traces\\trace_*.jsonl"
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

LANES = (-1.5, -0.5, 0.5, 1.5)   # 4 道路心的整道栅格（道，右正，原点=路中心）
LANE_HALF = 0.75                 # 道归属半宽（道）——车/币入道的横向判据
ROAD_HALF_MAX = 2.2              # 出界丢弃门（道）：4.4 道宽半宽+容差——远场
                                 # A1 膨胀读数（|x|>半宽=几何胡话）不入道格
HORIZON_S = 2.0                  # 收益前瞻（s）
CRASH_T = 0.8                    # 到站短于此 = 道热（不可收币；自车道=碰撞拍）
COIN_VALUE = 30.0                # RULES §4.2 镜像（探针自持，不进决策链）


def _nearest_lane(xr: float) -> float | None:
    if abs(xr) > ROAD_HALF_MAX:
        return None                  # 出界=膨胀读数，不入道格（不归最近道）
    best = min(LANES, key=lambda L: abs(xr - L))
    return best if abs(xr - best) <= LANE_HALF else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--horizon", type=float, default=HORIZON_S)
    ap.add_argument("--crash-t", type=float, default=CRASH_T)
    args = ap.parse_args()

    files: list[str] = []
    for pat in args.traces:
        files.extend(sorted(glob.glob(pat)) if any(c in pat for c in "*?[")
                     else [pat])
    if not files:
        print("无输入 trace")
        return

    tot = collections.Counter()
    all_deltas: list[float] = []
    print("== 道级收益反事实（粗模型上界：不含变道动力学/执行滞后）==")
    sys.path.insert(0, REPO)
    from maaracing_master.plugins.speedrush.world_model import (
        load_calib, t_meet_car_s)
    cal = load_calib()
    for path in files:
        run = os.path.basename(path)
        n_tick = cov = agree = hot_actual = hot_avoidable = 0
        deltas = []
        wall_delta = 0.0
        prev_ts = None
        worst = (0.0, None)          # (delta, fid) 单拍最大差
        lane_hist: collections.Counter = collections.Counter()
        best_hist: collections.Counter = collections.Counter()
        # 道格错位诊断面
        coin_x = []                  # 币路心系 x 采样
        car_x = []
        lane_coin = collections.Counter()    # 道 → 币值累计
        lane_block = collections.Counter()   # 道 → 占用拍数
        # fid 时基：**聚合比率**（总帧数/总时长）——拍间 inst 双峰（33/50/66ms
        # 节律）会把中位/EMA 钉低（实测 33 vs 聚合 38），与生产估计器同病同修
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        f0 = rows[0]["fid"]
        t0 = rows[0].get("ts_ns", 0) / 1e9
        fhz = ((rows[-1]["fid"] - f0) / ((rows[-1].get("ts_ns", 0) / 1e9) - t0)
               if len(rows) > 1 and rows[-1]["fid"] > f0 else 30.0)
        for d in rows:
            n_tick += 1
            led = d.get("ledger")
            ex = d.get("executed_lane")
            if not led or ex is None:
                continue
            coins = led.get("coins") or []
            cars = led.get("cars") or []
            cov += 1
            ts = d.get("ts_ns", 0) / 1e9
            dt = ts - prev_ts if prev_ts is not None else 0.0
            prev_ts = ts
            # 路心系重映射（车用成员位优先；币用组心）；t_meet 全量离线重算
            # （存续组 x_center=nan、确认账本脏值一律 nonfinite 过滤）
            car_lanes = []          # (lane, t_meet)
            for r in cars:
                x = r.get("x_memb")
                if x is None:
                    x = r["x"]
                if x != x or r["rel"] != r["rel"]:
                    continue        # nan（存续/病态行）不入账
                xr = x + ex
                car_x.append(xr)
                if r["cy"] >= cal.v_ego:
                    continue        # 已过自车行：不再占道
                tm = t_meet_car_s(r["rel"], r["cy"], cal, fhz) \
                    if r["rel"] > 0 else None
                L = _nearest_lane(xr)
                if L is not None:
                    car_lanes.append((L, tm))
            coin_lanes = []         # (lane, value)
            for r in coins:
                tm = r.get("t_miss")
                w = 1.0 if (tm is not None and tm <= 2 * args.horizon) else 0.3
                xr = r["x"] + ex
                if xr != xr:
                    continue        # 存续组 x_center=nan：不入账
                coin_x.append(xr)
                L = _nearest_lane(xr)
                if L is not None:
                    coin_lanes.append((L, COIN_VALUE * r["n"] * r["conf"] * w))
                # 逐道收益（tm=None 且在场 = pacing/远离车：整窗占用）
                benefit = {}
                hot = set()
                block = {}
                for L in LANES:
                    tms = [t for l, t in car_lanes if l == L]
                    block[L] = max((t for t in tms
                                    if t is not None and 0.0 <= t <= args.horizon),
                                   default=None)
                    if any(t is None for t in tms) or \
                            any(t < args.crash_t for t in tms
                                if t is not None):
                        hot.add(L)
                    if any(t is None for t in tms):
                        block[L] = args.horizon
                for L in LANES:
                    cv = sum(v for l, v in coin_lanes if l == L)
                    if L in hot:
                        benefit[L] = 0.0
                    elif block[L] is not None:
                        benefit[L] = cv * (1.0 - block[L] / args.horizon)
                    else:
                        benefit[L] = cv
                actual = _nearest_lane(ex)
                if actual is None:
                    continue            # 自车骑线/出格：本拍不判
                for L in LANES:
                    lane_coin[L] += sum(v for l, v in coin_lanes if l == L)
                    if L in hot or block[L] is not None:
                        lane_block[L] += 1
                best = max(LANES, key=lambda L: benefit[L])
                delta = benefit[best] - benefit[actual]
                deltas.append(delta)
                wall_delta += delta * dt
                lane_hist[actual] += 1
                best_hist[best] += 1
                if best == actual:
                    agree += 1
                if actual in hot:
                    hot_actual += 1
                    if best != actual:
                        hot_avoidable += 1
                if delta > worst[0]:
                    worst = (delta, d["fid"])
        ds = sorted(deltas)
        n = len(ds)
        if n == 0:
            print(f"{run}: 无覆盖拍")
            continue
        coin_x.sort()
        cx = coin_x
        cn = len(cx)
        ro_vals = sorted(d["executed_lane"] - d["road_offset"]
                         for d in rows
                         if d.get("executed_lane") is not None
                         and d.get("road_offset") is not None)
        ro_n = len(ro_vals)
        print(f"\n{run}: 拍 {n_tick}，账本覆盖 {cov}（{cov/n_tick*100:.0f}%）"
              f" fid_hz={fhz:.1f}")
        print(f"  [道格诊断] 币路心系 x P10/50/90 = "
              f"{cx[cn//10]:+.1f}/{cx[cn//2]:+.1f}/{cx[9*cn//10]:+.1f}；"
              f"车路心系 x P10/50/90 = "
              f"{car_x[len(car_x)//10]:+.1f}/{car_x[len(car_x)//2]:+.1f}/"
              f"{car_x[9*len(car_x)//10]:+.1f}"
              + (f"；executed−road_offset P50={ro_vals[ro_n//2]:+.2f}"
                 f"（重锚漂移）" if ro_n else ""))
        print(f"  [逐道] 币值/拍 " +
              " ".join(f"L{L:+.1f}:{lane_coin[L]/cov:.0f}" for L in LANES) +
              "；占用拍 " +
              " ".join(f"L{L:+.1f}:{lane_block[L]}" for L in LANES))
        print(f"  最优道=实际道 {agree}/{n}（{agree/n*100:.0f}%）；"
              f"单拍收益差 P50/P75/P95 = {ds[n//2]:.0f}/{ds[3*n//4]:.0f}/{ds[int(n*0.95)]:.0f} 分")
        print(f"  道热（自车道 0.8s 内有车到站）{hot_actual} 拍，"
              f"其中最优道不同（可避开）{hot_avoidable} 拍")
        print(f"  全程收益差积分（×dt）≈ {wall_delta:.0f} 分/局；"
              f"单拍最大差 {worst[0]:.0f} 分 @fid{worst[1]}")
        print(f"  实际道分布 {dict(sorted(lane_hist.items()))}")
        print(f"  最优道分布 {dict(sorted(best_hist.items()))}")
        tot["cov"] += cov
        tot["agree"] += agree
        tot["hot"] += hot_actual
        tot["hot_avoid"] += hot_avoidable
        tot["wall"] += wall_delta
        all_deltas.extend(deltas)
    if tot["cov"]:
        all_d = sorted(all_deltas)
        n = len(all_d)
        print(f"\n== 汇总（{len(files)} trace）==")
        print(f"覆盖拍 {tot['cov']}；最优道=实际道 {tot['agree']/tot['cov']*100:.0f}%；"
              f"单拍收益差 P50/P75/P95 = {all_d[n//2]:.0f}/{all_d[3*n//4]:.0f}/{all_d[int(n*0.95)]:.0f} 分")
        print(f"道热拍 {tot['hot']}（可避开 {tot['hot_avoid']}）；"
              f"收益差积分合计 ≈ {tot['wall']:.0f} 分")


if __name__ == "__main__":
    main()
