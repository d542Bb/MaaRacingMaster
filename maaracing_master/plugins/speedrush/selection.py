# -*- coding: utf-8 -*-
"""四层字典序选择（2026-10-18 走廊重建）。

**它是什么**：把「这一拍该跟谁」写成四条硬规则，按顺序取第一个可行的：

    1 奖励车（撞上去） > 2 普通车（擦边通过） > 3 金币（落币线） >
    4 走廊中心带（带内不修正；无机会时由粗层给出目标道）

层与层之间不求和、不比较分数、没有平手阈；层内才比较（金币取价值最高、
车取最早到站）。主目标之后最多插 3 个 3 秒内的中间路点，把顺路的金币和
必须错开的车带上；中间点不可行就丢，不影响主目标。

**它不是什么**：不碰感知、不做轨迹候选集、不做栅格否决、不做车道号。
所有横位都是连续米制；车道宽只做尺子。

**坐标**：选择层坐标 = 走廊中心在自车站为 0、右正。调用方负责把走廊带、
目标、自车位置换算进该坐标（origin 一次平移）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RoadBand:
    """一条走廊带的左右缘（选择层坐标，米）。None=该侧无证据。

    ``src/age_ms/conf`` 是场景快照的置信面（默认值保持旧构造可用）。"""

    z0: float
    z1: float
    left: float | None
    right: float | None
    src: str = "unknown"
    age_ms: float | None = None
    conf: float = 0.0


@dataclass(frozen=True)
class RoadFrame:
    """走廊选择帧：分带中心线 + 自车站处的左右缘。"""

    bands: tuple[RoadBand, ...]
    width_m: float | None
    left_m: float | None
    right_m: float | None
    ok: bool
    coarse_bands: tuple[RoadBand, ...] = ()   # 中远场粗带 15m/带（16~80m，
    # src=inferred；**不进 bands**——车道级消费面只读 bands，粗带是方向级
    # 约束（哪边更开），显式消费（coarse.far_direction 开关））
    far_hint_lane: float | None = None   # 远场方向（走廊层共享滤波结果，
    # 道，右正）：消费端只读，不再各自重算/去抖

    def _pairs(self) -> list[tuple[float, float]]:
        return [(0.5 * (b.z0 + b.z1), 0.5 * (b.left + b.right))
                for b in self.bands
                if b.left is not None and b.right is not None]

    def center_at(self, z: float) -> float | None:
        """中心线在 z 处（线性插值，端点外取端点）。"""
        pts = self._pairs()
        if not pts:
            return None
        if z <= pts[0][0]:
            return pts[0][1]
        for (z0, c0), (z1, c1) in zip(pts, pts[1:]):
            if z <= z1:
                w = 0.0 if z1 <= z0 else (z - z0) / (z1 - z0)
                return c0 + (c1 - c0) * w
        return pts[-1][1]

    def clamp(self, x: float, margin_m: float) -> float:
        if self.left_m is None or self.right_m is None:
            return x
        lo = self.left_m + margin_m
        hi = self.right_m - margin_m
        if hi < lo:
            return 0.5 * (self.left_m + self.right_m)
        return min(max(x, lo), hi)


@dataclass(frozen=True)
class Obj:
    """一个机会/障碍（选择层坐标，米/秒）。"""

    kind: str                     # reward / car / coin
    x: float
    t: float | None               # 到站秒；None=不接近（不参与时序判定）
    count: float = 1.0            # 金币枚数（币层排序用）
    conf: float = 1.0
    oid: int = -1


@dataclass(frozen=True)
class CoinSource:
    """一个金币组（决策边界换算好的米制目标；选择层只读 x/t/枚数/置信）。"""

    x_m: float
    observed_count: float
    conf: float
    t_miss_s: float | None
    age_ms: float = 0.0
    src: str = "group"


@dataclass(frozen=True)
class BonusSource:
    """一辆奖励车（撞上去；t_miss 由像素到站仪器给出）。"""

    x_m: float
    t_miss_s: float
    conf: float
    tol_m: float | None = None
    age_ms: float = 0.0
    src: str = "pixel"


@dataclass(frozen=True)
class CarSource:
    """一辆普通车的米制到站仪器（x/t；t=None=不接近/静态占位）。

    ``z_m`` 是该车在当前走廊内的纵深（米）；有值时调用方把 x 换算成
    「相对当地中心线」的横向偏移（弯道不把远处车读成偏一道）。"""

    x_m: float
    t_meet_s: float | None
    t_overtake_s: float | None = None
    bridged: bool = False
    z_m: float | None = None
    age_ms: float = 0.0
    src: str = "pixel"
    # 纵向近（t=None 时可当静态障碍的唯一许可）与真实半宽（米，None=标称）
    near: bool = False
    half_w_m: float | None = None


@dataclass(frozen=True)
class Waypoint:
    t: float
    x: float
    src: str                      # reward / pass / coin / center


@dataclass(frozen=True)
class SelectionReport:
    layer: int                    # 1~4；0=无路（hold）
    waypoints: tuple[Waypoint, ...]
    target_x_m: float
    why: str                      # reward/pass/coin/center/center:coarse/hold
    road_ok: bool
    maneuver: bool


@dataclass(frozen=True)
class SelectionParams:
    """选择层参数（数值真源 decision.json selection 节）。"""

    lane_w_m: float
    horizon_s: float = 3.0
    max_waypoints: int = 4
    graze_gap_lane: float = 0.65
    center_band_lane: float = 0.3
    opportunity_tol_lane: float = 0.75
    # 金币主目标只吃「当前线附近」的组（录制对照：人类不追 0.8 道外的散币）；
    # 选中后组消失仍保持 coin_hold_s，更高层出现立即覆盖。
    coin_pick_tol_lane: float = 1.2
    coin_hold_s: float = 0.6
    # 横移可达性由执行层同一套运动学推导（道/s²、道/s），不再用换道时间
    # 经验值；reaction/margin 只补感知与执行延迟。
    a_lat_lane_s2: float = 6.0
    v_lat_max_lane_s: float = 2.5
    reaction_s: float = 0.13
    margin_s: float = 0.15
    # 已基本对齐的目标不需要横移准备：只留反应时间，不套 t_move 基础项。
    aligned_lane: float = 0.1
    # 奖励车在这一横距内视为「已经对准」：不再要求横移准备时间。
    reward_aligned_lane: float = 0.2
    wall_margin_lane: float = 0.35
    center_lookahead_m: float = 0.0
    # 无即时目标时向未来机会侧预置：只看短视界外、车侧 2.5 道内、6 秒内的
    # 奖励车/普通车/金币，锚点最多偏 1 道（预置位，不追具体目标）。
    preposition_enabled: bool = True
    preposition_horizon_s: float = 6.0
    preposition_max_lane: float = 2.5
    preposition_bias_lane: float = 1.0
    switch_dwell_s: float = 0.2
    reward_tol_lane: float = 1.5
    car_half_lane: float = 0.27
    ego_half_lane: float = 0.27


def _t_move(dx_m: float, p: SelectionParams) -> float:
    """横移所需时间：先恒加速到横向速度上限，再匀速（道/s²、道/s）。

    与执行层是同一套运动学：0.7 道约 0.5 秒，而不是按人工变道节奏估的
    2 秒量级。自车当前横向速度未知，按静止起步算，偏保守。"""
    d = abs(dx_m) / p.lane_w_m
    a = p.a_lat_lane_s2
    vmax = p.v_lat_max_lane_s
    if d <= 0.0:
        return 0.0
    if a <= 0.0 or vmax <= 0.0:
        return math.inf
    t_acc = vmax / a
    d_acc = 0.5 * a * t_acc * t_acc
    if d <= d_acc:
        return math.sqrt(2.0 * d / a)
    return t_acc + (d - d_acc) / vmax


def _reachable(x: float, x0: float, t: float | None,
               p: SelectionParams) -> bool:
    if t is None or not math.isfinite(t) or t <= 0.0:
        return False
    if abs(x - x0) <= p.aligned_lane * p.lane_w_m:
        return t >= p.reaction_s
    return t >= _t_move(x - x0, p) + p.reaction_s + p.margin_s


def _blocked(cars: Sequence[Obj], x: float, t: float,
             p: SelectionParams) -> bool:
    """目标 x 在 t 时刻是否被另一辆车占住（只查会与自车同时到达的车）。"""
    half = (p.car_half_lane + p.ego_half_lane) * p.lane_w_m
    for c in cars:
        if c.t is None or c.t > t + p.margin_s:
            continue
        if c.t < -0.2:
            continue
        if abs(c.x - x) < half:
            return True
    return False


def _pick_reward(rewards: Sequence[Obj], cars: Sequence[Obj],
                 road: RoadFrame, ego: float,
                 p: SelectionParams) -> tuple[float, float, str] | None:
    for r in sorted(rewards, key=lambda o: (o.t if o.t is not None else math.inf)):
        if r.t is None or not (0.0 <= r.t <= p.horizon_s):
            continue
        x = road.clamp(r.x, p.wall_margin_lane * p.lane_w_m)
        if abs(r.x - x) > p.reward_tol_lane * p.lane_w_m:
            continue
        if abs(x - ego) > p.reward_aligned_lane * p.lane_w_m \
                and not _reachable(x, ego, r.t, p):
            continue
        if _blocked(cars, x, r.t, p):
            continue
        return x, r.t, "reward"
    return None


def _pass_x(c: Obj, road: RoadFrame, p: SelectionParams) -> float:
    """选车心两侧空间更大的一侧作为通过侧。"""
    room_l = (c.x - road.left_m) if road.left_m is not None else 0.0
    room_r = (road.right_m - c.x) if road.right_m is not None else 0.0
    side = 1.0 if room_r >= room_l else -1.0
    x = road.clamp(c.x + side * p.graze_gap_lane * p.lane_w_m,
                   p.wall_margin_lane * p.lane_w_m)
    return x


def _pick_pass(cars: Sequence[Obj], road: RoadFrame, ego: float,
               p: SelectionParams) -> tuple[float, float, str] | None:
    half = (p.car_half_lane + p.ego_half_lane) * p.lane_w_m
    cands = [c for c in cars
             if c.t is not None and 0.0 <= c.t <= p.horizon_s
             and abs(c.x - ego) <= half]
    for c in sorted(cands, key=lambda o: o.t):
        x = _pass_x(c, road, p)
        if abs(x - c.x) < half:
            continue                    # 夹进走廊后仍压在车里：这侧过不去
        if not _reachable(x, ego, c.t, p):
            continue
        others = [o for o in cars if o is not c]
        if _blocked(others, x, c.t, p):
            continue
        return x, c.t, "pass"
    return None


def _pick_coin(coins: Sequence[Obj], cars: Sequence[Obj],
               road: RoadFrame, ego: float,
               p: SelectionParams) -> tuple[float, float, str] | None:
    cands = [c for c in coins
             if c.t is not None and 0.0 <= c.t <= p.horizon_s]
    cands.sort(key=lambda o: (-max(o.count, 3.0) * o.conf, o.t))
    for c in cands:
        # 只吃当前线附近一道左右的组；更远的散币不追（追了会把车拉离
        # 币线，且 3 秒内也横移不过去）。
        if abs(c.x - ego) > p.coin_pick_tol_lane * p.lane_w_m:
            continue
        x = road.clamp(c.x, p.wall_margin_lane * p.lane_w_m)
        if abs(c.x - x) > p.opportunity_tol_lane * p.lane_w_m:
            continue
        if not _reachable(x, ego, c.t, p):
            continue
        if _blocked(cars, x, c.t, p):
            continue
        return x, c.t, "coin"
    return None


def _preposition_target(rewards: Sequence[Obj], cars: Sequence[Obj],
                        coins: Sequence[Obj], road: RoadFrame, ego: float,
                        p: SelectionParams) -> float | None:
    """短视界外的机会侧（预置位用）：取层级最高、同层最早到站的一个。"""
    if not p.preposition_enabled:
        return None
    lo, hi = p.horizon_s, p.preposition_horizon_s
    max_off = p.preposition_max_lane * p.lane_w_m
    best: tuple[int, float, float] | None = None
    for layer, objs in ((1, rewards), (2, cars), (3, coins)):
        for o in objs:
            if o.t is None or not math.isfinite(o.t) or not (lo < o.t <= hi):
                continue
            if abs(o.x - ego) > max_off:
                continue
            x = road.clamp(o.x, p.wall_margin_lane * p.lane_w_m)
            if abs(o.x - x) > p.opportunity_tol_lane * p.lane_w_m:
                continue
            if best is None or layer < best[0] \
                    or (layer == best[0] and o.t < best[1]):
                best = (layer, o.t, x)
    return None if best is None else best[2]


def _center_target(road: RoadFrame, ego: float,
                   p: SelectionParams,
                   coarse_lane: float | None = None) -> tuple[float, str, bool]:
    if not road.ok:
        return ego, "hold:no_road", False
    c = road.center_at(p.center_lookahead_m)
    if c is None:
        c = 0.0
    why = "center"
    if coarse_lane is not None and coarse_lane != 0.0:
        # 粗层方向：细层没有得分机会时去净空最长的车道，不贴回中心线。
        c += coarse_lane * p.lane_w_m
        why = "center:coarse"
    band = p.center_band_lane * p.lane_w_m
    maneuver = abs(ego - c) > band
    if not maneuver:
        x = ego                       # 带内不修正（治画龙的核心规则）
    else:
        x = c + math.copysign(band, ego - c)   # 带外只去近侧带边
    return road.clamp(x, p.wall_margin_lane * p.lane_w_m), why, maneuver


def _waypoints(layer: int, x_p: float, t_p: float,
               cars: Sequence[Obj], coins: Sequence[Obj],
               road: RoadFrame, ego: float,
               p: SelectionParams) -> tuple[Waypoint, ...]:
    """主目标之前最多 3 个顺路点：先保擦边车，再收顺路币。"""
    if layer == 4:
        return (Waypoint(t_p, x_p, "center"),)
    lo, hi = sorted((ego, x_p))
    slack = 0.05 * p.lane_w_m
    mids: list[Waypoint] = []

    def along(t: float) -> float:
        if t_p <= 1e-6:
            return x_p
        return ego + (x_p - ego) * (t / t_p)

    half = (p.car_half_lane + p.ego_half_lane) * p.lane_w_m
    for c in sorted([c for c in cars
                     if c.t is not None and 0.0 < c.t < t_p],
                    key=lambda o: o.t):
        if abs(c.x - along(c.t)) > half:
            continue
        x = _pass_x(c, road, p)
        if abs(x - c.x) < half:
            continue
        # 擦边是安全动作：不要求与主目标同向（为躲车短暂反向是必要的）
        mids.append(Waypoint(c.t, x, "pass"))

    for c in sorted([c for c in coins
                     if c.t is not None and 0.0 < c.t < t_p],
                    key=lambda o: o.t):
        if abs(c.x - along(c.t)) > p.opportunity_tol_lane * p.lane_w_m:
            continue
        x = road.clamp(c.x, p.wall_margin_lane * p.lane_w_m)
        if not (lo - slack <= x <= hi + slack):
            continue
        mids.append(Waypoint(c.t, x, "coin"))

    # 同一时刻只留一个点；按时间排序后逐点做过时与防蛇形检查
    mids.sort(key=lambda w: w.t)
    out: list[Waypoint] = []
    prev_t, prev_x = 0.0, ego
    for w in mids:
        if out and abs(w.t - out[-1].t) < 1e-6:
            continue
        if not _reachable(w.x, prev_x, w.t - prev_t, p):
            continue
        out.append(w)
        prev_t, prev_x = w.t, w.x
    # 主目标必须仍然来得及；来不及就从尾部丢中间点（不丢主目标）
    while out:
        last = out[-1]
        if _reachable(x_p, last.x, t_p - last.t, p):
            break
        out.pop()
    out = out[:max(0, p.max_waypoints - 1)]
    out.append(Waypoint(t_p, x_p, "reward" if layer == 1
                        else "pass" if layer == 2 else "coin"))
    return tuple(out)


def select(road: RoadFrame, rewards: Sequence[Obj],
           cars: Sequence[Obj], coins: Sequence[Obj],
           ego: float, p: SelectionParams,
           *, coarse_lane: float | None = None,
           committed: bool = False) -> SelectionReport:
    """纯选择：四层字典序 + 路点（无状态；驻留由 LayeredSelector 做）。

    ``coarse_lane``：粗层选定的车道（相对 z=0 中心线，道，右正）；只影响
    L4 回中兜底，机会层（奖励车/擦边/金币）仍由细层决定。None=纯回中。

    ``committed``：并线承诺期（路线已定目标道）。机会层与预置偏移全部关闭，
    只按承诺道回中——承诺期的目标不能再被奖励车/擦边/金币改道（契约 §4/§5）。"""
    if not road.ok:
        return SelectionReport(0, (), ego, "hold:no_road", False, False)
    if not committed:
        got = _pick_reward(rewards, cars, road, ego, p)
        if got is not None:
            x, t, why = got
            return SelectionReport(1, _waypoints(1, x, t, cars, coins, road,
                                                 ego, p), x, why, True, True)
        got = _pick_pass(cars, road, ego, p)
        if got is not None:
            x, t, why = got
            return SelectionReport(2, _waypoints(2, x, t, cars, coins, road,
                                                 ego, p), x, why, True, True)
        got = _pick_coin(coins, cars, road, ego, p)
        if got is not None:
            x, t, why = got
            return SelectionReport(3, _waypoints(3, x, t, cars, coins, road,
                                                 ego, p), x, why, True, True)
    x, why, maneuver = _center_target(road, ego, p, coarse_lane)
    if committed:
        return SelectionReport(4, (Waypoint(p.horizon_s, x, "commit"),),
                               x, "commit", True, maneuver)
    pre = _preposition_target(rewards, cars, coins, road, ego, p)
    if pre is not None:
        cap = p.preposition_bias_lane * p.lane_w_m
        x = x + max(-cap, min(cap, pre - x))
        x = road.clamp(x, p.wall_margin_lane * p.lane_w_m)
    return SelectionReport(4, (Waypoint(p.horizon_s, x, "center"),),
                           x, why, True, maneuver)


class LayeredSelector:
    """有状态包装：只做目标驻留与层切换，不改变分层规则。

    层变化立即切；同层目标变化超过半道且持续 switch_dwell_s 才切；
    现任目标被车占住时立即切（不停留等撞）。
    """

    def __init__(self, p: SelectionParams) -> None:
        self.p = p
        self.reset()

    def reset(self) -> None:
        self._layer: int | None = None
        self._target: float | None = None
        self._dwell_x: float | None = None
        self._dwell_t = 0.0
        self._coin_hold_t = 0.0
        self._coin_target: float | None = None
        self.last: SelectionReport | None = None

    def _adopt(self, rep: SelectionReport) -> SelectionReport:
        self._layer = rep.layer
        self._target = rep.target_x_m
        self._dwell_x = None
        self._dwell_t = 0.0
        if rep.layer == 3:
            self._coin_hold_t = self.p.coin_hold_s
            self._coin_target = rep.target_x_m
        else:
            self._coin_hold_t = 0.0
            self._coin_target = None
        return rep

    def _incumbent_blocked(self, cars: Sequence[Obj]) -> bool:
        if self._target is None:
            return False
        half = (self.p.car_half_lane + self.p.ego_half_lane) * self.p.lane_w_m
        return any(c.t is not None and -0.2 <= c.t <= self.p.horizon_s
                   and abs(c.x - self._target) < half for c in cars)

    def update(self, road: RoadFrame, rewards: Sequence[Obj],
               cars: Sequence[Obj], coins: Sequence[Obj],
               ego: float, dt_s: float,
               *, coarse_lane: float | None = None,
               committed: bool = False) -> SelectionReport:
        raw = select(road, rewards, cars, coins, ego, self.p,
                     coarse_lane=coarse_lane, committed=committed)
        self.last = raw
        # 承诺期：目标是路线定的道，驻留/金币短承诺都不参与——拖住或沿用
        # 旧目标都等于在并线中改道。
        if committed:
            return self._adopt(raw)
        # 金币短承诺：币组从视野里消失后，沿最后一条币线再走 coin_hold_s，
        # 不立刻回中心（录制对照：金币段中位只有 2 拍，车刚起步就放弃）。
        # 奖励车/普通车擦边出现仍立即覆盖；目标被车占住也立即放弃。
        if self._layer == 3 and raw.layer == 4 \
                and self._coin_hold_t > 0.0 and self._coin_target is not None \
                and not self._incumbent_blocked(cars):
            x = road.clamp(self._coin_target,
                           self.p.wall_margin_lane * self.p.lane_w_m)
            if abs(x - self._coin_target) <= 0.3 * self.p.lane_w_m:
                self._coin_hold_t -= dt_s
                if self._coin_hold_t > 0.0:
                    self._target = x
                    held = SelectionReport(
                        3, (Waypoint(0.0, x, "coin_hold"),), x,
                        "coin_hold", True, True)
                    self.last = held
                    return held
        if self._layer is None or raw.layer != self._layer:
            return self._adopt(raw)
        if raw.layer == 0:
            return self._adopt(raw)
        if self._target is None or self._incumbent_blocked(cars):
            return self._adopt(raw)
        d = abs(raw.target_x_m - self._target)
        if d <= 0.5 * self.p.lane_w_m:
            return self._adopt(raw)
        if self._dwell_x is None \
                or abs(raw.target_x_m - self._dwell_x) > 0.2 * self.p.lane_w_m:
            self._dwell_x = raw.target_x_m
            self._dwell_t = 0.0
        else:
            self._dwell_t += dt_s
        if self._dwell_t >= self.p.switch_dwell_s:
            return self._adopt(raw)
        # 未到驻留：保持现任目标，路点/层仍用本拍结果（trace 可见）
        held = SelectionReport(raw.layer, raw.waypoints, self._target,
                               raw.why, raw.road_ok, raw.maneuver)
        return held
