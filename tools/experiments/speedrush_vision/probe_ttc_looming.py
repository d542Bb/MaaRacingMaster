# -*- coding: utf-8 -*-
"""TTC 估计器对照探针 v3（刀二消费端准入证据，stdout-only 零文件写）。

问题：换装 TTC 消费点前，incumbent 线性行模型与 looming（尺度无关）谁更准——
  E1 looming = Δt / ln(h_now/h_prev)   （纯框高流，免疫锚刻度偏差与重标锯齿）
  E2 线性行 = (v_ego − cy) / (rel·hz)  （现产线三消费点用的式子）
真值 = 该轨峰值框高拍（掠过相机）的实际倒计时。

v3 修订（v1/v2 的教训，两处结构性污染）：
  1. **滑行段**：遮挡期跟踪层按外推纪律滑行——h/x/rel 冻结、cy 合成。滑行拍
     不是观测（h 流已死），喂估计器必爆（ln 分母趋零）；峰值 h 拍也因滑行
     提前，真值本身错。修法：拍级「新鲜过滤」（h 或 rel 相对前拍有变化）+
     真值集只收「尾部仍新鲜」的轨（检测撑到近掠过才丢）。
  2. **机动/横向分层**（car_pos 列，commit 8493016）：无机动·邻道桶的实际
     掠过 ≈ 反事实——此桶偏差才是估计器误差；机动桶混入反事实差（安全
     消费端要的恰是估计器读到的「不机动会怎样」）。

用法：.venv python probe_ttc_looming.py <trace.jsonl> [--v-ego=716.0]
（car_pos 缺失的旧 trace 自动退化为仅 E1、无分层。）
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict

PASS_MIN_H = 150           # 峰值框高阈值：确实掠过近处的轨才进真值集
PASS_END_FRAC = 0.6        # 末拍框高 ≥ 峰值的 60%：丢在近处（掠过）而非远处淡出
FRESH_TAIL_BEATS = 2       # 峰值前 N 拍须新鲜（检测撑到近掠过），否则真值不可知
WINDOWS_MS = (200.0, 250.0, 350.0, 500.0)
TTC_CAP_S = 30.0           # h 几乎不涨时 ln→0，封顶
MIN_GROWTH = 1.02          # 区间增长 <2% 视为不接近
HZ = 20.0                  # 控制名义频率（decision.json control.frame_rate_hz）
TRUE_LO, TRUE_HI = 0.3, 3.0   # 消费端相关真值区间（s）
MANEUVER_LANES = 0.25      # 掠过前 ego 横移超此值 = 机动
OUR_LANE_GAP = 0.6         # 横向间隔 < 此值（道）= 本车道


def load(path: str):
    with open(path, encoding="utf-8") as f:
        beats = [json.loads(l) for l in f if l.strip()]
    beats.sort(key=lambda b: b["fid"])
    tracks: dict[int, list[dict]] = defaultdict(list)
    for b in beats:
        pos = {e["id"]: e for e in b.get("car_pos") or []}
        for e in b.get("car_h") or []:
            p = pos.get(e["id"], {})
            tracks[e["id"]].append({
                "fid": b["fid"], "ts": b["ts_ns"], "h": e["h"],
                "x": p.get("x"), "cy": p.get("cy"), "rel": p.get("rel"),
                "exec": b.get("executed_lane")})
    out = {}
    for k, seq in tracks.items():
        seq.sort(key=lambda r: r["fid"])
        for i, r in enumerate(seq):   # 新鲜 = h 或 rel 相对前拍有变化（滑行段两者全冻）
            prev = seq[i - 1] if i else None
            r["fresh"] = prev is not None and (
                r["h"] != prev["h"]
                or (r["rel"] is not None and prev["rel"] is not None
                    and r["rel"] != prev["rel"]))
        out[k] = seq
    return out


def pass_ts(seq: list[dict]) -> float | None:
    """掠过拍 = 峰值框高拍；峰值及其前 FRESH_TAIL_BEATS 拍须新鲜，
    且末拍框高 ≥ 峰值六成（丢在近处而非远处淡出）。"""
    if len(seq) < 10:
        return None
    hmax = max(r["h"] for r in seq)
    if hmax < PASS_MIN_H or seq[-1]["h"] < PASS_END_FRAC * hmax:
        return None
    peak = max(seq, key=lambda r: r["h"])
    pi = seq.index(peak)
    if pi < FRESH_TAIL_BEATS or not all(
            seq[j]["fresh"] for j in range(pi - FRESH_TAIL_BEATS, pi + 1)):
        return None
    return peak["ts"]


def looming_ttc(seq: list[dict], i: int, w_ms: float) -> float | None:
    """h 流上找 dt 最接近 w 的前一拍，TTC = dt/ln 比。只在新鲜拍上算。"""
    r = seq[i]
    prev = None
    for j in range(i - 1, -1, -1):
        dt_ms = (r["ts"] - seq[j]["ts"]) / 1e6
        if dt_ms > w_ms + 60:
            break
        if seq[j]["fresh"]:
            prev = (dt_ms, seq[j]["h"])
    if prev is None:
        return None
    return min(prev[0] / math.log(r["h"] / prev[1]) / 1000.0, TTC_CAP_S) \
        if r["h"] >= prev[1] * MIN_GROWTH and prev[1] > 0 else TTC_CAP_S


def pixel_ttc(r: dict, v_ego_row: float) -> float | None:
    if r["rel"] is None or r["rel"] <= 1e-9 or r["cy"] is None:
        return None
    return max(0.0, v_ego_row - r["cy"]) / (r["rel"] * HZ)


def main() -> None:
    # v_ego 行号出处：plugins/<id>/resources 的标定（world_model.load_calib 读数，
    # 2026-10-07 为 716.0）——实验探针不 import 生产代码，标定变了这里要跟
    v_ego_row = 716.0
    args = []
    for a in sys.argv[1:]:
        if a.startswith("--v-ego="):
            v_ego_row = float(a.split("=", 1)[1])
        else:
            args.append(a)

    for path in args:
        tracks = load(path)
        rows: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        n_pass = n_coast = 0
        for seq in tracks.values():
            pts = pass_ts(seq)
            if pts is None:
                continue
            n_pass += 1
            for i, r in enumerate(seq):
                if not r["fresh"] or r["ts"] >= pts:
                    continue
                true_s = (pts - r["ts"]) / 1e9
                if not (TRUE_LO <= true_s <= TRUE_HI):
                    continue
                if r["x"] is None:
                    cls = "域外"        # 无横向读数：不做机动/横向分层
                else:
                    execs = [q["exec"] for q in seq[i:]
                             if q["exec"] is not None and q["ts"] <= pts]
                    maneuvered = (max(execs) - min(execs) > MANEUVER_LANES) \
                        if len(execs) >= 2 else True
                    gap = abs(r["x"] - (r["exec"] or 0.0))
                    cls = ("机动" if maneuvered else
                           "无机动·本道" if gap < OUR_LANE_GAP else "无机动·邻道")
                e1 = looming_ttc(seq, i, 250.0)
                if e1 is not None:
                    rows[(cls, "E1_looming", "all")].append((e1 - true_s) / true_s)
                    if cls == "无机动·邻道":
                        rows[(cls, "E1_looming",
                              "真值<1s" if true_s < 1.0 else "真值≥1s")].append(
                                  (e1 - true_s) / true_s)
                e2 = pixel_ttc(r, v_ego_row)
                if e2 is not None:
                    rows[(cls, "E2_pixel", "all")].append((e2 - true_s) / true_s)
                    if cls == "无机动·邻道":
                        rows[(cls, "E2_pixel",
                              "真值<1s" if true_s < 1.0 else "真值≥1s")].append(
                                  (e2 - true_s) / true_s)

        def _stat(xs: list[float]) -> str:
            if not xs:
                return "n=0"
            xs = sorted(xs)
            return (f"n={len(xs)} 偏差 p50={xs[len(xs)//2]:+.2f} "
                    f"p10={xs[int(len(xs)*0.1)]:+.2f} p90={xs[int(len(xs)*0.9)]:+.2f}")

        print(f"\n=== {path.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]} ===")
        print(f"有效掠过轨 {n_pass}（真值区间 {TRUE_LO}~{TRUE_HI}s，窗口 250ms，"
              f"仅新鲜拍；滑行穿场的轨已从真值集剔除）")
        for cls in ("无机动·邻道", "无机动·本道", "机动", "域外"):
            for est in ("E1_looming", "E2_pixel"):
                xs = [v for (c, e, _), vs in rows.items() if c == cls and e == est
                      for v in vs]
                if xs:
                    print(f"  {cls} {est}: {_stat(xs)}")
        print("  [判决桶·无机动·邻道 分真值]")
        for est in ("E1_looming", "E2_pixel"):
            for b in ("真值<1s", "真值≥1s"):
                print(f"    {est} {b}: {_stat(rows.get(('无机动·邻道', est, b), []))}")


if __name__ == "__main__":
    main()
