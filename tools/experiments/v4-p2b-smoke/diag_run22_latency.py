# -*- coding: utf-8 -*-
"""v4 延迟画像（P2b 收尾·反应慢）：量化单次决策/点击往返的真实耗时。

从 19:32 真机日志解析时间线，统计：
1. policy_loop 相邻 Succeeded 间隔分布（决策帧率——若远大于自身 300ms
   rate_limit，说明被父 dwell 的 600ms 节流或误入其他等待拖慢）；
2. 内有点击的 policy 帧 vs 无点击帧的周期差（等待 wait_after 800ms 是否
   是每帧固定支付）；
3. 相邻点击事件间隔（真实点击节奏）。

判据：延迟构成 = dwell.rate_limit + policy_loop.rate_limit + 点击 wait_after
   + 每次锚点 miss 的识别帧。哪个占比最高先动哪个。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

LOG = Path(os.environ["APPDATA"]) / "MaaRacingAssistant" / "logs/MRA_20260909_193225.log"


def main() -> None:
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    # 时间戳 [19:33:00] → 秒；同一秒内多次 .00/.01 丢失，但同秒视为 0 间隔（上限采样）
    policy_ts: list[float] = []
    click_ts: list[float] = []  # [鉴宝点击] 行（真实点击发生）
    policy_frame_with_click: list[bool] = []

    last_policy = -1.0
    # 逐行扫描：记录 policy_loop Succeeded 时刻 + 随后的 clicking 标记
    pat_time = re.compile(r"\[(\d\d):(\d\d):(\d\d)\]")
    pat_policy_done = re.compile(r"Succeeded 动作: treasure\.policy_loop")
    pat_click = re.compile(r"\[鉴宝点击\]")
    in_policy_frame = False
    frame_has_click = False
    frame_start = -1.0

    for ln in lines:
        m = pat_time.search(ln)
        if not m:
            continue
        h, mi, s = map(int, m.groups())
        t = h * 3600 + mi * 60 + s  # 精确到秒

        if pat_policy_done.search(ln):
            # 前一帧结束：记录周期
            if frame_start >= 0:
                policy_ts.append(t - frame_start)
                policy_frame_with_click.append(frame_has_click)
            in_policy_frame = True
            frame_start = t
            frame_has_click = False
            last_policy = t
        elif "Starting 动作: treasure.policy_loop" in ln:
            pass  # 帧开始
        elif pat_click.search(ln):
            click_ts.append(t)
            if in_policy_frame or frame_start >= 0:
                frame_has_click = True

    # 补最后一段
    if frame_start >= 0:
        policy_ts.append(30 * 60 + 5 - frame_start if False else 3)  # 尾部忽略

    print("=== 日志总览 ===")
    print(f"policy_loop 决策帧数(近似): {len(policy_ts)}")
    print(f"点击事件总数: {len(click_ts)}")

    # 决策帧间隔分布（秒粒度，最小 0 == 同秒多帧）
    if policy_ts:
        import collections
        dist = collections.Counter(policy_ts)
        print("\n=== policy_loop 相邻帧间隔分布（秒）===")
        for k in sorted(dist):
            print(f"  {k:.0f}s × {dist[k]}")
        vals = [t for t in policy_ts]
        import statistics
        print(f"  中位 {statistics.median(vals):.1f}s 均值 {statistics.mean(vals):.1f}s")

    if click_ts:
        diffs = [b - a for a, b in zip(click_ts, click_ts[1:]) if b - a >= 0]
        _diffs = [d for d in diffs if 0 < d <= 10]  # 过滤尾部异常
        print(f"\n=== 相邻点击间隔（秒，{len(_diffs)} 段）===")
        import statistics
        print(f"  中位 {statistics.median(_diffs):.1f}s 均值 {statistics.mean(_diffs):.1f}s")
        print(f"  各间隔: {sorted(set(round(x,1) for x in _diffs))}")

    # 有/无点击帧周期对比
    w_click = [t for t, has in zip(policy_ts, policy_frame_with_click) if has]
    w_no = [t for t, has in zip(policy_ts, policy_frame_with_click) if not has]
    if w_click and w_no:
        import statistics
        print("\n=== 决策帧周期：有点击 vs 无点击 ===")
        print(f"  有点击: n={len(w_click)} 中位 {statistics.median(w_click):.1f}s")
        print(f"  无点击: n={len(w_no)} 中位 {statistics.median(w_no):.1f}s")


if __name__ == "__main__":
    main()