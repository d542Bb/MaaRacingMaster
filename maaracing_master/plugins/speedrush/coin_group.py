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
DEFAULT_GROUP_GRACE = 24      # 组失联退役窗（帧）：≈0.6s@40fps——与场景保鲜窗同量级，
                              # 让单枚币短暂丢检后仍能续上同一条线（b1e6cb9 起值 8 偏短）
DEFAULT_GAP_FACTOR = 1.8      # 组内间距 > factor×组内中位 → partial（§7.2 验证间距均匀性）
DEFAULT_SPATIAL_DX_LANE = 0.35   # 单枚续组：与旧组 lead 的横位容差（道）
DEFAULT_SPATIAL_CY = 140.0       # 单枚续组：与旧组 lead 的纵向容差（px，≈3 车长）


@dataclass(frozen=True)
class AggParams:
    max_dx_lane: float = DEFAULT_MAX_DX_LANE
    max_cy_adj: float = DEFAULT_MAX_CY_ADJ
    jaccard_min: float = DEFAULT_JACCARD_MIN
    group_grace: int = DEFAULT_GROUP_GRACE
    gap_factor: float = DEFAULT_GAP_FACTOR
    # 单枚反复出现（跟踪 id 每帧重建）时靠空间近邻续上旧组——Jaccard=0
    # 会不断新建组，组的横位/到站时间也就跟着最后一枚币漂。
    spatial_dx_lane: float = DEFAULT_SPATIAL_DX_LANE
    spatial_cy: float = DEFAULT_SPATIAL_CY
    lead_hold: int = 24          # lead 币失联后最多保留多少帧（其到站时间按龄衰减）

    def __post_init__(self) -> None:
        if (self.max_dx_lane <= 0 or self.max_cy_adj <= 0
                or not 0.0 < self.jaccard_min <= 1.0 or self.group_grace < 1
                or self.gap_factor <= 1.0 or self.spatial_dx_lane <= 0
                or self.spatial_cy <= 0 or self.lead_hold < 1):
            raise ValueError(f"聚合参数非法：{self}")


@dataclass
class _GState:
    gid: int
    members: frozenset[int]
    first_seen: int
    last_seen: int
    # lead=最近一枚（cy 最大）的观测；组位置与到站时间都锚在它上面。
    # 近枚短暂丢检时保留原 lead（lead_fid 不动），由消费端按龄衰减。
    lead_id: int | None = None
    lead_x: float | None = None
    lead_cy: int | None = None
    lead_conf: float = 0.0
    lead_fid: int = 0
    x_f: float | None = None     # 组横位的时间平滑值（道）


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

        by_id = {t.id: t for t in coins}
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
            base_i: int | None = best[0] if best is not None else \
                self._spatial_match(cur, by_id, matched, fid)
            if base_i is not None:
                matched.add(base_i)
                base = self._groups[base_i]
                st = _GState(base.gid, cur, base.first_seen, fid,
                             base.lead_id, base.lead_x, base.lead_cy,
                             base.lead_conf, base.lead_fid, base.x_f)
            else:
                st = _GState(self._next_gid, cur, fid, fid)
                self._next_gid += 1
            new_states.append(st)
        # 本帧没聚到成员的旧组：宽限窗内继续在册（与跟踪同一纪律），过期退役
        survivors = [g for i, g in enumerate(self._groups)
                     if i not in matched and fid - g.last_seen <= self.p.group_grace]
        self._groups = new_states + survivors

        groups: list[CoinGroup] = []
        for g in self._groups:
            ms = [by_id[i] for i in g.members if i in by_id]
            if not ms:
                # 宽限存续：横位/置信/lead 时间保持最后观测（消费端按龄衰减），
                # 不再发 NaN/0——那会让组在场景里直接消失。
                if g.lead_x is None:
                    continue
                groups.append(CoinGroup(
                    group_id=g.gid, member_ids=tuple(sorted(g.members)),
                    observed_count=len(g.members), estimated_count=len(g.members),
                    x_center=g.x_f if g.x_f is not None else g.lead_x,
                    x_span=0.0, cy_min=0,
                    cy_max=g.lead_cy or 0, conf_min=g.lead_conf,
                    partial_observation=True,
                    first_seen_fid=g.first_seen, last_seen_fid=g.last_seen,
                    validity_until_fid=g.last_seen + self.p.group_grace,
                    lead_fid=g.lead_fid))
                continue
            xs = sorted((m.x_lane for m in ms))
            gaps = [b - a for a, b in zip(xs, xs[1:])]
            med = statistics.median(gaps) if gaps else 0.0
            partial = bool(gaps) and med > 0 and max(gaps) > self.p.gap_factor * med
            obs_lead = max(ms, key=lambda m: m.cy)
            hold = (g.lead_cy is not None and g.lead_x is not None
                    and obs_lead.cy < g.lead_cy - 2
                    and fid - g.lead_fid <= self.p.lead_hold)
            if hold:
                # 近枚这帧没看到、只看到后面的币：保留近枚的位置与时间，
                # 由消费端按 lead_fid 的龄衰减——不被后一枚顶替。
                lead_x, lead_cy = g.lead_x, g.lead_cy
                lead_conf, lead_fid = g.lead_conf, g.lead_fid
                x_f = g.x_f if g.x_f is not None else lead_x
            else:
                lead_x, lead_cy = obs_lead.x_lane, obs_lead.cy
                lead_conf, lead_fid = obs_lead.conf, fid
                target_x = (sum(xs) / len(xs) if len(ms) > 1
                            else obs_lead.x_lane)
                x_f = target_x if g.x_f is None \
                    else g.x_f + 0.5 * (target_x - g.x_f)
                g.lead_id = obs_lead.id
            g.lead_x, g.lead_cy = lead_x, lead_cy
            g.lead_conf, g.lead_fid, g.x_f = lead_conf, lead_fid, x_f
            groups.append(CoinGroup(
                group_id=g.gid, member_ids=tuple(sorted(g.members)),
                observed_count=len(ms), estimated_count=len(ms),
                x_center=x_f,
                x_span=xs[-1] - xs[0] if len(xs) > 1 else 0.0,
                cy_min=min(m.cy for m in ms), cy_max=lead_cy,
                conf_min=lead_conf,
                partial_observation=partial,
                first_seen_fid=g.first_seen, last_seen_fid=g.last_seen,
                validity_until_fid=g.last_seen + self.p.group_grace,
                lead_fid=lead_fid))
        groups.sort(key=lambda c: -c.cy_max)
        return replace(obs, coin_groups=tuple(groups))

    def _spatial_match(self, cur: frozenset[int], by_id: dict,
                       matched: set[int], fid: int) -> int | None:
        """单枚续组：当前簇只有一枚币时，找空间上最接近的未匹配旧组。

        只在“新簇 1 枚”时启用——多枚组的身份仍由 Jaccard 判，避免把
        不同币线并到一起。旧组须在宽限窗内、且有 lead。"""
        if len(cur) != 1:
            return None
        tid = next(iter(cur))
        c = by_id.get(tid)
        if c is None:
            return None
        best: tuple[int, float] | None = None
        for i, g in enumerate(self._groups):
            if i in matched or g.lead_x is None or g.lead_cy is None:
                continue
            if fid - g.last_seen > self.p.group_grace:
                continue
            dx = abs(c.x_lane - g.lead_x)
            dcy = abs(c.cy - g.lead_cy)
            if dx <= self.p.spatial_dx_lane and dcy <= self.p.spatial_cy:
                score = dx + dcy / 1000.0
                if best is None or score < best[1]:
                    best = (i, score)
        return None if best is None else best[0]

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
