# -*- coding: utf-8 -*-
"""粗层选道：绿区给空间，2D 给占用，输出方向与目标车道。

**它是什么**：在走廊（绿区实测边界）上按车道切候选线，用每带的左右边界算
这条线还能走多远；再用场景里的车把被占的车道截断；选连续净空最长的一条。
**它不是什么**：不算精确轨迹、不替代细层的落点与得分优化，也不改路心。

坐标：全部换到**中心线相对帧**再用——车道的 k 值就是「相对当前中心线第 k
道」。绿区带边界是场景绝对 x，先减去该带中心线；自车同理减去 z=0 中心线；
有纵深的车在 build_scene 里已经扣过自身 z 的中心线，无纵深车按 z≈0 近似。
这样弯道与走廊中心偏移都不会把「第几道」算错。

距离口径（只用于粗排序，不当精确距离）：
- 有纵深：车头系纵深（相机纵深 − 基准），直接比较；
- 无纵深但有到站时间：按 `range_speed_mps` 折算一个保守纵深；
- 无纵深、无时间但近（2D 行号近）：按贴脸处理；
- 其余无纵深车：只把该道净空截到观察窗末端，并打 unknown 标记，
  不让「看不见纵深」被当成「车道空」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class _Car(Protocol):
    kind: str
    x_m: float
    z_m: float | None
    t_arrive_s: float | None
    near: bool


@dataclass(frozen=True)
class CoarseParams:
    """粗层参数（真源 decision.json [coarse]）。"""

    lookahead_m: float = 24.0
    max_offset_lane: float = 2.0
    collision_margin_m: float = 0.5
    range_speed_mps: float = 45.0
    switch_margin_m: float = 4.0
    align_tol_lane: float = 0.4
    wall_margin_lane: float = 0.35
    ego_half_lane: float = 0.27
    car_half_lane: float = 0.27
    cam_z_datum_m: float = 1.79


@dataclass(frozen=True)
class CoarseLane:
    """一条候选车道的净空读数（道单位、中心线相对）。"""

    lane: float                  # 相对中心线的车道数（右正）
    free_m: float                # 连续净空（观察窗内，米）
    width_margin_m: float | None  # 全段最小单侧余量（米）；None=绿区无有效带
    blocker: str | None          # 截断来源：space / car / unknown_car / None
    unknown_car: bool = False    # 有车但缺纵深/时间，净空按窗末截断


@dataclass(frozen=True)
class CoarseReport:
    """一拍粗层结果。"""

    ego_lane: float              # 自车相对中心线（道，右正）
    lane: float                  # 选定车道（道）
    direction: int               # -1 左 / 0 保持 / +1 右
    free_m: float
    reason: str                  # space / keep / no_road
    lanes: tuple[CoarseLane, ...]

    def as_trace(self) -> dict:
        return {
            "ego": round(self.ego_lane, 3),
            "lane": round(self.lane, 3),
            "dir": self.direction,
            "free": round(self.free_m, 2),
            "why": self.reason,
            "cand": [[round(c.lane, 3), round(c.free_m, 2),
                      None if c.width_margin_m is None
                      else round(c.width_margin_m, 2),
                      c.blocker, c.unknown_car]
                     for c in self.lanes],
        }


def _rel_center(road, z: float) -> float | None:
    """该纵深处的中心线（场景帧）。"""
    return road.center_at(z)


def _car_distance(c: _Car, p: CoarseParams) -> tuple[float, bool] | None:
    """粗排序用纵深（车头系）。None=窗内不构成占用；bool=缺纵深/时间的近似。"""
    z = c.z_m
    if z is not None:
        zf = z - p.cam_z_datum_m
        if zf <= -2.0 or zf > p.lookahead_m:
            return None
        return max(0.0, zf), False
    t = c.t_arrive_s
    if t is not None and 0.0 <= t * p.range_speed_mps <= p.lookahead_m:
        return t * p.range_speed_mps, False
    if c.near:
        return 0.5, False
    return p.lookahead_m, True


def choose_lane(road, cars: Sequence[_Car], ego_lane: float,
                lane_w_m: float, p: CoarseParams,
                current_lane: float | None = None) -> CoarseReport:
    """在走廊上选连续净空最长的车道。

    ``road``：场景帧分带走廊（带边界为场景绝对 x，带 z 为车头系）。
    ``cars``：场景对象里 kind=='car' 的那些（x_m 为各自 z 的中心线相对帧）。
    ``ego_lane``：自车相对中心线的车道数（右正，与 planner 的 executed 同帧；
    不再从场景绝对 x 反推，避免 center_at 带间插值的偏移）。``current_lane``：
    上一拍选定车道（道），用于换道迟滞；None=以自车最近车道为现任。
    """
    if not road.ok:
        return CoarseReport(ego_lane, ego_lane, 0, 0.0, "no_road", ())
    c0 = road.center_at(0.0)
    if c0 is None:
        return CoarseReport(ego_lane, ego_lane, 0, 0.0, "no_road", ())
    if current_lane is None:
        current_lane = round(ego_lane)

    nmax = int(round(p.max_offset_lane))
    candidates = sorted({float(k) for k in range(-nmax, nmax + 1)})
    ego_half = p.ego_half_lane * lane_w_m
    car_half = p.car_half_lane * lane_w_m
    wall_margin = p.wall_margin_lane * lane_w_m
    envelope = ego_half + car_half + p.collision_margin_m
    band_rows = [b for b in road.bands
                 if b.left is not None and b.right is not None
                 and b.z1 > 0.0 and b.z0 < p.lookahead_m]

    lanes: list[CoarseLane] = []
    for k in candidates:
        x_rel = k * lane_w_m
        free = p.lookahead_m
        width_margin: float | None = None
        blocker = None
        # 1) 绿区：中心线相对后逐带检查这条车道是否仍在路内。
        for b in band_rows:
            zc = 0.5 * (b.z0 + b.z1)
            cen = _rel_center(road, zc)
            if cen is None:
                continue
            l = (b.left - cen) + wall_margin
            r = (b.right - cen) - wall_margin
            if l > r:
                free = min(free, max(0.0, b.z0))
                blocker = "space"
                break
            if x_rel < l or x_rel > r:
                free = min(free, max(0.0, b.z0))
                blocker = "space"
                break
            m = min(x_rel - l, r - x_rel)
            width_margin = m if width_margin is None else min(width_margin, m)
        # 2) 2D/米制占用：只截断该车道，不改变别的车道。
        unknown_car = False
        if blocker != "space" or free > 0.0:
            for c in cars:
                if c.kind != "car":
                    continue
                if c.z_m is not None:
                    cx = c.x_m
                else:
                    cx = c.x_m - c0   # 无纵深的场景 x 近似扣 z=0 中心线
                if abs(cx - x_rel) >= envelope:
                    continue
                dist = _car_distance(c, p)
                if dist is None:
                    continue
                d, approx = dist
                if d <= free:
                    free = d
                    blocker = "unknown_car" if approx else "car"
                    unknown_car = approx
        lanes.append(CoarseLane(k, free, width_margin, blocker, unknown_car))

    if not lanes:
        return CoarseReport(ego_lane, ego_lane, 0, 0.0, "no_road", ())

    def key(c: CoarseLane) -> tuple:
        # 净空优先；净空相同优先没有「缺纵深车」的道；再回中；再少横移；
        # 最后比两侧余量。unknown 车只做同净空排序，不冒充精确距离。
        return (c.free_m, not c.unknown_car, -abs(c.lane),
                c.width_margin_m if c.width_margin_m is not None else -1e9,
                -abs(c.lane - ego_lane))

    best = max(lanes, key=key)
    # 迟滞：现任车道有效且与最优只差一个换道余量时保持，防逐拍翻面。
    # 例外：最优道更靠近中心时不保持——否则在左缘选过一次就永远回不来。
    cur = next((c for c in lanes if abs(c.lane - current_lane) < 1e-6), None)
    closer_to_center = cur is not None and abs(best.lane) < abs(cur.lane) - 1e-9
    better_known = cur is not None and cur.unknown_car and not best.unknown_car
    # 现任道一旦有车/收窄（blocker 非空），迟滞不适用：空道再好也要并。
    cur_clear = cur is not None and cur.blocker is None
    if cur is not None and cur.free_m > 0.0 and cur is not best \
            and cur_clear \
            and not closer_to_center and not better_known \
            and best.free_m - cur.free_m <= p.switch_margin_m:
        chosen = cur
        reason = "keep"
    else:
        chosen = best
        reason = "space"
    delta = chosen.lane - ego_lane
    direction = 0 if abs(delta) <= p.align_tol_lane else (1 if delta > 0 else -1)
    return CoarseReport(ego_lane, chosen.lane, direction, chosen.free_m,
                        reason, tuple(lanes))
