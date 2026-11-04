# -*- coding: utf-8 -*-
"""重算触发事件（2026-11-01，第 4 期影子）。

记录「这一拍为什么值得重选」：开度跨档、锚变化、选择层变化、地图支撑
变化、路线相位/参考道变化。当前只落 trace，选择器仍逐拍重算；等事件触发
接管时用它做迟滞与取证。

不记录逐拍目标漂移——那是本方案要压掉的噪声，不是触发理由。
"""
from __future__ import annotations

TRIG_OPEN = "open"
TRIG_ANCHOR = "anchor"
TRIG_LAYER = "layer"
TRIG_MAP = "map"
TRIG_ROUTE = "route"
MAP_STEP_M = 5.0


class TriggerLog:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._open = None
        self._anchor = None
        self._layer = None
        self._route = None
        self._map = None

    def step(self, *, open_level: str | None, anchor: dict | None,
             layer: int | None, map_support: float | None,
             route_phase: str | None, route_ref: float | None) -> list[str]:
        out: list[str] = []
        if open_level is not None and self._open is not None \
                and open_level != self._open:
            out.append(TRIG_OPEN)
        if open_level is not None:
            self._open = open_level
        aid = None if anchor is None else (anchor.get("k"), anchor.get("id"))
        if aid != self._anchor and self._anchor is not None:
            out.append(TRIG_ANCHOR)
        self._anchor = aid
        if layer is not None and self._layer is not None and layer != self._layer:
            out.append(TRIG_LAYER)
        if layer is not None:
            self._layer = layer
        route = (route_phase, None if route_ref is None else round(route_ref, 1))
        if route != self._route and self._route is not None:
            out.append(TRIG_ROUTE)
        self._route = route
        if map_support is not None:
            if self._map is not None \
                    and abs(map_support - self._map) >= MAP_STEP_M:
                out.append(TRIG_MAP)
            self._map = map_support
        return out
