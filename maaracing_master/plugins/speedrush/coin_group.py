# -*- coding: utf-8 -*-
"""金币组聚合器（设计稿 v2 §三；§八 step 3）。

**为什么是独立组件而不是评分器里的临时聚类**（审查 §二.3 裁定）：组聚合有跨帧状态
（组身份、存续窗），评分器是无状态纯函数——混写会让"迟滞"与"聚类"两件事互相污染，
且 §七.2 回放校准需要单独换参数重跑。

**聚合口径**：
- 只聚近场金币（`targets` 中 kind=coin；far 金币没有横向读数，组归并不了——
  进域后经跟踪 id 晋升自动入组，身份通道见 §一契约与回放实证 commit `b1e6cb9`）；
- 同帧：按 x_lane 升序链接相邻对（|Δx_lane| ≤ max_dx 且 |Δcy| ≤ max_cy 才相连），
  连通分量即组——链式传递会把紧邻两组并成一组的风险由 §七.2 的 Δx 双峰分离度背书，
  分离度不足再换真聚类；
- 跨帧：与上一帧各组的成员 id 集合 Jaccard ≥ jaccard_min 者继承其 group_id，
  否则新建；组失联超 grace 退役（与目标同一纪律，id 不复用）；
- 收益纪律：**observed 计数、估计=观测**（§三"宁可低估，不虚增"）；
  `partial_observation` 标记组内异常大间距（疑似漏检），供回放阶段核算
  "漏检折算"该不该上（RULES §7#3 组结构真值出来后定公式）。

阈值经 §七.2 回放测量（probe_coin_groups，42 场全素材）校准一轮：
max_dx=0.3 确认（组内邻距 0.1–0.3 车道、组间 ≈5.0，分离干净）；
max_cy 由起值 40 改 60（同组行距 P90=37）；jaccard 0.5 落在干净双峰的谷中（97%≥0.8、
<0.5 无样本）确认。gap_factor=1.8 样本太少（域内三枚组仅 25 例、间距完美均匀）——
维持设计值等 step 4。step 4 后参数进 decision.json。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import KIND_COIN

DEFAULT_MAX_DX_LANE = 0.3     # §7.2 实测确认：组内邻距 0.1–0.3 / 组间 ≈5.0 车道，谷清晰
DEFAULT_MAX_CY_ADJ = 60.0     # §7.2 实测定档：同组行距 P90=37，起值 40 切尾改 60
DEFAULT_JACCARD_MIN = 0.5     # §三设计起值
DEFAULT_GROUP_GRACE = 8       # 组失联退役窗（行）：与 grace 定档同源（≈0.5s，b1e6cb9）
DEFAULT_GAP_FACTOR = 1.8      # 组内间距 > factor×组内中位 → partial（§7.2 验证间距均匀性）


@dataclass(frozen=True)
class AggParams:
    max_dx_lane: float = DEFAULT_MAX_DX_LANE
    max_cy_adj: float = DEFAULT_MAX_CY_ADJ
    jaccard_min: float = DEFAULT_JACCARD_MIN
    group_grace: int = DEFAULT_GROUP_GRACE
    gap_factor: float = DEFAULT_GAP_FACTOR

    def __post_init__(self) -> None:
        if (self.max_dx_lane <= 0 or self.max_cy_adj <= 0
                or not 0.0 < self.jaccard_min <= 1.0 or self.group_grace < 1
                or self.gap_factor <= 1.0):
            raise ValueError(f"聚合参数非法：{self}")


@dataclass
class _GState:
    gid: int
    members: frozenset[int]
    first_seen: int
    last_seen: int


class CoinGroupAggregator:
    """逐 tick 喂 `Tracker.update()` 的产出，回填 `coin_groups` 字段后返回。
    `frame_id` 必须随观测流单调（信任上游 Tracker 已 fail-loud，不重复校验）。"""

    def __init__(self, params: AggParams = AggParams()):
        self.p = params
        self._groups: list[_GState] = []
        self._next_gid = 1

    def reset(self) -> None:
        """清空组状态（阶段切换/保守态出口用；gid 计数器不回卷）。"""
        self._groups.clear()

    def update(self, obs: WorldObservation) -> WorldObservation:
        fid = obs.frame_id
        coins = [t for t in obs.targets if t.kind == KIND_COIN]
        clusters = self._cluster(coins)

        new_states: list[_GState] = []
        matched: set[int] = set()
        for member_ids in clusters:
            cur = frozenset(member_ids)
            best: tuple[int, float] | None = None
            for i, g in enumerate(self._groups):
                if i in matched or not cur | g.members:
                    continue
                jac = len(cur & g.members) / len(cur | g.members)
                if jac >= self.p.jaccard_min and (best is None or jac > best[1]):
                    best = (i, jac)
            if best is not None:
                i = best[0]
                matched.add(i)
                st = _GState(self._groups[i].gid, cur,
                             self._groups[i].first_seen, fid)
            else:
                st = _GState(self._next_gid, cur, fid, fid)
                self._next_gid += 1
            new_states.append(st)
        # 本帧没聚到成员的旧组：宽限窗内继续在册（与跟踪同一纪律），过期退役
        survivors = [g for i, g in enumerate(self._groups)
                     if i not in matched and fid - g.last_seen <= self.p.group_grace]
        self._groups = new_states + survivors

        by_id = {t.id: t for t in coins}
        groups: list[CoinGroup] = []
        for g in self._groups:
            ms = [by_id[i] for i in g.members if i in by_id]
            if not ms:
                groups.append(CoinGroup(  # 宽限存续：成员坐标保持最后观测（同跟踪纪律）
                    group_id=g.gid, member_ids=tuple(sorted(g.members)),
                    observed_count=len(g.members), estimated_count=len(g.members),
                    x_center=float("nan"), x_span=float("nan"),
                    cy_min=0, cy_max=0, conf_min=0.0,
                    partial_observation=True,
                    first_seen_fid=g.first_seen, last_seen_fid=g.last_seen,
                    validity_until_fid=g.last_seen + self.p.group_grace))
                continue
            xs = sorted((m.x_lane for m in ms))
            gaps = [b - a for a, b in zip(xs, xs[1:])]
            med = statistics.median(gaps) if gaps else 0.0
            partial = bool(gaps) and med > 0 and max(gaps) > self.p.gap_factor * med
            groups.append(CoinGroup(
                group_id=g.gid, member_ids=tuple(sorted(g.members)),
                observed_count=len(ms), estimated_count=len(ms),
                x_center=sum(xs) / len(xs),
                x_span=xs[-1] - xs[0] if len(xs) > 1 else 0.0,
                cy_min=min(m.cy for m in ms), cy_max=max(m.cy for m in ms),
                conf_min=min(m.conf for m in ms),
                partial_observation=partial,
                first_seen_fid=g.first_seen, last_seen_fid=g.last_seen,
                validity_until_fid=g.last_seen + self.p.group_grace))
        groups.sort(key=lambda c: -c.cy_max)
        return replace(obs, coin_groups=tuple(groups))

    def _cluster(self, coins: list[TrackedTarget]) -> list[list[int]]:
        """x_lane 升序链接相邻对，连通分量为组。"""
        if not coins:
            return []
        srt = sorted(coins, key=lambda t: t.x_lane)
        clusters: list[list[int]] = [[srt[0].id]]
        for prev, cur in zip(srt, srt[1:]):
            if (cur.x_lane - prev.x_lane <= self.p.max_dx_lane
                    and abs(cur.cy - prev.cy) <= self.p.max_cy_adj):
                clusters[-1].append(cur.id)
            else:
                clusters.append([cur.id])
        return clusters
