# -*- coding: utf-8 -*-
"""N1-a：把「阶段判定搬到观察线程」的成本先量出来，再决定搬不搬。

不注生产代码，直接离线跑真 `TreasureStageDetector.detect()`，帧取真机会话的 raw 原图。

两个口径：
  A 全量扫描（active_rois=None）      —— 最坏上界：全部 detect 锚点 × 多尺度。
  B 稳态感知裁剪（按上一帧判定阶段取 active ∪ 全局锚点）—— 搬迁后的实际负载。

观察线程现状：每 150ms 一次 `screenshot()` → 帧号自增 → 入队；渲染与写盘在 IO worker
线程（不同线程，只竞争 CPU）。所以 detect 是**串行叠加在产帧节律上**的，判据看 p95
相对 150ms 的占比。

用法：
    .venv\\Scripts\\python.exe tools/experiments/observe-stage-cost/bench_detect.py <会话目录> [--step 3]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402

PLUGIN = REPO / "maaracing_master/plugins/treasure"


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def report(tag, ms):
    n = len(ms)
    total = sum(ms)
    print(f"  {tag:<12} n={n:<5} 均 {total / n:6.2f}  p50 {pct(ms, .5):6.2f}  "
          f"p95 {pct(ms, .95):6.2f}  p99 {pct(ms, .99):6.2f}  max {max(ms):7.2f}  ms")


def per_anchor(det, plan, frames, warm=8):
    """逐锚点拆解散成本：定位「贵在哪一个锚点」，供选择性降档。

    前 warm 帧作模板装载/尺度预热不计入；每锚点单独扫（不含短路），得到的是
    独立成本上界——实际扫描命中即 return，真实均值低于各锚点之和。
    """
    print("\n逐锚点独立成本（预热后，帧=全量 template 锚点）：")
    rows = []
    for name in plan.detect_anchors:
        ms = []
        for i, f in enumerate(frames):
            bgr = cv2.imread(str(f))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            t = time.perf_counter()
            det.detect(rgb, {name})
            dt = (time.perf_counter() - t) * 1000
            if i >= warm:
                ms.append(dt)
        if ms:
            sc = len(plan.spec[name].templates)
            rows.append((name, sc, sum(ms) / len(ms), pct(ms, .95)))
    rows.sort(key=lambda r: -r[2])
    print("  %-24s %-6s %8s %8s" % ("锚点", "模板数", "均 ms", "p95 ms"))
    for name, sc, avg, p95 in rows:
        print("  %-24s %-6d %8.2f %8.2f" % (name, sc, avg, p95))
    print(f"  单锚点均值求和 = {sum(r[2] for r in rows):.1f} ms（全扫上界，实际命中短路后更低）")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    session = Path(sys.argv[1])
    step = 3
    for i, a in enumerate(sys.argv):
        if a == "--step":
            step = max(1, int(sys.argv[i + 1]))
    frames = sorted((session / "raw").glob("*_raw.jpg"))[::step]
    if not frames:
        print(f"[FAIL] {session}/raw 无 *_raw.jpg")
        return 2

    det = TreasureStageDetector(PLUGIN)
    plan = det.plan
    if plan is None:
        print("[FAIL] DetectionPlan 未加载")
        return 1
    anchors = list(plan.detect_anchors)
    print(f"会话 {session.name}  抽样 {len(frames)}/{len(sorted((session / 'raw').glob('*_raw.jpg')))} 帧"
          f"（step={step}）  detect 锚点 {len(anchors)} 个 × {len(plan.scales)} 尺度")
    print(f"阶段清单 {len(plan.stage_order)} 页，全局锚点 {list(plan.global_anchors)}")

    if "--per-anchor" in sys.argv:
        per_anchor(det, plan, frames[:24])
        return 0

    full_ms, cut_ms, skip = [], [], 0
    stage = "游戏大厅"          # 起点：与真机启动默认一致
    t0 = time.perf_counter()
    for i, f in enumerate(frames):
        bgr = cv2.imread(str(f))
        if bgr is None:
            skip += 1
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        t = time.perf_counter()
        det.detect(rgb, None)
        dt_full = (time.perf_counter() - t) * 1000

        active = plan.active_for(stage)
        rois = None if active is None else set(active) | set(plan.global_anchors)
        t = time.perf_counter()
        res = det.detect(rgb, rois)
        dt_cut = (time.perf_counter() - t) * 1000
        if res.stage:
            stage = res.stage
        if i >= 8:                      # 模板装载/尺度预热不计入
            full_ms.append(dt_full)
            cut_ms.append(dt_cut)

    print(f"\n读图失败 {skip} 帧  总耗时 {time.perf_counter() - t0:.1f}s")
    print("两种口径的 detect 成本：")
    report("A 全量", full_ms)
    report("B 稳态裁剪", cut_ms)

    budget = 150.0
    p95 = pct(cut_ms, .95)
    print(f"\n对观察线程 150ms 周期的占用（口径 B）：p50 {p95 and pct(cut_ms, .5) / budget * 100:.0f}%"
          f"  p95 {p95 / budget * 100:.0f}%  →  产帧间隔约 "
          f"{budget + pct(cut_ms, .5):.0f}~{budget + p95:.0f}ms")
    verdict = ("p95 在周期 35% 内：可每帧直搬" if p95 <= budget * 0.35 else
               f"p95 占周期 {p95 / budget * 100:.0f}%：按 N 帧降频后搬"
               f"（N≥{p95 / (budget * 0.35):.0f} 时摊薄到 35% 内）"
               if p95 <= budget * 0.8 else
               "p95 已超周期：不能搬进观察线程，另寻档位")
    print(f"判定：{verdict}")
    print("注：绝对 ms 受同期负载抬高（真机同批决策帧含全流程仅 ~112ms），"
          "当相对量级用，不当预算真值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
