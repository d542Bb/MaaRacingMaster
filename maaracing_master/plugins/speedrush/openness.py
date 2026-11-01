# -*- coding: utf-8 -*-
"""开度/拥挤度量（2026-11-01，第 4 期观测面）。

回答「这一拍路面有多开」：近场走廊宽、60 m 内安全车数、最小到站时间。
当前只落 trace、不接视界；分级时域（车道视界 2~3 s）等实车标定后再消费。

口径（方案 B）：
- 开阔 = 走廊宽 ≥ 2.6 道且无 TTC < 4 s；
- 拥挤 = 走廊宽 ≤ 2.0 道或 ≥ 2 辆 TTC < 2 s；
- 其余正常；两个证据都缺时 `unknown`。
"""
from __future__ import annotations

from dataclasses import dataclass

OPEN_WIDTH_LANE = 2.6
CROWD_WIDTH_LANE = 2.0
OPEN_TTC_S = 4.0
FAST_TTC_S = 2.0
NEAR_M = 60.0


@dataclass(frozen=True)
class Openness:
    width_lane: float | None
    cars_near: int
    cars_fast: int
    min_ttc_s: float | None
    level: str                    # open / normal / crowded / unknown

    def as_trace(self) -> dict:
        return {"w": None if self.width_lane is None
                else round(self.width_lane, 2),
                "n": self.cars_near, "fast": self.cars_fast,
                "ttc": None if self.min_ttc_s is None
                else round(self.min_ttc_s, 2),
                "lvl": self.level}


def assess(scene, safety_cars=()) -> Openness:
    """`safety_cars`：对象表的安全车（已排除超龄/退役）。"""
    lane_w = scene.lane_w_m if scene is not None else None
    width = (scene.road.width_m / lane_w
             if scene is not None and lane_w and scene.road.width_m is not None
             else None)
    near = fast = 0
    ttc = None
    for c in safety_cars:
        z = getattr(c, "z_m", None)
        t = getattr(c, "t_arrive_s", None)
        is_near = (z is not None and 0.0 <= z <= NEAR_M) \
            or (z is None and t is not None and t <= OPEN_TTC_S)
        if is_near:
            near += 1
        if t is not None and t > 0.0:
            if t < FAST_TTC_S:
                fast += 1
            ttc = t if ttc is None else min(ttc, t)
    if width is None and ttc is None:
        level = "unknown"
    elif (width is not None and width <= CROWD_WIDTH_LANE) or fast >= 2:
        level = "crowded"
    elif (width is not None and width >= OPEN_WIDTH_LANE) \
            and (ttc is None or ttc >= OPEN_TTC_S):
        level = "open"
    else:
        level = "normal"
    return Openness(width, near, fast, ttc, level)
