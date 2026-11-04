"""护栏板缝的「过缝率」探针——不靠逐点对应，只测固定像素列上的亮度振荡频率。

**原理**：隔音墙/护栏的板缝沿道路方向近似等间距 s 米。相机以速度 V 前进时，任一固定
图像列（只要它落在墙面内）会被板缝依次扫过，亮度于是以 **f = V / s** 振荡。这是**频率
测量**：不需要知道哪根缝对哪根缝，不受重复纹理的对应歧义影响，也不受低纹理沥青影响。
20 Hz 采样对 1~3 Hz 的信号有 7~20 倍余量（奈奎斯特 10 Hz，即 s ≥ 4 m 才混叠）。

**要回答什么**：
1. 墙面列上到底有没有一个**突出的**振荡峰（相对噪声地板的比）；
2. 峰频率在左右两侧、不同列、不同时间窗上是否**一致**——一致才说明它是同一个物理量
  （同一个 s），而不是画面噪声或游戏内的闪烁；
3. 由 f 反推的 s = V/f 是否是** plausible 的常数**（几米到几十米的板缝间距）。

对照：同一列取天空/远景区间（不该以 V/s 振荡）给出噪声地板。

用法：
    python tools/experiments/speedrush_geom/probe_barrier_rate.py 20261022_172231_p2 \
        --truth 41.0 [--lo 100 --hi 900]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import median_filter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "experiments" / "speedrush_geom"))

import probe_repeat_and_flow as R  # noqa: E402

DEBUG = R.DEBUG
DT = 0.05              # 统一重采样步长（20 Hz）
F_LO, F_HI = 0.3, 9.0  # 搜索带（Hz）
NFFT = 1 << 15
# (名, 列, 起行, 止行)：墙面落在左/右侧的行带，另给天空对照
PROBES = (
    ("左墙A", 70, 400, 440), ("左墙B", 120, 395, 430), ("左墙C", 180, 385, 415),
    ("左墙D", 260, 375, 405),
    ("右墙A", 1210, 400, 440), ("右墙B", 1150, 395, 430), ("右墙C", 1080, 385, 415),
    ("右墙D", 1000, 375, 405),
    ("天空对照", 120, 300, 330), ("远景区间", 1150, 330, 350),
)


def collect(name: str, lo: int, hi: int):
    rows = R._rows(name)
    hi = min(hi, len(rows))
    t, acc = [], {k[0]: [] for k in PROBES}
    for i in range(lo, hi):
        rgb = R._frame(name, i)
        if rgb is None:
            continue
        g = R._gray(rgb).astype(np.float32)
        t.append(rows[i]["ts_ns"] / 1e9)
        for key, col, r0, r1 in PROBES:
            acc[key].append(float(g[r0:r1, col].mean()))
    t = np.array(t)
    grid = np.arange(t[0], t[-1], DT)
    return grid, {k: np.interp(grid, t, np.array(v)) for k, v in acc.items()}


def spectrum(v):
    """去趋势 → 加窗周期图。返回 (频率, 功率)。"""
    v = v - median_filter(v, size=int(2.0 / DT) | 1)     # 去掉 2 秒以上的缓慢光照变化
    w = np.hanning(len(v))
    F = np.fft.rfft((v - v.mean()) * w, n=NFFT)
    return np.fft.rfftfreq(NFFT, DT), np.abs(F) ** 2


def top_peaks(f, p, k=3):
    band = (f >= F_LO) & (f <= F_HI)
    fb, pb = f[band], p[band]
    floor = float(np.median(pb)) + 1e-12
    idx = np.argsort(pb)[::-1]
    taken, out = [], []
    for j in idx:
        if any(abs(fb[j] - t) < 0.25 for t in taken):
            continue
        taken.append(float(fb[j]))
        out.append((float(fb[j]), float(pb[j]) / floor))
        if len(out) >= k:
            break
    return out, floor


def run(name: str, lo: int, hi: int, truth: float) -> None:
    grid, ser = collect(name, lo, hi)
    print(f"\n===== 过缝率 {name}  帧 {lo}~{hi}  时长 {grid[-1] - grid[0]:.1f}s"
          f"  频率分辨率 {1 / (grid[-1] - grid[0]):.3f}Hz  参考速度 {truth:.0f} m/s")
    print(f"  {'探针':>8} {'峰1 Hz':>7} {'突出比':>7} {'反推间距m':>9}   峰2 Hz(比)      峰3 Hz(比)")
    res = {}
    for key, col, r0, r1 in PROBES:
        f, p = spectrum(ser[key])
        pk, floor = top_peaks(f, p)
        s = truth / pk[0][0] if pk and pk[0][0] > 0 else float("nan")
        res[key] = {"peaks": pk, "spacing_m": s, "col": col, "rows": (r0, r1)}
        print(f"  {key:>8} {pk[0][0]:>7.2f} {pk[0][1]:>7.1f} {s:>9.1f}   "
              + "  ".join(f"{a:.2f}({b:.0f})" for a, b in pk[1:3]))
    wall = [v["spacing_m"] for k, v in res.items() if "墙" in k]
    ctrl = [v["peaks"][0][1] for k, v in res.items() if "墙" not in k]
    wallp = [v["peaks"][0][1] for k, v in res.items() if "墙" in k]
    print(f"  墙面 8 探针反推间距：中位 {np.median(wall):.1f}m  全距[{min(wall):.1f},{max(wall):.1f}]"
          f"  一致系数 {np.std(wall) / np.mean(wall):.0%}")
    print(f"  突出比：墙面中位 {np.median(wallp):.1f}  对照中位 {np.median(ctrl):.1f}"
          f"  → 对照若接近或超过墙面，说明峰不是运动信号")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--lo", type=int, default=60)
    ap.add_argument("--hi", type=int, default=940)
    ap.add_argument("--truth", type=float, default=41.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv[1:])
    run(a.session, a.lo, a.hi, a.truth)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
