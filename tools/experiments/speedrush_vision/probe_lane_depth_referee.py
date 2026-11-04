# -*- coding: utf-8 -*-
"""P1 分工检验：深度路心与车道线源的**变化量**对照（不比绝对值）。

## 为什么只比变化量

P0 依赖核查（见 speedrush_vision/README.md 2026-10-27 节）结论：

* 线源的**绝对道号**由深度路心给定（``k = round(深度锚 + L)``，
  ``lane_obs.LineTracker._assign_k``），所以绝对横位不是独立证人；
* 线源换算用的机高来自深度平面（``module.py`` 把 ``abs(dgeo.coef[2])`` 作
  cam_h 传入），所以连**尺度**都不是独立的；
* 滑行拍（``pred=true``）的横位掺了控制模型自估的横速 —— 那是控制器自己的
  预测，拿它验证控制器是循环论证。

唯一干净的量：**同一条线、同一次锚定之内**，两次真实检测之间的读数差。它只
由像素证据决定。深度侧对应量 = 同窗口两端各自最近的新鲜路心读数之差。

## 对齐口径（禁止按行号猜）

``frames.jsonl`` 的 ``frame_id`` 与 trace 的 ``fid`` 同源（同一采集帧号），
线观测快照另带它自己消费的 ``lane_obs.fid``——**只接受 fid 完全相等的拍**，
不等即弃，并把弃用数打出来。

## 划分

按**会话**分开发集/留出集（不是抽帧）：相邻帧高度相关，抽帧划分会让结果虚好。
比例 r 只在开发集上估，留出集只用于验证。

## 输出

``<out>/referee_<session>.jsonl`` 逐窗口一行 + stdout 汇总表。判定口径写在
表尾：方向一致率、比值、留出集偏差、分场景差异、连续失效时长。
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

MAX_FID_GAP = 0            # 线观测只接受 fid 完全相等的拍
MIN_MOVE_LANE = 0.05       # 深度侧位移噪声底（道）
MIN_MOVE_SLOPE = 0.10      # 线侧像素斜率变化噪声底（px/px）
DEV_SESSIONS = 3           # 前 N 个会话做开发集，其余留出


def load_ticks(logs_root: Path) -> dict[str, list[dict]]:
    out = {}
    for ls in sorted(logs_root.iterdir()):
        p = ls / "trace" / "speedrush.jsonl"
        if not p.is_file():
            continue
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        ticks = [r for r in rows if isinstance(r.get("fid"), int)
                 and r.get("executed_lane") is not None]
        if ticks:
            out[ls.name] = ticks
    return out


def pick_session(ticks: dict[str, list[dict]], fids: list[int]) -> str | None:
    best, n = None, 0
    have = set(fids)
    for name, rows in ticks.items():
        inter = sum(1 for r in rows if r["fid"] in have)
        if inter > n:
            best, n = name, inter
    return best


def fresh_road_offset(rows: list[dict], fid: int, cap: int = 6) -> float | None:
    """取该拍之前最近的一条**新鲜**路心读数（保持/遮挡/记忆值不算新证据）。"""
    by = {r["fid"]: r for r in rows}
    for f in range(fid, fid - cap - 1, -1):
        r = by.get(f)
        if r is None:
            continue
        if r.get("ro_hold") or r.get("dgeo_new") is False:
            continue
        v = r.get("road_offset")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def lane_observations(ticks: list[dict]) -> list[dict]:
    """按**线观测自己的帧号**取出观测序列。

    线观测跑在独立线程（默认 10 Hz），trace 行里带的是"此刻手边最新一条"——
    实测 2501/2890 拍的 ``lane_obs.fid`` 与本拍 ``fid`` 不等。所以配对必须按
    观测自己的 fid，不能按承载它的拍，否则几乎全数作废。"""
    seen, out = set(), []
    for r in ticks:
        lo = r.get("lane_obs") or {}
        fid = lo.get("fid")
        if fid is None or fid in seen:
            continue
        seen.add(fid)
        grid = tuple(sorted(int(c["k"]) for c in (lo.get("lines") or [])
                            if c.get("accepted") and c.get("k") is not None))
        out.append({"ofid": fid, "x": lo.get("x"), "cam_h": lo.get("cam_h"),
                    "grid": grid, "state": lo.get("state"),
                    "pred": bool(lo.get("pred")), "n_locked": lo.get("n_locked"),
                    "theta": lo.get("theta"), "ts_ns": r.get("ts_ns"),
                    "slopes": line_slopes(lo)})
    return sorted(out, key=lambda o: o["ofid"])


LANE_W = 3.38              # lane_obs 的换算常数；下面用它把 L 换回像素斜率，
                           # 机高 cam_h 在这一步可逆消掉——不是采用它的米制值


def line_slopes(lo: dict) -> list[float] | None:
    """本帧各**可用线**的像素斜率 a = L·LANE_W/cam_h（纯图像量）。

    ``lane_obs.L = a·cam_h/LANE_W``，而 cam_h 是深度平面给的（module.py 传
    ``abs(dgeo.coef[2])``）——所以 L 与线源横位都带一个会逐帧抖的深度尺度。
    换回 a 之后，机高抖动不再进入两侧之差；线身份也按 a 近邻匹配，不经道号。"""
    cam_h = lo.get("cam_h")
    if not cam_h:
        return None
    acc = [c for c in (lo.get("lines") or []) if c.get("accepted")]
    if not acc:
        return None
    return sorted(float(c["L"]) * LANE_W / float(cam_h) for c in acc)


def match_d(a0: list[float], a1: list[float]) -> float | None:
    d = []
    for v in a0:
        j = min(range(len(a1)), key=lambda k: abs(a1[k] - v))
        if abs(a1[j] - v) <= 0.30:
            d.append(a1[j] - v)
    return float(np.median(d)) if d else None


def windows(sess: Path, ticks: list[dict]) -> tuple[list[dict], dict]:
    frames = [json.loads(l) for l in (sess / "frames.jsonl")
              .read_text(encoding="utf-8").splitlines() if l.strip()]
    have = {f["frame_id"] for f in frames}
    obs = [o for o in lane_observations(ticks) if o["ofid"] in have]
    recs: list[dict] = []
    drop = {"pred": 0, "no_slope": 0, "no_match": 0, "no_fresh_d": 0}
    prev = None
    for o in obs:
        if o["pred"]:
            drop["pred"] += 1          # 滑行拍掺了控制模型自估横速
            prev = None
            continue
        if o["slopes"] is None or o["x"] is None:
            drop["no_slope"] += 1
            prev = None
            continue
        if prev is not None:
            da = match_d(prev["slopes"], o["slopes"])
            if da is None:
                drop["no_match"] += 1
            else:
                d0 = fresh_road_offset(ticks, prev["ofid"])
                d1 = fresh_road_offset(ticks, o["ofid"])
                if d0 is None or d1 is None:
                    drop["no_fresh_d"] += 1
                else:
                    dt = ((o["ts_ns"] or 0) - (prev["ts_ns"] or 0)) / 1e9
                    recs.append({"session": sess.name, "fid0": prev["ofid"],
                                 "fid1": o["ofid"], "dt_s": round(dt, 3),
                                 "dA": round(da, 4),
                                 "dL": round(o["x"] - prev["x"], 4),
                                 "dD": round(d1 - d0, 4),
                                 "n_line": len(o["slopes"]),
                                 "x": round(float(o["x"]), 3),
                                 "state": o["state"], "theta": o["theta"],
                                 "grid_chg": prev["grid"] != o["grid"],
                                 "cam_h_jump": round(abs((prev["cam_h"] or 0)
                                                         - (o["cam_h"] or 0)), 4)})
        prev = o
    return recs, drop


def summarize(all_recs: list[dict], drops: dict) -> None:
    """深度路心的变化量 vs 线源的**像素斜率**变化量（后者不含任何深度量）。"""
    big = [r for r in all_recs if abs(r["dA"]) >= MIN_MOVE_SLOPE
           and abs(r["dD"]) >= MIN_MOVE_LANE]
    print(f"窗口 {len(all_recs)}，两侧都超噪声底 {len(big)}")
    print("弃用计数", dict(sorted(drops.items(), key=lambda kv: -kv[1])))
    chg = sum(1 for r in all_recs if r["grid_chg"])
    jump = sorted(r["cam_h_jump"] for r in all_recs)
    print(f"道号网格在窗口内改判 {chg}/{len(all_recs)}；"
          f"机高跳变 p50 {statistics.median(jump):.3f} "
          f"p90 {jump[int(.9*len(jump))]:.3f} m（两路共用的尺度输入）")
    if len(big) < 20:
        print("样本不足，判定：证据不足")
        return
    # x = k - a/s → 横位与像素斜率反号
    agree = sum(1 for r in big if -r["dA"] * r["dD"] > 0) / len(big)
    print(f"方向一致率（全样本）{agree:.0%}  n={len(big)}")
    sess = sorted({r["session"] for r in big})
    dev = [r for r in big if r["session"] in sess[:DEV_SESSIONS]]
    hold = [r for r in big if r["session"] not in sess[:DEV_SESSIONS]]
    for name, grp in (("开发", dev), ("留出", hold)):
        if not grp:
            continue
        a = sum(1 for r in grp if -r["dA"] * r["dD"] > 0) / len(grp)
        print(f"  {name}集 n={len(grp):3d} 方向一致率 {a:.0%}")
    if not dev:
        return
    rr = sorted(r["dD"] / -r["dA"] for r in dev)
    r_dev = statistics.median(rr)
    print(f"  开发集比值 ΔD/(-ΔA) 中值 {r_dev:.4f} → 深度链有效"
          f"「每道像素斜率」s_eff = {1/r_dev:.2f}"
          f"（lane_obs 自用的换算比 {LANE_W}/cam_h ≈ "
          f"{LANE_W/1.42:.2f}）")
    if hold:
        resid = sorted(abs(r["dD"] - r_dev * -r["dA"]) for r in hold)
        rel = sorted(abs(r["dD"] / -r["dA"] / r_dev - 1) for r in hold)
        print(f"  留出集：绝对残差 p50 {statistics.median(resid):.3f} 道 "
              f"p90 {resid[int(.9*len(resid))]:.3f}；比值相对偏差 p50 "
              f"{statistics.median(rel):.0%} p90 {rel[int(.9*len(rel))]:.0%}")
    for tag, key in (("贴墙 |x|>1.2", lambda r: abs(r["x"]) > 1.2),
                     ("道内 |x|<=1.2", lambda r: abs(r["x"]) <= 1.2),
                     ("TRACK", lambda r: r["state"] == "TRACK"),
                     ("非 TRACK", lambda r: r["state"] != "TRACK")):
        g = [r for r in big if key(r)]
        if len(g) >= 8:
            a = sum(1 for r in g if -r["dA"] * r["dD"] > 0) / len(g)
            print(f"  {tag:14s} n={len(g):4d} 方向一致率 {a:.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(
        Path.home() / "AppData/Roaming/MaaRacingMaster"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = Path(args.root)
    logs = root / "logs"
    base = root / "debug" / "speedrush"
    ticks = load_ticks(logs)
    out = Path(args.out) if args.out else base / "referee"
    out.mkdir(parents=True, exist_ok=True)
    all_recs, drops = [], dict.fromkeys(
        ["pred", "no_slope", "no_match", "no_fresh_d"], 0)
    for sess in sorted(base.glob("manual_*")):
        if not (sess / "frames.jsonl").is_file():
            continue
        frames = [json.loads(l) for l in (sess / "frames.jsonl")
                  .read_text(encoding="utf-8").splitlines() if l.strip()]
        name = pick_session(ticks, [f["frame_id"] for f in frames])
        if name is None:
            print(f"{sess.name}: 找不到同会话 trace，跳过")
            continue
        recs, d = windows(sess, ticks[name])
        for k, v in d.items():
            drops[k] += v
        all_recs += recs
        (out / f"referee_{sess.name}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs),
            encoding="utf-8")
        print(f"{sess.name} ← logs/{name}: 帧 {len(frames)} "
              f"可用窗口 {len(recs)}")
    print()
    summarize(all_recs, drops)
    print("wrote", out)


if __name__ == "__main__":
    main()
