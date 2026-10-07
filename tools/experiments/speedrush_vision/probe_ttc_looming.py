# -*- coding: utf-8 -*-
"""looming-TTC 离线探针（刀二消费端准入证据，stdout-only 零文件写）。

问题：尺度无关 TTC = Δt / ln(h_now/h_prev)（纯框高流，不依赖锚点刻度）
能否当「到掠过时刻的倒计时」用——对照真值 = 该轨峰值框高拍（车掠过相机）。

背景：三个像素系 TTC 消费点（score_car t_meet / dodge ttc / LateralSafety
_eta）全用线性行模型 (v_ego−cy)/(rel·hz)，_t_miss_ground_s 注释自证该式在
远行低估一半量级。若 looming-TTC 无偏，消费端可以不碰绝对距离（免疫锚点
刻度偏差与重标锯齿）直接换时间量。

用法：.venv python probe_ttc_looming.py <trace.jsonl> [第二个 ...]
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict

DT_BEAT_MS = 33.0          # 控制拍周期（30fps）
PASS_MIN_H = 150           # 峰值框高阈值：确实掠过近处的轨才进真值集
PASS_END_FRAC = 0.6        # 末拍框高 ≥ 峰值的 60%：丢在近处（掠过）而非远处淡出
WINDOWS_MS = (200.0, 250.0, 350.0, 500.0)
TTC_CAP_S = 30.0           # h 几乎不涨时 ln→0，封顶
MIN_GROWTH = 1.02          # 区间增长 <2% 视为不接近，报 inf


def load_tracks(path: str) -> dict[int, list[tuple[int, int]]]:
    tracks: dict[int, list[tuple[int, int]]] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            b = json.loads(line)
            for e in b.get("car_h") or []:
                tracks[e["id"]].append((b["fid"], e["h"]))
    return {k: sorted(v) for k, v in tracks.items()}


def pass_fid(seq: list[tuple[int, int]]) -> int | None:
    """掠过拍 = 峰值框高拍；末拍仍须占峰值六成以上（丢在近处=掠过，
    远处淡出=中途脱靶，不配当真值）。"""
    if len(seq) < 10:
        return None
    hmax = max(h for _, h in seq)
    if hmax < PASS_MIN_H:
        return None
    if seq[-1][1] < PASS_END_FRAC * hmax:
        return None
    return max(seq, key=lambda x: x[1])[0]


def looming_ttc(h_now: int, h_prev: int, win_ms: float) -> float:
    if h_now <= 0 or h_prev <= 0 or h_now < h_prev * MIN_GROWTH:
        return TTC_CAP_S
    return min(win_ms / math.log(h_now / h_prev) / 1000.0, TTC_CAP_S)


def main() -> None:
    for path in sys.argv[1:]:
        tracks = load_tracks(path)
        n_tracks = len(tracks)
        n_pass = 0
        # 误差桶：按真值 TTC 分桶 + 按框高分桶（远距量化噪声的量级实测）
        err_by_ttc: dict[str, list[float]] = defaultdict(list)
        err_by_h: dict[str, list[float]] = defaultdict(list)
        bias_by_win: dict[float, list[float]] = defaultdict(list)
        for seq in tracks.values():
            pf = pass_fid(seq)
            if pf is None:
                continue
            n_pass += 1
            hmap = dict(seq)
            for i, (fid, h) in enumerate(seq):
                if fid >= pf:
                    continue
                true_s = (pf - fid) * DT_BEAT_MS / 1000.0
                if true_s < 0.1:
                    continue
                for w in WINDOWS_MS:
                    prev = None
                    for j in range(i - 1, -1, -1):
                        dt_ms = (fid - seq[j][0]) * DT_BEAT_MS
                        if dt_ms > w + DT_BEAT_MS:
                            break
                        prev = (dt_ms, seq[j][1])   # 取 dt≤w+一拍 内最远一拍
                    if prev is None:
                        continue
                    est = looming_ttc(h, prev[1], prev[0])
                    rel = (est - true_s) / true_s
                    bias_by_win[w].append(rel)
                    if w == WINDOWS_MS[0]:
                        bucket = ("<0.5" if true_s < 0.5 else
                                  "0.5-1" if true_s < 1.0 else
                                  "1-2" if true_s < 2.0 else ">2")
                        err_by_ttc[bucket].append(rel)
                        hb = ("h<40" if h < 40 else
                              "40-100" if h < 100 else "h>100")
                        err_by_h[hb].append(rel)

        def _stat(xs: list[float]) -> str:
            if not xs:
                return "n=0"
            xs = sorted(xs)
            p50 = xs[len(xs) // 2]
            p10, p90 = xs[int(len(xs) * 0.1)], xs[int(len(xs) * 0.9)]
            return f"n={len(xs)} 偏差 p50={p50:+.2f} p10={p10:+.2f} p90={p90:+.2f}"

        print(f"\n=== {path.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]} ===")
        print(f"轨总数 {n_tracks}，有效掠过轨 {n_pass}")
        for w, xs in sorted(bias_by_win.items()):
            print(f"[窗口 {w:.0f}ms] 总体相对误差: {_stat(xs)}")
        print("按真值倒计时分桶（窗口 250ms）:")
        for k in ("<0.5", "0.5-1", "1-2", ">2"):
            print(f"  true TTC {k:>5}s: {_stat(err_by_ttc.get(k, []))}")
        print("按当拍框高分桶（窗口 250ms）:")
        for k in ("h<40", "40-100", "h>100"):
            print(f"  {k:>7}: {_stat(err_by_h.get(k, []))}")


if __name__ == "__main__":
    main()
