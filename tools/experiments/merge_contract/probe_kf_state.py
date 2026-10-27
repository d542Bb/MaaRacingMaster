# -*- coding: utf-8 -*-
"""横向状态估计器 A/B：现行 α-β vs 不确定模型加速度的 1-D 卡尔曼（工作台）。

背景：横向车模增益可能随前向速度变化，而前向速度不可知（53~77% 拍是
默认 45 m/s）。不追求辨识绝对速度，转而把"模型加速度"当**不确定输入**：
卡尔曼过程噪声随 |杆值| 增大，感知修正按不确定度加权——模型负责预测、
感知负责相对修正，两边按把握大小融合。

两臂输入完全相同（同一录像的杆值流 + 同一份新鲜路心观测）：
  A 现行：α=0.3 位置 / β=0.5 速度（时间基、死区、同号门、变化率上限），
    大新息 3 次同向确认后一步重基，陈旧 1s 重基（planner ⑥b 语义）。
  B 卡尔曼：状态 [x, v]，预测 = 双积分（a=g·steer, τ=12），过程噪声
    Q_v=(σ_g·steer·dt)²（增益不确定）+ Q_d（未建模扰动）；观测 = 新鲜路心
    （skip 参考系跳变拍），R=σ_z²；权由 P 决定。
指标：位置残差（对干净观测）、横速真值误差（相邻非跳变新鲜读数的差，
仅在 |Δ|>0.15 的激励段）、贴边僵直拍、预算触发时刻；另给撞栏段轨迹。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_kf_state.py \
        --session 20261027_110753_1 --dump 25.2:27.4
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.module import classify_ro_hop  # noqa: E402

DEFAULT_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.environ.get(
        "SPEEDRUSH_DEBUG",
        r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/speedrush"))),
    "logs")
HOLD_SRC = {"pair_slot", "wall_occl", "memory"}
TICK_HZ = 18.0


def load_games(logs_dir: str, token: str):
    path = os.path.join(logs_dir, token, "trace", "speedrush.jsonl")
    games, cur = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            kind = d.get("type")
            if kind == "stage_begin":
                if cur:
                    games.append(cur)
                cur = []
                continue
            if kind in ("stage_end", "session_begin"):
                continue
            cur.append(d)
    if cur:
        games.append(cur)
    return games


def tick_inputs(row, i):
    dt = float(row.get("dt") or (1.0 / TICK_HZ))
    ts = (row.get("ts_mono_ns") or 0) / 1e9 or i / TICK_HZ
    u = float(row.get("steer_raw") or 0.0)
    ro = row.get("road_offset")
    src = row.get("ro_source") or "none"
    hold = src in HOLD_SRC
    new = bool(row.get("dgeo_new")) and not hold and ro is not None
    return dt, ts, u, ro, src, new, hold


def run_recorded(g):
    """A 臂=实录产线状态（executed_lane / v_lat_est），不做任何复刻。"""
    out = []
    prev_fresh = None
    for i, row in enumerate(g):
        dt, ts, u, ro, src, new, hold = tick_inputs(row, i)
        hop = None
        if new:
            if prev_fresh is not None:
                hop = classify_ro_hop(prev_fresh[0], prev_fresh[1],
                                      prev_fresh[2], ts, ro, src)
            prev_fresh = (ts, ro, src)
        out.append(dict(t=(ts or i / TICK_HZ),
                        x=float(row.get("executed_lane") or 0.0),
                        v=float(row.get("v_lat_est") or 0.0), u=u,
                        z=(ro if (ro is not None and not hold) else None),
                        hop=None if hop is None else hop["hop"]))
    return out


def run_kf(g, p, sg=2.0, sz=0.15, qd=0.5, rebase_confirm=2):
    """1-D 卡尔曼：模型加速度不确定（σ_g·|steer|），感知修正按 P 加权。"""
    x = v = steer = 0.0
    P = [[1.0, 0.0], [0.0, 4.0]]
    anchor = None
    prev_fresh = None
    hop_streak = 0
    hop_r = None
    out = []
    for i, row in enumerate(g):
        dt, ts, u, ro, src, new, hold = tick_inputs(row, i)
        steer += (1 - math.exp(-dt / p.tau_steer_s)) * (u - steer)
        a = p.a_lat_gain * steer
        # 预测：双积分 + 线性化协方差传播
        x = x + v * dt + 0.5 * a * dt * dt
        v = v + (a - v / p.tau_align_s) * dt
        v = max(-p.v_lat_max, min(p.v_lat_max, v))
        f01 = dt
        f11 = 1.0 - dt / p.tau_align_s
        p00 = P[0][0] + 2 * dt * P[0][1] + dt * dt * P[1][1]
        p01 = P[0][1] * f11 + dt * P[1][1] * f11
        p11 = P[1][1] * f11 * f11
        qa = (sg * abs(steer)) ** 2 + qd * qd
        p00 += 0.25 * dt ** 4 * qa
        p01 += 0.5 * dt ** 3 * qa
        p11 += dt * dt * qa
        P = [[p00, p01], [p01, p11]]
        hop = None
        if new:
            if prev_fresh is not None:
                hop = classify_ro_hop(prev_fresh[0], prev_fresh[1],
                                      prev_fresh[2], ts, ro, src)
            prev_fresh = (ts, ro, src)
        entry = dict(t=(ts or i / TICK_HZ), x=x, v=v, u=u, z=None,
                     hop=None if hop is None else hop["hop"])
        if ro is not None and not hold:
            obs = ro - (anchor or 0.0)
            entry["z"] = obs
            is_hop = hop is not None and hop["hop"] != 0
            if is_hop:
                # 换档：不喂观测（把不确定度放大），同向连续确认后重基
                r = obs - x
                if hop_streak > 0 and (r > 0) == (hop_r > 0) \
                        and abs(r - hop_r) <= 1.0:
                    hop_streak += 1
                else:
                    hop_streak = 1
                hop_r = r
                p11 += 2.0
                if hop_streak >= rebase_confirm:
                    x = obs
                    P = [[sz * sz, 0.0], [0.0, max(P[1][1], 1.0)]]
                    hop_streak = 0
            else:
                hop_streak = 0
                s = P[0][0] + sz * sz
                k0 = P[0][0] / s
                k1 = P[0][1] / s
                innov = obs - x
                x += k0 * innov
                v += k1 * innov
                v = max(-p.v_lat_max, min(p.v_lat_max, v))
                n00 = (1 - k0) * P[0][0]
                n01 = (1 - k0) * P[0][1]
                n11 = P[1][1] - k1 * P[0][1]
                P = [[n00, n01], [n01, n11]]
        out.append(entry)
    return out


def run_kf2(g, p, sg=2.0, sz=0.15, qd=0.5):
    """KF + 相对速度观测：相邻干净观测差当作速度测量，噪声 2σz²/gap²。"""
    x = v = steer = 0.0
    P = [[1.0, 0.0], [0.0, 4.0]]
    prev_fresh = None
    last_clean = None
    hop_streak = 0
    hop_r = None
    out = []
    for i, row in enumerate(g):
        dt, ts, u, ro, src, new, hold = tick_inputs(row, i)
        steer += (1 - math.exp(-dt / p.tau_steer_s)) * (u - steer)
        a = p.a_lat_gain * steer
        x = x + v * dt + 0.5 * a * dt * dt
        v = v + (a - v / p.tau_align_s) * dt
        v = max(-p.v_lat_max, min(p.v_lat_max, v))
        f11 = 1.0 - dt / p.tau_align_s
        p00 = P[0][0] + 2 * dt * P[0][1] + dt * dt * P[1][1]
        p01 = P[0][1] * f11 + dt * P[1][1] * f11
        p11 = P[1][1] * f11 * f11
        qa = (sg * abs(steer)) ** 2 + qd * qd
        p00 += 0.25 * dt ** 4 * qa
        p01 += 0.5 * dt ** 3 * qa
        p11 += dt * dt * qa
        P = [[p00, p01], [p01, p11]]
        hop = None
        if new:
            if prev_fresh is not None:
                hop = classify_ro_hop(prev_fresh[0], prev_fresh[1],
                                      prev_fresh[2], ts, ro, src)
            prev_fresh = (ts, ro, src)
        entry = dict(t=(ts or i / TICK_HZ), x=x, v=v, u=u, z=None,
                     hop=None if hop is None else hop["hop"])
        if ro is not None and not hold:
            entry["z"] = ro
            if hop is not None and hop["hop"] != 0:
                r = ro - x
                if hop_streak > 0 and (r > 0) == (hop_r > 0) \
                        and abs(r - hop_r) <= 1.0:
                    hop_streak += 1
                else:
                    hop_streak = 1
                hop_r = r
                P[1][1] += 2.0
                if hop_streak >= 2:
                    x = ro
                    P = [[sz * sz, 0.0], [0.0, max(P[1][1], 1.0)]]
                    hop_streak = 0
                last_clean = None
            else:
                hop_streak = 0
                s = P[0][0] + sz * sz
                k0, k1 = P[0][0] / s, P[0][1] / s
                innov = ro - x
                x += k0 * innov
                v += k1 * innov
                n00 = (1 - k0) * P[0][0]
                n01 = (1 - k0) * P[0][1]
                n11 = P[1][1] - k1 * P[0][1]
                P = [[n00, n01], [n01, n11]]
                if last_clean is not None:
                    gap = ts - last_clean[0]
                    if 0.15 <= gap <= 0.6:
                        vo = (ro - last_clean[1]) / gap
                        rv = 2 * sz * sz / (gap * gap)
                        kv = P[1][1] / (P[1][1] + rv)
                        v += kv * (vo - v)
                        v = max(-p.v_lat_max, min(p.v_lat_max, v))
                        P[1][1] *= (1 - kv)
                        P[0][1] *= (1 - kv)
                last_clean = (ts, ro)
        out.append(entry)
    return out


def predict_metrics(trace, g, p):
    """预测下一步：新鲜干净观测拍用状态推演到下次观测，比预测与实测。"""
    errs = {0.25: [], 0.5: []}
    fresh = [i for i, e in enumerate(trace)
             if e["z"] is not None and e["hop"] in (None, 0)]
    for a in range(1, len(fresh)):
        i0, i1 = fresh[a - 1], fresh[a]
        gap = trace[i1]["t"] - trace[i0]["t"]
        if not (0.15 <= gap <= 0.6):
            continue
        key = 0.25 if gap < 0.37 else 0.5
        x, v = trace[i0]["x"], trace[i0]["v"]
        for k in range(i0 + 1, i1 + 1):
            row = g[k]
            dt = float(row.get("dt") or 1 / TICK_HZ)
            u = float(row.get("steer_raw") or 0.0)
            a = p.a_lat_gain * u
            x += v * dt + 0.5 * a * dt * dt
            v += (a - v / p.tau_align_s) * dt
            v = max(-p.v_lat_max, min(p.v_lat_max, v))
        errs[key].append(abs(x - trace[i1]["z"]))
    return errs


def metrics(trace):
    """位置残差 / 横速误差（非跳变、激励段）/ 僵直拍。"""
    pos, verr, blind = [], [], 0
    prev = None
    for e in trace:
        if e["z"] is not None and (e["hop"] in (None, 0)):
            pos.append(abs(e["z"] - e["x"]))
        if prev is not None and e["z"] is not None and (e["hop"] in (None, 0)) \
                and (prev["hop"] in (None, 0)):
            gap = e["t"] - prev["t"]
            if 0.15 <= gap <= 0.5:
                dv = (e["z"] - prev["z"]) / gap
                if abs(e["z"] - prev["z"]) >= 0.15:
                    verr.append(abs(e["v"] - dv))
            prev = e
        elif e["z"] is not None:
            prev = e
    return pos, verr


def _stats(xs):
    if not xs:
        return (0.0, 0.0, 0.0)
    xs = sorted(xs)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    return (q(0.5), q(0.9), xs[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--sg", type=float, default=2.0, help="增益不确定度 道/s²")
    ap.add_argument("--sz", type=float, default=0.15, help="观测噪声 道")
    ap.add_argument("--qd", type=float, default=0.5, help="未建模扰动 道/s²")
    ap.add_argument("--dump", default=None, help="逐拍窗 t0:t1")
    args = ap.parse_args()

    p = load_decision().planner
    games = load_games(args.logs, args.session)
    print("session %s games=%d  KF(sg=%.1f sz=%.2f qd=%.1f)" % (
        args.session, len(games), args.sg, args.sz, args.qd))
    dump = None
    if args.dump:
        a, b = args.dump.split(":")
        dump = (float(a), float(b))
    for gi, g in enumerate(games):
        arms = {"A 实录": run_recorded(g),
                "B KF": run_kf(g, p, args.sg, args.sz, args.qd),
                "C KF+速差": run_kf2(g, p, args.sg, args.sz, args.qd)}
        print("\n-- game%d --" % (gi + 1))
        for name, tr in arms.items():
            pos, verr = metrics(tr)
            sp, sv = _stats(pos), _stats(verr)
            ev = predict_metrics(tr, g, p)
            e25, e50 = _stats(ev[0.25]), _stats(ev[0.5])
            print("  %-8s 位置 p50/p90=%.2f/%.2f | 横速 p50/p90=%.2f/%.2f(n=%d) | "
                  "预测0.25s %.2f/%.2f | 预测0.5s %.2f/%.2f" % (
                      name, sp[0], sp[1], sv[0], sv[1], len(verr),
                      e25[0], e25[1], e50[0], e50[1]))
        if dump:
            print("  逐拍 (t | 观测 | A x/v | C x/v | u)：")
            for i, e in enumerate(arms["A 实录"]):
                t = e["t"]
                if not (dump[0] <= t <= dump[1]):
                    continue
                ec = arms["C KF+速差"][i]
                print("  %6.2f z=%6s | %+5.2f v%+5.2f | %+5.2f v%+5.2f | u%+5.2f" % (
                    t, ("%+.2f" % e["z"]) if e["z"] is not None else "NA",
                    e["x"], e["v"], ec["x"], ec["v"], e["u"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
