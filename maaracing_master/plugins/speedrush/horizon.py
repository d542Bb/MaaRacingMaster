# -*- coding: utf-8 -*-
"""拟议车道视界（2026-11-01，第 4 期影子）。

按开度给目标视界：开阔 3.0 s、正常 2.5 s、拥挤 2.0 s；0.5 s 驻留、
跨阈至少 0.5 s、单次最多变 1 s。**只落 trace，不接管选择层**——选择层
仍是固定 `selection.horizon_s`。等实车两局对照「影子视界 vs 固定视界」的
重算次数与杆值后，再决定是否接控制。
"""
from __future__ import annotations

from dataclasses import dataclass

LO_S = 2.0
HI_S = 3.0
DWELL_S = 0.5
MIN_STEP_S = 0.5
MAX_STEP_S = 1.0
_TARGET = {"open": HI_S, "normal": 2.5, "crowded": LO_S}


@dataclass(frozen=True)
class HorizonState:
    current_s: float
    target_s: float | None
    why: str                      # init / hold / switch

    def as_trace(self) -> dict:
        return {"h": round(self.current_s, 2),
                "tgt": None if self.target_s is None
                else round(self.target_s, 2),
                "why": self.why}


class HorizonGovernor:
    def __init__(self, *, lo: float = LO_S, hi: float = HI_S,
                 dwell_s: float = DWELL_S, max_step_s: float = MAX_STEP_S,
                 initial_s: float = HI_S) -> None:
        self.lo, self.hi = lo, hi
        self.dwell_s = dwell_s
        self.max_step_s = max_step_s
        self._initial = initial_s
        self.reset()

    def reset(self) -> None:
        self.current = min(max(self._initial, self.lo), self.hi)
        self.target: float | None = None
        self._dwell = 0.0

    def step(self, level: str, dt_s: float) -> HorizonState:
        tgt = _TARGET.get(level)
        if tgt is None:
            return HorizonState(self.current, None, "hold")
        tgt = min(max(tgt, self.lo), self.hi)
        if self.target is None or abs(tgt - self.target) > 1e-9:
            self.target = tgt
            self._dwell = 0.0
        else:
            self._dwell += max(0.0, dt_s)
        self.target = tgt
        if abs(tgt - self.current) < MIN_STEP_S:
            return HorizonState(self.current, tgt, "hold")
        if self._dwell < self.dwell_s:
            return HorizonState(self.current, tgt, "hold")
        delta = max(-self.max_step_s, min(self.max_step_s, tgt - self.current))
        self.current = min(max(self.current + delta, self.lo), self.hi)
        self._dwell = 0.0
        return HorizonState(self.current, tgt, "switch")
