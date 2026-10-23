# -*- coding: utf-8 -*-
"""存在性占用影子（观测面，2026-10-22）。

把“距离是否可信”和“车道是否被占”分开：距离失效只表示不发布 ``m``，不表示
车道空闲。占用成立要同时有近场证人和可归的横向位置；释放只认独立证据。

本模块**只读**：DecisionEngine 每拍调用，结果进 trace 的 ``occ`` 列；不改变
选择层/规划层的输出。判据（与离线影子探针同一口径）：

- 建占：可信近距 <8m / 框高 ≥80px / 框底接地行 0.25s 内下移 ≥20px；
  横向 ≤1.7m；横向未知只在落地行 ≥500px（约 8m）保守建占。
- 释放：pass 事件 / 横向连续 2 次新鲜观测 >2.0m / 最后新鲜 2D 命中后
  >500ms 的有界丢失。``z<0`` 不作掠过证据（与距离锚同源）。

生产侧只拿得到“已发布的 m”，没有实验探针里的质量门分级；``m`` 非空且
<8m 还须框底 ≥400px（约 18m 内）才作近场证人，防止远处小框的旧小 m 单独
建占。因此它仍只是影子，不直接控制。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

NEAR_M = 8.0
NEAR_METRIC_Y2 = 400.0   # m<8 的可信近距证人还须框底在此行以下（≈18m 内）；
                         # 生产侧没有实验里的框/平面质量门，防止远处小框的
                         # 旧小 m 单独建占（框底 330~350px 的远框直接挡掉）
NEAR_H = 80.0
NEAR_Y2 = 500.0
APPROACH_Y2 = 20.0
WIN_S = 0.25
LANE_OUT = 1.7
LANE_RELEASE = 2.0
DEPART_TICKS = 2
LOST_MS = 500.0


@dataclass(frozen=True)
class OccObs:
    """本拍一辆车对占用层的观测（全部来自已发布字段）。"""

    tid: int
    h: float | None
    cy: float | None
    x: float | None          # 车道坐标（道，右正）；None=横向不可归
    m: float | None          # 已发布米制距离；None=本拍无有效距离
    fresh: bool              # 本拍是否有新鲜 2D 命中（seen==0）


@dataclass
class _Slot:
    occ: bool = False
    since: float | None = None
    last_fresh: float | None = None
    last_lat: float | None = None
    depart_n: int = 0
    mode: str | None = None
    m: float | None = None


class PresenceOccupancy:
    """逐目标占用状态；``update`` 返回本拍被占用的目标快照。"""

    def __init__(self, lane_w_m: float) -> None:
        self.lane_w = float(lane_w_m)
        self.slots: dict[int, _Slot] = {}
        self.hist: dict[int, list[tuple[float, float | None]]] = {}

    def reset(self) -> None:
        self.slots.clear()
        self.hist.clear()

    @staticmethod
    def _release(s: _Slot) -> None:
        s.occ = False
        s.since = None
        s.depart_n = 0
        s.mode = None
        s.m = None

    def update(self, ts_s: float, obs: Sequence[OccObs],
               ego_lane: float | None,
               passed_ids: set[int]) -> dict | None:
        """推进一拍；返回 ``None`` 或占用快照（blocked/ids/m/modes/since_ms）。"""
        ts = ts_s
        for o in obs:
            s = self.slots.setdefault(o.tid, _Slot())
            y2 = (None if o.h is None or o.cy is None
                  else float(o.cy) + float(o.h) / 2.0)
            if o.fresh:
                s.last_fresh = ts
            lat = (None if o.x is None or ego_lane is None
                   else abs(o.x - ego_lane) * self.lane_w)
            if lat is not None:
                s.last_lat = lat
            if o.h is not None and y2 is not None:
                hh = self.hist.setdefault(o.tid, [])
                hh.append((ts, y2))
                self.hist[o.tid] = [e for e in hh if ts - e[0] <= 0.5]
            near_metric = (o.m is not None and 0.0 < o.m < NEAR_M
                           and y2 is not None and y2 >= NEAR_METRIC_Y2)
            near2d = ((o.h is not None and o.h >= NEAR_H)
                      or (y2 is not None and y2 >= NEAR_Y2))
            appr = False
            hh = self.hist.get(o.tid) or []
            if len(hh) >= 2:
                y0, y1 = hh[0][1], hh[-1][1]
                appr = (y0 is not None and y1 is not None
                        and y1 - y0 >= APPROACH_Y2)
            threat = near_metric or near2d or appr
            if threat:
                s.mode = ("metric" if near_metric
                          else "2d" if near2d else "approach")
            s.m = o.m
            if s.occ:
                if o.tid in passed_ids:
                    self._release(s)
                    continue
                if lat is not None and lat > LANE_RELEASE:
                    if o.fresh:
                        s.depart_n += 1
                    if s.depart_n >= DEPART_TICKS:
                        self._release(s)
                        continue
                else:
                    s.depart_n = 0
            if not s.occ:
                lat_ok = ((lat is not None and lat <= LANE_OUT)
                          or (lat is None and y2 is not None
                              and y2 >= NEAR_Y2))
                if threat and lat_ok:
                    s.occ = True
                    s.since = ts
                    s.depart_n = 0
        # 掠过事件可能针对本拍已不在 obs 里的轨道：只释放该目标自己的槽位
        for tid in passed_ids:
            s = self.slots.get(tid)
            if s is not None and s.occ:
                self._release(s)
        # 没有新鲜 2D 命中后的有界丢失释放（不自动判空以外的东西）
        for s in self.slots.values():
            if not s.occ:
                continue
            ref = s.last_fresh if s.last_fresh is not None else s.since
            if ref is not None and (ts - ref) * 1000.0 > LOST_MS:
                self._release(s)
        ids = sorted(tid for tid, s in self.slots.items() if s.occ)
        if not ids:
            return None
        since_vals = [self.slots[tid].since for tid in ids
                      if self.slots[tid].since is not None]
        since = min(since_vals) if since_vals else ts
        return {
            "blocked": True,
            "ids": ids,
            "m": {tid: (None if self.slots[tid].m is None
                        else round(self.slots[tid].m, 2)) for tid in ids},
            "modes": {tid: self.slots[tid].mode for tid in ids},
            "since_ms": int((ts - since) * 1000.0),
        }
