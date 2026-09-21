"""金币组聚合口径的离线测量（设计稿 v2 §七.2）：真实 coin 框流喂 Tracker+Aggregator，
出四组校准数据回答三个问题——

①**0.3 组阈值站得住吗**：帧内相邻金币的 Δx_lane 分布是否双峰（组内间隙 vs 组间间隙），
   阈值放双峰谷处才有分离度；单峰贴阈值 = 聚类会随机拆并。
②**cy 邻接阈值定多少**：同组金币的行距分布（透视下组内行差应远小于组间）。
③**跨帧组身份稳不稳**：相邻帧组间 Jaccard 分布（0.5 继承阈值是拍脑袋起值）；
   组寿命分布（宽限 8 行是否够）。

**输入**：`tracking_scan.jsonl`（probe_tracking_replay.py scan 的产物，含三类框几何）。
**注意**：组内金币的跟踪 id 来自真 Tracker（含遮挡保持），与实机链路同构；
x_lane 只在域内行（denom≥MIN_DENOM）有读数——远处金币天然聚不了组（§三 预告机制
不在本测口径），本测只回答"能聚的时候聚得对不对"。

用法（仓库根，需先跑过 tracking 扫描）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_coin_groups.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.coin_group import (  # noqa: E402
    AggParams, CoinGroupAggregator)
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    Detection, PerceptionResult)
from maaracing_master.plugins.speedrush.tracking import Tracker  # noqa: E402

_DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
SCAN = _DATA / "tracking_scan.jsonl"

MAX_DX = 0.3      # 设计稿 §三起值（本测验证对象）
MAX_CY = 40.0     # Aggregator 默认（本测要定档的量）


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    srt = sorted(xs)
    return srt[min(len(srt) - 1, int(round(q * (len(srt) - 1))))]


def main() -> None:
    rows_by_sess: dict[str, list[dict]] = defaultdict(list)
    with open(SCAN, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                if r["boxes"]["coin"]:
                    rows_by_sess[r["sess"]].append(r)
    n_sess = len(rows_by_sess)
    print(f"有币场次 {n_sess}（coin 框非空的行）\n")

    # ① 帧内相邻金币 Δx_lane：先按 x 排序，记录所有相邻对（不按阈值过滤——
    #    双峰形态要在无偏样本上看；cy 相近的"候选同组对"单独统计）
    adj_dx_all: list[float] = []
    adj_dx_near_cy: list[float] = []   # |Δcy|≤60（宽松邻排）
    adj_dy_same_group: list[float] = []  # 阈值下同组的行距
    gap_ratio: list[float] = []         # 组内 max_gap/median_gap（gap_factor 判据实测）
    group_sizes: list[int] = []
    jaccars: list[float] = []
    partial_rate = [0, 0]

    for sess, rows in sorted(rows_by_sess.items()):
        trk = Tracker()
        agg = CoinGroupAggregator(params=AggParams(max_dx_lane=MAX_DX,
                                                   max_cy_adj=MAX_CY))
        prev_members: dict[int, frozenset[int]] = {}
        prev = 0
        for row in rows:
            fid = row["frame_id"]
            if fid <= prev:
                continue
            prev = fid
            per = PerceptionResult(
                frame_id=fid, ts_ns=row["ts_ns"],
                cars=[Detection(*b) for b in row["boxes"]["car"]],
                coins=[Detection(*b) for b in row["boxes"]["coin"]],
                bonuses=[Detection(*b) for b in row["boxes"]["bonus"]])
            obs = agg.update(trk.update(per, frame_age_ms=row["age_ms"], stage=1))
            coins = sorted((t for t in obs.targets if t.kind == "coin"),
                           key=lambda t: t.x_lane)
            for a, b in zip(coins, coins[1:]):
                dx = b.x_lane - a.x_lane
                dy = abs(b.cy - a.cy)
                adj_dx_all.append(dx)
                if dy <= 60:
                    adj_dx_near_cy.append(dx)
            for g in obs.coin_groups:
                group_sizes.append(g.observed_count)
                partial_rate[1] += 1
                partial_rate[0] += int(g.partial_observation)
                xs = sorted(t.x_lane for t in coins if t.id in g.member_ids)
                gaps = [y - x for x, y in zip(xs, xs[1:])]
                adj_dy_same_group += [
                    abs(b.cy - a.cy) for a, b in zip(coins, coins[1:])
                    if a.id in g.member_ids and b.id in g.member_ids]
                if len(gaps) >= 2:
                    med = sorted(gaps)[len(gaps) // 2]
                    if med > 0:
                        gap_ratio.append(max(gaps) / med)
                cur = {g.group_id: frozenset(g.member_ids)}
                for gid, ms in cur.items():
                    if gid in prev_members and (ms | prev_members[gid]):
                        jaccars.append(len(ms & prev_members[gid])
                                       / len(ms | prev_members[gid]))
                prev_members = {**prev_members, **cur}

    def hist(xs, edges, label):
        print(f"\n{label}（n={len(xs)}）")
        if not xs:
            print("  （无样本）")
            return
        import bisect
        srt = sorted(xs)
        edges = sorted(edges)
        counts = [0] * (len(edges) + 1)
        for x in xs:
            counts[bisect.bisect_right(edges, x)] += 1
        for i, c in enumerate(counts):
            lo = "-inf" if i == 0 else f"{edges[i - 1]:g}"
            hi = "inf" if i == len(edges) else f"{edges[i]:g}"
            print(f"  [{lo}, {hi}): {c:6d} ({c / len(xs):5.1%})")
        print(f"  P50={_pct(srt, 0.5):.3f} P90={_pct(srt, 0.9):.3f}")

    hist(adj_dx_all, [0.1, 0.2, 0.3, 0.4, 0.6, 1.0], "① 相邻金币 Δx_lane（全部邻对）")
    hist(adj_dx_near_cy, [0.1, 0.2, 0.3, 0.4, 0.6, 1.0], "①b 相邻且 |Δcy|≤60 的 Δx_lane")
    hist(adj_dy_same_group, [5, 10, 20, 40, 80], "② 同组金币 |Δcy| 行距")
    hist(gap_ratio, [1.2, 1.5, 1.8, 2.5], "③ 组内 max_gap/median_gap（gap_factor=1.8 判据）")
    hist(jaccars, [0.2, 0.35, 0.5, 0.65, 0.8], "④ 相邻帧组 Jaccard（继承阈值 0.5 判据）")
    hist([float(n) for n in group_sizes], [1, 2, 3, 4], "⑤ 组观测枚数分布（RULES §7#3 参照）")
    pr = partial_rate[0] / partial_rate[1] if partial_rate[1] else 0
    print(f"\n⑥ partial_observation 占比：{partial_rate[0]}/{partial_rate[1]} = {pr:.1%}"
          f"（组样本 n={len(group_sizes)}）")


if __name__ == "__main__":
    main()
