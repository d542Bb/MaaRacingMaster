# -*- coding: utf-8 -*-
"""60 m 内可行动元素的锚（2026-11-01，第 4 期观测面）。

锚 = 当前最该盯的机会元素（币组/奖励车）的位置；给分级时域与预置一个
跨帧稳定的参考点。本阶段只落 trace，不接选道/视界。

规则（方案 B）：
- 候选：到站时间在 (0, 6] s、横向在车侧 2.5 道内，按到站时间最近优先；
- 已持有的锚在 1 道迟滞内直接续用（只刷新时间，不换身份）；
- 换锚需连续 3 拍同 id 确认，且距上次换锚满 1 s；
- 无候选立即释放（回退到路线/中心，由消费端决定）。
"""
from __future__ import annotations

from dataclasses import dataclass

CONFIRM_TICKS = 3
HYSTERESIS_LANE = 1.0
HOLD_S = 1.0
MAX_T_S = 6.0
MAX_OFF_LANE = 2.5


@dataclass(frozen=True)
class Candidate:
    kind: str
    id: int
    x_lane: float
    t_s: float | None
    conf: float


@dataclass(frozen=True)
class Anchor:
    kind: str
    id: int
    x_lane: float
    t_s: float | None
    conf: float
    held_s: float

    def as_trace(self) -> dict:
        return {"k": self.kind, "id": self.id,
                "x": round(self.x_lane, 3),
                "t": None if self.t_s is None else round(self.t_s, 2),
                "c": round(self.conf, 2), "held": round(self.held_s, 2)}


class AnchorTracker:
    def __init__(self, *, confirm_ticks: int = CONFIRM_TICKS,
                 hysteresis_lane: float = HYSTERESIS_LANE,
                 hold_s: float = HOLD_S) -> None:
        self.confirm_ticks = max(1, int(confirm_ticks))
        self.hysteresis_lane = hysteresis_lane
        self.hold_s = hold_s
        self.reset()

    def reset(self) -> None:
        self._anchor: Anchor | None = None
        self._pending: Candidate | None = None
        self._pending_n = 0
        self._held_s = 0.0
        self._why = "none"

    def step(self, candidates, dt_s: float) -> Anchor | None:
        cands = [c for c in candidates
                 if c.t_s is not None and 0.0 < c.t_s <= MAX_T_S
                 and abs(c.x_lane) <= MAX_OFF_LANE]
        cands.sort(key=lambda c: (c.t_s, -c.conf))
        self._held_s += max(0.0, dt_s)
        if not cands:
            self._anchor = None
            self._pending, self._pending_n = None, 0
            self._why = "none"
            return None
        best = cands[0]
        if self._anchor is not None:
            same = self._same(self._anchor, best)
            near = abs(best.x_lane - self._anchor.x_lane) <= \
                self.hysteresis_lane
            if same or near:
                cur = self._anchor
                self._anchor = Anchor(cur.kind, cur.id, cur.x_lane,
                                      best.t_s, best.conf, self._held_s)
                self._pending, self._pending_n = None, 0
                self._why = "keep"
                return self._anchor
        # 确认计数照常累计；最短持有只拦「切换」，不拦确认
        if self._pending is not None and self._same(self._pending, best):
            self._pending_n += 1
            self._pending = best
        else:
            self._pending, self._pending_n = best, 1
        if self._anchor is not None and self._held_s < self.hold_s:
            self._why = "hold"
            return self._anchor
        if self._pending_n >= self.confirm_ticks or self._anchor is None:
            self._anchor = Anchor(best.kind, best.id, best.x_lane, best.t_s,
                                  best.conf, 0.0)
            self._held_s = 0.0
            self._pending, self._pending_n = None, 0
            self._why = "switch"
        return self._anchor

    @staticmethod
    def _same(a: Candidate | Anchor, b: Candidate) -> bool:
        return a.kind == b.kind and a.id == b.id and a.id >= 0

    @property
    def why(self) -> str:
        return self._why
