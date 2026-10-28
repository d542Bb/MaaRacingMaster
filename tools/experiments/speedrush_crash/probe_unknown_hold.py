# -*- coding: utf-8 -*-
"""离线影子：目标距离变 UNKNOWN 后，粗层占用保持的收益与代价
（撞车复盘实验 2 的验收版）。

只读控制 trace，不 import 生产代码、不发控制指令。输入每拍 ``coarse.cand``
（逐车道 [lane, free_m, width_margin, blocker, unknown_car]），对「上一拍仍被
占、这一拍突然变空」的车道做有限期保持，再比较保持前后的净空与最优车道。

保持规则（影子候选，非生产实现）：
- 触发：某车道本拍 blocker 非空或 unknown_car=true，记下净空、该车道最近的
  米制车（id/x/z/v）与时刻。
- 应用：该车道净空取 min(本拍净空, 最后已知净空)。
- 释放顺序：该车出现 pass 事件 → 纵深年龄超过生产锚龄上限 600ms →
  有界外推（最后已知纵深 − 相对速度×龄 ≤ 车头前 3m）→ 超时（TTL）。
  外推是确定性信号，优先于兜底超时；锚龄硬顶保证保持不会沿用比生产更老的纵深。

**距离新鲜度与目标存在分开**（GPT 审阅意见）：
- 保持沿用的一定是旧值/外推值，本探针逐拍记录它的年龄（距最后一次纵深更新）；
- 目标是否仍在，用最后已知纵深/横向位/ pass 事件判断，不靠 2D 命中刷新距离。

验收指标：
- 保护拍：保持把选择移出“仍在前方、仍在该道”的车所占车道；
- 误避让拍：保持改变了选择，但该车已通过/已并排/已离开该道；
- 过期占用拍：保持仍在生效、但该车按最后状态已不该再占道的拍；
- 释放滞后：实际释放时刻 − 按最后 z/v 预测的“不再占道”时刻；
- 纵深年龄：保持拍上旧值的年龄分位。

最优车道是简化复选（有效宽余量车道里净空最大、平手取离参照最近），不是生产
choose_lane 的逐位复刻——本探针只回答「占用状态与代价量级」。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

LANE_W_M = 3.38          # calibration/gate0.json
ENVELOPE_M = (0.27 + 0.27) * LANE_W_M + 0.5   # coarse.collision_margin_m
PASS_Z_M = 3.0           # 车头前 3m 起不再算前方障碍（coarse: zf <= -2 忽略）
HOLD_Z_M = 29.0          # 5m 相机基准 + 24m 窗
MAX_HOLD_AGE_MS = 600.0  # 与生产 anchor_max_age_ms 同值：保持不得沿用更老的纵深


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def _best_lane(cand, ref):
    """简化复选：有效宽余量车道里净空最大；平手取离参照最近。"""
    valid = [(lane, free) for lane, free, margin, _b, _u in cand
             if margin is not None and free > 0]
    if not valid:
        return None
    top = max(f for _l, f in valid)
    ties = [lane for lane, f in valid if abs(f - top) < 1e-6]
    return min(ties, key=lambda lane: abs(lane - ref))


def _lane_center(lane: float, ego: float) -> float:
    return (lane - ego) * LANE_W_M


def _car_for_free(r, free: float):
    """按净空反查占用来源：粗层净空 ≈ 纵深 − 相机基准，取 z 最接近的车。

    这比按车道包络归属更稳：中心线在目标纵深处的偏移不在 trace 里，
    按 ego 帧近似会把车归错道（事故车在 ego 帧 x≈0.15，却同时截断两条道）。
    """
    target = free + 5.0
    best = None
    for m in r.get("mcars") or []:
        z = m.get("z")
        if z is None or z < 0:
            continue
        d = abs(z - target)
        if best is None or d < best[0]:
            best = (d, m)
    # 差距过大说明该道不是这辆车截断的（space / 未知车）
    return None if best is None or best[0] > 6.0 else best[1]


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def replay(rows, ttl_s: float, window, preempt_pass: bool,
           pred_pass: bool, v_cap: float):
    held: dict[float, dict] = {}
    passed: dict[int, float] = {}
    stats = {
        "ticks": 0, "hold_ticks": 0, "lane_changed": 0,
        "rel_pass": 0, "rel_age": 0, "rel_pred": 0, "rel_ttl": 0,
        "lanes_touched": 0,
        "protect": 0, "unnecessary": 0, "neutral": 0,
        "prod_in_occupied": 0, "hold_diverts_prod": 0,
        "stale_ticks": 0,
        "age_ms": [], "over_hold": [], "under_hold": [],
        "episodes": 0, "ep_protect": 0, "ep_unnecessary": 0,
    }
    table = []
    details = []
    for r in rows:
        cand = (r.get("coarse") or {}).get("cand") or []
        if not cand:
            continue
        now = r.get("_t")
        fid = r.get("fid")
        ego = (r.get("coarse") or {}).get("ego") or 0.0
        prod = (r.get("coarse") or {}).get("lane")
        stats["ticks"] += 1
        for p in r.get("passes") or []:
            if p.get("id") is not None:
                passed.setdefault(p["id"], now)
        # 本拍更新占用源
        for lane, free, _m, blocker, unknown in cand:
            if free is None or not (blocker is not None or unknown):
                continue
            if blocker == "space":
                # 墙/路缘占用与车无关：保留净空，但不做车 attribution
                h = held.get(lane)
                if h is not None:
                    h["free"] = min(h["free"], free)
                    h["t"] = now
                    h["tid"] = None
                    h["z"] = None
                    h["v"] = None
                    h["t_z"] = None
                continue
            car = _car_for_free(r, free)
            h = held.get(lane)
            if h is None:
                h = {"free": free, "t": now, "tid": None, "z": None,
                     "v": None, "x": None, "t_z": None,
                     "t0": now, "protect": 0, "unnecessary": 0}
                stats["episodes"] += 1
            h["free"] = min(h.get("free", free), free)
            h["t"] = now
            if car is not None:
                cid = car.get("id")
                if cid != h.get("tid"):
                    h["z"] = None
                    h["t_z"] = None
                    h["v"] = None
                h["tid"] = cid
                h["x"] = car.get("x")
                if car.get("z") is not None:
                    h["z"] = car.get("z")
                    h["v"] = car.get("v")
                    h["t_z"] = now
            held[lane] = h
        # 逐车道释放与应用
        out = []
        active = {}
        touched = 0
        for lane, free, margin, blocker, unknown in cand:
            f = free
            h = held.get(lane)
            if h is not None:
                age = now - h["t"]
                reason = None
                pred_t = None
                if (h.get("z") is not None and h.get("v") is not None
                        and h["v"] > 0):
                    v = min(h["v"], v_cap)
                    pred_t = h["t_z"] + max(0.0, h["z"] - PASS_Z_M) / v
                if preempt_pass and h.get("tid") is not None \
                        and h["tid"] in passed:
                    reason = "rel_pass"
                elif (h.get("t_z") is not None
                      and (now - h["t_z"]) * 1000.0 > MAX_HOLD_AGE_MS):
                    reason = "rel_age"
                elif pred_pass and pred_t is not None and now >= pred_t:
                    reason = "rel_pred"
                elif age > ttl_s:
                    reason = "rel_ttl"
                if reason:
                    del held[lane]
                    stats[reason] += 1
                    if pred_t is not None:
                        d = now - pred_t
                        (stats["over_hold"] if d >= 0
                         else stats["under_hold"]).append(d)
                    if h.get("protect"):
                        stats["ep_protect"] += 1
                    if h.get("unnecessary"):
                        stats["ep_unnecessary"] += 1
                else:
                    if f is None or h["free"] < f:
                        f = h["free"]
                        touched += 1
                    active[lane] = h
            out.append([lane, f, margin, blocker, unknown])
        if touched:
            stats["hold_ticks"] += 1
            stats["lanes_touched"] += touched
        base = _best_lane(cand, prod if prod is not None else 0.0)
        hold = _best_lane(out, base if base is not None else 0.0)
        if base != hold:
            stats["lane_changed"] += 1
        # 逐车道判收益/代价
        for lane, h in active.items():
            if base != lane:
                continue          # 无保持时本来就不会选这条道，保持没有改变选择
            if hold == lane:
                continue          # 保持没有把选择移开
            cid = h.get("tid")
            if cid is None:
                stats["neutral"] += 1
                continue
            seen = passed.get(cid)
            last_z = h.get("z")
            # 横向归属不可靠（中心线在目标纵深处的偏移不在 trace 里），
            # “已并排”只认 pass 事件与纵深进入车头前 3m。
            beside = (seen is not None
                      or (last_z is not None and last_z <= PASS_Z_M))
            ahead = (not beside and last_z is not None
                     and last_z >= PASS_Z_M and last_z <= HOLD_Z_M)
            if ahead:
                stats["protect"] += 1
                h["protect"] = h.get("protect", 0) + 1
            elif beside:
                stats["unnecessary"] += 1
                h["unnecessary"] = h.get("unnecessary", 0) + 1
                stats["stale_ticks"] += 1
            else:
                stats["neutral"] += 1
            if h.get("t_z") is not None:
                stats["age_ms"].append((now - h["t_z"]) * 1000.0)
            x = h.get("x")
            if prod is not None and x is not None \
                    and abs(x - _lane_center(prod, ego)) < ENVELOPE_M:
                stats["prod_in_occupied"] += 1
                if hold != prod:
                    stats["hold_diverts_prod"] += 1
            if window and window[0] <= fid <= window[1]:
                details.append((fid, cid, lane, h.get("z"), h.get("v"),
                                round((now - h["t_z"]) * 1000.0, 1)
                                if h.get("t_z") is not None else None,
                                "protect" if ahead else
                                "unnecessary" if beside else "neutral",
                                prod, base, hold))
        if window and window[0] <= fid <= window[1]:
            l0 = next((c for c in cand if abs(c[0]) < 1e-6), None)
            h0 = next((c for c in out if abs(c[0]) < 1e-6), None)
            table.append((fid, l0, h0, prod, base, hold,
                          r.get("executed_lane"), r.get("x_target"),
                          r.get("sel_why")))
    return stats, table, details


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+", help="debug/speedrush 下的局名或 trace 路径")
    ap.add_argument("--ttl-ms", type=float, default=400.0)
    ap.add_argument("--window", default=None, help="打印窗口，如 1552-1570（按 fid）")
    ap.add_argument("--no-pass-release", action="store_true",
                    help="不使用「确认掠过」释放")
    ap.add_argument("--no-pred-pass", action="store_true",
                    help="不使用「有界外推」释放")
    ap.add_argument("--v-cap", type=float, default=60.0,
                    help="外推相对速度上限（m/s）")
    args = ap.parse_args()
    window = None
    if args.window:
        a, b = args.window.split("-")
        window = (int(a), int(b))
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        rows = [json.loads(line) for line in open(path, encoding="utf-8")]
        t = 0.0
        for r in rows:
            r["_t"] = t
            t += r.get("dt") or 0.05
        stats, table, details = replay(
            rows, args.ttl_ms / 1000.0, window,
            preempt_pass=not args.no_pass_release,
            pred_pass=not args.no_pred_pass, v_cap=args.v_cap)
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        print(f"   拍 {stats['ticks']}  TTL {args.ttl_ms:.0f}ms  "
              f"保持生效拍 {stats['hold_ticks']}  改变最优道 {stats['lane_changed']}  "
              f"受影响车道拍次 {stats['lanes_touched']}")
        print(f"   释放: 掠过 {stats['rel_pass']} / 超龄 {stats['rel_age']} / "
              f"外推 {stats['rel_pred']} / 超时 {stats['rel_ttl']}")
        print(f"   改变选择的拍里：保护 {stats['protect']}  "
              f"误避让 {stats['unnecessary']}  证据不足 {stats['neutral']}  "
              f"（过期占用拍 {stats['stale_ticks']}）")
        print(f"   生产道仍被该车占用、保持会移开：{stats['prod_in_occupied']} 拍"
              f"（其中保持确实移开 {stats['hold_diverts_prod']}）")
        ages = stats["age_ms"]
        if ages:
            print(f"   保持时旧纵深年龄：P50 {_pct(ages,.5):.0f}ms "
                  f"P90 {_pct(ages,.9):.0f}ms 最大 {max(ages):.0f}ms "
                  f"（>600ms {sum(1 for a in ages if a > 600)} 拍）")
        if stats["over_hold"]:
            print(f"   释放滞后（晚于预测不再占道）：P50 "
                  f"{_pct(stats['over_hold'],.5)*1000:.0f}ms "
                  f"P90 {_pct(stats['over_hold'],.9)*1000:.0f}ms "
                  f"最大 {max(stats['over_hold'])*1000:.0f}ms")
        if stats["under_hold"]:
            print(f"   提前释放（早于预测）：P50 "
                  f"{_pct(stats['under_hold'],.5)*1000:.0f}ms "
                  f"P90 {_pct(stats['under_hold'],.9)*1000:.0f}ms")
        print(f"   保持段 {stats['episodes']}：含保护拍 "
              f"{stats['ep_protect']}，含误避让拍 {stats['ep_unnecessary']}")
        for fid, l0, h0, prod, base, hold, exe, xt, why in table:
            print(f"   fid {fid}  0道净空 {None if l0 is None else l0[1]}"
                  f"→{None if h0 is None else h0[1]}"
                  f"  生产选道 {prod} 无保持 {base} 有保持 {hold}"
                  f"  exe {exe}  xt {xt}  sel {why}")
        for fid, cid, lane, z, v, age, cat, prod, base, hold in details:
            print(f"     car{cid} fid{fid} 道{lane} z={z} v={v} 龄={age}ms "
                  f"{cat}  生产{prod} 无保持{base} 有保持{hold}")


if __name__ == "__main__":
    main()
