# -*- coding: utf-8 -*-
"""fov↔speed 标定探针（stdout-only，2026-10-08 预登记，GPT 三问）。

背景（probe_moge_fov_ramp.py 判决的下一刀）：静止→全速窗口中位 +11.4°
支持动态透视，恒速段单帧散布 74~99.5° 否决单帧速度计。本探针回答标定
三问——①单调吗 ②可逆吗（同速下加速段/减速段 fov 是否同值）③哪些速度
区间塌缩（fov 不再随速度变化）。

数据全部现成：control_traces/depth_debug_*/ 的 evid 每帧已存产线恢复的
归一化焦距 fx/fy（cx=cy=0.5 口径，fov_x=2·atan(0.5/fx)）；配对
hud_*/hud.jsonl 的 trusted rate_b 是 m/s 真值（底值≤42，>42 必含得分
事件，弃）。rate_b 是滞后 1s 的滑窗均值，取 t+0.5s 最近采样近似对齐。

会话对齐：depth_debug_<ts> ↔ hud_<ts>_p*（fid 同一游戏计数器，已验证
evid fx 与 raw 帧探针逐帧一致）；fid<0 的预启动帧弃；fid 超出 hud 覆盖
范围弃。

用法：.venv python probe_fov_speed_calib.py <control_traces_dir>
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

RATE_MAX = 42          # 底值上限（全速 41~42 m/s；>42 必含得分事件）
LAG_S = 0.5            # rate_b 滞后 1s 滑窗的近似对齐
LAUNCH_WIN_S = 6.0     # 每局起步爬坡窗（首个 trusted rate 行起）


def fov_of(fx: float) -> float:
    return 2.0 * math.degrees(math.atan(0.5 / fx))


def load_session(dd_dir: str) -> list[dict]:
    stem = os.path.basename(dd_dir).replace("depth_debug_", "")
    hud_dirs = sorted(glob.glob(os.path.join(os.path.dirname(dd_dir), f"hud_{stem}_p*")))
    hud_paths = [os.path.join(h, "hud.jsonl") for h in hud_dirs
                 if os.path.exists(os.path.join(h, "hud.jsonl"))]
    if not hud_paths:
        return []
    rows = [json.loads(l) for l in open(hud_paths[0], encoding="utf-8") if l.strip()]
    hud = [(r["frame_id"], r["ts_ns"]) for r in rows]
    trusted = [(r["frame_id"], r["ts_ns"], r["fields"]["rate_b"]["value"])
               for r in rows if (r["fields"].get("rate_b") or {}).get("trusted")]
    if not trusted:
        return []

    def t_of_fid(fid: int) -> float | None:
        if fid < hud[0][0] or fid > hud[-1][0]:
            return None
        for (f0, t0), (f1, t1) in zip(hud, hud[1:]):
            if f0 <= fid <= f1:
                return t0 + (t1 - t0) * (fid - f0) / max(f1 - f0, 1)
        return None

    def rate_at(t: float) -> int | None:
        best = min(trusted, key=lambda r: abs(r[1] / 1e9 - t))
        return best[2] if abs(best[1] / 1e9 - t) <= 1.0 else None

    pts: list[dict] = []
    for npz in sorted(glob.glob(os.path.join(dd_dir, "*_evid.npz"))):
        z = np.load(npz)
        if "fid" not in z.files:   # 早期 schema 无 fid，无法对齐 hud，弃
            continue
        fid = int(z["fid"])
        if fid < 0:
            continue
        t = t_of_fid(fid)
        if t is None:
            continue
        rate = rate_at(t / 1e9 + LAG_S)
        if rate is None:
            continue
        pts.append({"fid": fid, "t": t / 1e9, "rate": rate,
                    "fov": fov_of(float(z["fx"]))})
    return pts


def p50(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def main() -> None:
    root = sys.argv[1]
    sessions: dict[str, list[dict]] = {}
    for dd in sorted(glob.glob(os.path.join(root, "depth_debug_*"))):
        pts = load_session(dd)
        if pts:
            sessions[os.path.basename(dd)] = pts
    total = sum(len(v) for v in sessions.values())
    print(f"会话 {len(sessions)}  点 {total}")

    # 每局对照（地图偏置受控）：局内低速率 vs 高速率 fov 中位与位移
    print("\n[每局对照] 局内 rate≤8 vs rate≥38 的 fov 中位（各图场景偏置不同，局内自比才干净）")
    print(f"  {'会话':<26} {'n低':>4} {'低中位':>7} {'n高':>4} {'高中位':>7} {'位移':>6}")
    shifts = []
    for name, pts in sessions.items():
        lo = [p["fov"] for p in pts if p["rate"] <= 8]
        hi = [p["fov"] for p in pts if p["rate"] >= 38]
        if len(lo) >= 4 and len(hi) >= 8:
            d = p50(hi) - p50(lo)
            shifts.append(d)
            print(f"  {name.replace('depth_debug_', ''):<26} {len(lo):>4} {p50(lo):>7.2f} "
                  f"{len(hi):>4} {p50(hi):>7.2f} {d:>+6.2f}")
    if shifts:
        print(f"  位移分布: n={len(shifts)} 中位 {p50(shifts):+.2f}  最小 {min(shifts):+.2f}  最大 {max(shifts):+.2f}  全为正: {all(s > 0 for s in shifts)}")

    # ① 每局起步窗单调性（Spearman(fov, rate)，窗内 rate 需有变化）
    import statistics
    print("\n[① 起步窗单调性] 每局前 6s，Spearman(fov_x, rate_b)")
    pos = neg = flat = 0
    for name, pts in sessions.items():
        t0 = pts[0]["t"]
        win = [p for p in pts if p["t"] - t0 <= LAUNCH_WIN_S]
        rates = {p["rate"] for p in win}
        if len(win) < 5 or len(rates) < 3:
            continue
        rs = [p["rate"] for p in win]
        fs = [p["fov"] for p in win]
        def rank(a):
            order = sorted(range(len(a)), key=lambda i: a[i])
            r = [0.0] * len(a)
            for k, i in enumerate(order):
                r[i] = float(k)
            return r
        mr, mf = statistics.mean(rs), statistics.mean(fs)
        sr = (sum((x - mr) ** 2 for x in rs)) ** 0.5
        sf = (sum((x - mf) ** 2 for x in fs)) ** 0.5
        rho = (sum((a - mr) * (b - mf) for a, b in zip(rank(rs), rank(fs))) / (sr * sf)) if sr and sf else 0.0
        pos += rho > 0.5
        neg += rho < -0.5
        flat += abs(rho) <= 0.5
    print(f"  强正相关(ρ>0.5) {pos}  强负相关 {neg}  平/弱 {flat}")

    # ②③ 池化标定曲线：rate 整数分箱
    print(f"\n[②③ 池化标定] fov_x 中位 by rate_b（lag+{LAG_S}s 对齐，≤{RATE_MAX}）")
    bins: dict[int, list[float]] = defaultdict(list)
    for pts in sessions.values():
        for p in pts:
            if p["rate"] <= RATE_MAX:
                bins[p["rate"]].append(p["fov"])
    print(f"  {'rate':>4} {'n':>5} {'fov中位':>8} {'IQR':>14}")
    curve: list[tuple[int, float, int]] = []
    for r in sorted(bins):
        xs = sorted(bins[r])
        q1, q3 = xs[len(xs) // 4], xs[(3 * len(xs)) // 4]
        curve.append((r, p50(xs), len(xs)))
        print(f"  {r:>4} {len(xs):>5} {p50(xs):>8.2f} [{q1:.1f},{q3:.1f}]")

    # 塌缩区：相邻有效箱（n≥5）中位差
    seg = [(r, f) for r, f, n in curve if n >= 5]
    print("\n  相邻箱中位差（Δfov / 1 m/s）:")
    for (r0, f0), (r1, f1) in zip(seg, seg[1:]):
        flag = "  ← 塌缩" if abs(f1 - f0) < 0.3 else ""
        print(f"    {r0:>2}→{r1:<2} {f1 - f0:+.2f}{flag}")

    # ② 可逆性：局部趋势（rate(t±1s) 比较）分组的同速 fov 差
    print("\n[② 可逆性] 同一 rate 箱内，加速点 vs 减速点 fov 中位差")
    rise: dict[int, list[float]] = defaultdict(list)
    fall: dict[int, list[float]] = defaultdict(list)
    for pts in sessions.values():
        ts = [p["t"] for p in pts]
        for p in pts:
            if p["rate"] > RATE_MAX:
                continue
            before = [q["rate"] for q in pts if 0 < p["t"] - q["t"] <= 1.0]
            after = [q["rate"] for q in pts if 0 < q["t"] - p["t"] <= 1.0]
            if not before or not after:
                continue
            tgt = rise if after[-1] > before[-1] else fall
            tgt[p["rate"]].append(p["fov"])
    print(f"  {'rate':>4} {'n升':>4} {'升中位':>7} {'n降':>4} {'降中位':>7} {'差':>6}")
    for r in sorted(set(rise) & set(fall)):
        a, b = rise[r], fall[r]
        if len(a) >= 4 and len(b) >= 4:
            print(f"  {r:>4} {len(a):>4} {p50(a):>7.2f} {len(b):>4} {p50(b):>7.2f} {p50(a) - p50(b):>+6.2f}")


if __name__ == "__main__":
    main()
