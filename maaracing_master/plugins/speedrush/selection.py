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

**跨线唯一入口**（2026-10-27）：给了 ``commit_x_m``
（本拍承诺道中心）时，机会层目标超出承诺道安全区间就不再拽车跨车道线——
本拍目标贴到区间边界内，越线意图以 ``lane_request`` 发出。道内
微调永远逐拍实时，不锁方向。

**坐标**：选择层坐标 = 走廊中心在自车站为 0、右正。调用方负责把走廊带、
目标、自车位置换算进该坐标（origin 一次平移）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from maaracing_master.plugins.speedrush.planner import lane_move_time


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
    # 越界机会目标 → 换道请求（道，相对承诺道；None=无请求）。
    # 给了 commit_x_m 时跨车道线的目标不再直接拽，改发请求。
    lane_request: float | None = None


@dataclass(frozen=True)
class SelectionParams:
    """选择层参数（数值真源 decision.json selection 节）。"""

    lane_w_m: float
    horizon_s: float = 3.0
    max_waypoints: int = 4
    graze_gap_lane: float = 0.68
    center_band_lane: float = 0.3
    opportunity_tol_lane: float = 0.75
    # 金币主目标只吃「当前线附近」的组（录制对照：人类不追 0.8 道外的散币）；
    # 选中后组消失仍保持 coin_hold_s，更高层出现立即覆盖。
    coin_pick_tol_lane: float = 1.2
    coin_hold_s: float = 0.6
    # 横移可达性用执行层同一条闭环律（k_lat / tau_heading / heading_max，
    # 见 ``planner.lane_move_time``）；这里的四个数由配置从执行层抄一份，
    # 不另立模型。reaction/margin 只补感知与执行延迟。
    k_lat_lane_s: float = 1.2
    tau_heading_s: float = 0.25
    heading_max_rad: float = 0.15
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
    # 车道内安全区间的车身余量（道）：车心可在车道中心 ±(0.5−半宽−余量)
    # 内活动而不压线。0.06 道≈0.20m，配 0.27 道半宽得 ±0.17 道——实测
    # 车道内可偏 0.23 道，留 0.06 道给检测/执行抖动（20261027 局 134222_1
    # s1 9.1s 车心偏 0.16 道，仍在区间内）。
    lane_clearance_lane: float = 0.06


def _t_move(dx_m: float, p: SelectionParams) -> float:
    """横移直到「已对齐」所需时间——执行层那条闭环律的同一份答案。

    到位判据用 ``aligned_lane``（残差进带即不再要求准备时间）。旧实现按
    「先恒加速到横速上限再匀速」估 0.7 道约 0.5 秒，比实车快一倍多：实车
    0.7 道要约 1.1 秒、一道约 1.5 秒（台架与实机同口径）。乐观的估计会把
    决策放到车到眼前才起意，执行层又按真实车判到不了，于是两边各说各话。"""
    return lane_move_time(abs(dx_m) / p.lane_w_m,
                          k_lat_lane_s=p.k_lat_lane_s,
                          tau_heading_s=p.tau_heading_s,
                          heading_max_rad=p.heading_max_rad,
                          settle_lane=p.aligned_lane)


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


def _lane_interval(ref_x: float, p: SelectionParams,
                   road: RoadFrame) -> tuple[float, float]:
    """承诺道内的可活动区间（车心安全区间，与 L4 回中同一把尺）。"""
    half = (0.5 - p.ego_half_lane - p.lane_clearance_lane) * p.lane_w_m
    return _clamp_span(ref_x - max(0.0, half), ref_x + max(0.0, half), p, road)


def _lane_span(ref_x: float, p: SelectionParams,
               road: RoadFrame) -> tuple[float, float]:
    """承诺道的**跨度**（车心不出这一道的范围：中心 ±半道）。

    与 `_lane_interval` 的分工：区间是「不压线的可活动带」（回中与待发请求
    的预备位用它），跨度是「算不算另一条道」的判据（机会目标在跨度内照旧
    直追——吃同道偏一侧的币线/奖励车是得分动作，不是变道）。"""
    return _clamp_span(ref_x - 0.5 * p.lane_w_m, ref_x + 0.5 * p.lane_w_m,
                       p, road)


def _clamp_span(lo: float, hi: float, p: SelectionParams,
                road: RoadFrame) -> tuple[float, float]:
    if road.left_m is not None:
        lo = max(lo, road.left_m + p.wall_margin_lane * p.lane_w_m)
    if road.right_m is not None:
        hi = min(hi, road.right_m - p.wall_margin_lane * p.lane_w_m)
    if hi < lo:
        lo = hi = 0.5 * (lo + hi)
    return lo, hi


def _in_interval(x: float, iv: tuple[float, float] | None) -> bool:
    return iv is None or (iv[0] - 1e-9 <= x <= iv[1] + 1e-9)


def _edge_of(x: float, iv: tuple[float, float]) -> float:
    return min(max(x, iv[0]), iv[1])


def _req_lane(x: float, commit_x: float | None,
              p: SelectionParams) -> float | None:
    """出界目标所在道（相对承诺道）：只允许相邻 ±1 道，再远不请求。"""
    if commit_x is None:
        return None
    k = round((x - commit_x) / p.lane_w_m)
    return k if abs(k) == 1 else None


def _pick_reward(rewards: Sequence[Obj], cars: Sequence[Obj],
                 road: RoadFrame, ego: float,
                 p: SelectionParams,
                 commit_x: float | None = None,
                 ) -> tuple[tuple[float, float, str] | None, float | None]:
    """返回 (本拍可执行目标, 换道请求)——**两者可以都为 None**。

    出跨度的奖励车只产请求、不占目标位：占着目标位就等于把后面的擦边/让位
    层整拍屏蔽（一辆本拍根本撞不到的邻道奖励车取消掉让位动作＝撞车）。"""
    sp = _lane_span(commit_x, p, road) if commit_x is not None else None
    pending: float | None = None
    for r in sorted(rewards, key=lambda o: (o.t if o.t is not None else math.inf)):
        if r.t is None or not (0.0 <= r.t <= p.horizon_s):
            continue
        x = road.clamp(r.x, p.wall_margin_lane * p.lane_w_m)
        if abs(r.x - x) > p.reward_tol_lane * p.lane_w_m:
            continue
        if sp is not None and not _in_interval(r.x, sp):
            # 奖励车在隔壁道：本拍不拽过去（撞它得先站对道），发换道请求
            req_x = _req_lane(r.x, commit_x, p)
            if pending is None and req_x is not None and \
                    not _blocked(cars, r.x, r.t, p) and _reachable(r.x, ego, r.t, p):
                pending = req_x
            continue
        if abs(x - ego) > p.reward_aligned_lane * p.lane_w_m \
                and not _reachable(x, ego, r.t, p):
            continue
        if _blocked(cars, x, r.t, p):
            continue
        return (x, r.t, "reward"), None
    return None, pending


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
    """擦边/让位：逐拍实时的安全动作，**不受跨线闸门约束，也不许被请求屏蔽**。

    挤过去所需的横向量（graze_gap 0.68 道）本身就压过车道线——这是「微
    调」不是「变道」：车一过就回中，不会停在两道中间。若把它也夹进安全
    区间（0.17 道），0 道上的车只让开 0.17（车半宽 0.27）＝迎面撞上去。
    """
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


def _coin_aim(coin_x: float, ego: float, p: SelectionParams) -> float:
    """币不必正中：车有半宽，币心落在车身带内就保持原线，带外只走到带边。

    收集判据（计划层 `coin_collect_half_lane`=0.35 道）比车身半宽（0.27
    道）宽出币半径；这里取车身半宽作保守带，币心在带内不改目标——
    为偏 0.2 道的币去拽方向盘，正是人工局里金币层反复进出、意图抖动的
    来源之一。"""
    band = p.ego_half_lane * p.lane_w_m
    d = coin_x - ego
    if abs(d) <= band:
        return ego
    return coin_x - math.copysign(band, d)


def _pick_coin(coins: Sequence[Obj], cars: Sequence[Obj],
               road: RoadFrame, ego: float,
               p: SelectionParams,
               commit_x: float | None = None,
               ) -> tuple[tuple[float, float, str] | None, float | None]:
    """返回 (本拍可执行目标, 换道请求)，口径同 `_pick_reward`。"""
    cands = [c for c in coins
             if c.t is not None and 0.0 <= c.t <= p.horizon_s]
    cands.sort(key=lambda o: (-max(o.count, 3.0) * o.conf, o.t))
    sp = _lane_span(commit_x, p, road) if commit_x is not None else None
    pending: float | None = None
    for c in cands:
        # 只吃当前线附近一道左右的组；更远的散币不追（追了会把车拉离
        # 币线，且 3 秒内也横移不过去）。
        if abs(c.x - ego) > p.coin_pick_tol_lane * p.lane_w_m:
            continue
        x = road.clamp(_coin_aim(c.x, ego, p),
                       p.wall_margin_lane * p.lane_w_m)
        if abs(c.x - x) > p.opportunity_tol_lane * p.lane_w_m:
            continue
        if not _reachable(x, ego, c.t, p):
            continue
        if _blocked(cars, x, c.t, p):
            continue
        if sp is not None and not _in_interval(x, sp):
            # 目标点（已按车身带收过）仍在承诺道外：本拍不拽（拽一半停在
            # 两道中间是这条门的来由），按到站时间发出换道请求
            req_x = _req_lane(x, commit_x, p)
            if pending is None and req_x is not None and \
                    _reachable(x, ego, c.t, p):
                pending = req_x
            continue
        return (x, c.t, "coin"), None
    return None, pending


def _preposition_target(rewards: Sequence[Obj], cars: Sequence[Obj],
                        coins: Sequence[Obj], road: RoadFrame, ego: float,
                        p: SelectionParams,
                        commit_x: float | None = None,
                        ) -> tuple[float, float | None] | None:
    """短视界外的机会侧（预置位用）：取层级最高、同层最早到站的一个。"""
    if not p.preposition_enabled:
        return None
    lo, hi = p.horizon_s, p.preposition_horizon_s
    max_off = p.preposition_max_lane * p.lane_w_m
    sp = _lane_span(commit_x, p, road) if commit_x is not None else None
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
    if best is None:
        return None
    x = best[2]
    # 请求只由得分机会发起（奖励车/金币）。未来普通车不发请求：擦边的收益
    # 在本道位置上，为它并到隔壁道＝跟到一辆车后面排队。
    req = None
    if sp is not None and best[0] != 2 and not _in_interval(x, sp):
        req = _req_lane(x, commit_x, p)
    return x, req


def _center_target(road: RoadFrame, ego: float,
                   p: SelectionParams,
                   coarse_lane: float | None = None) -> tuple[float, str, bool]:
    """L4 回中：目标不是车道中心一个点，而是「车心安全区间」。

    区间=车道中心 ±(0.5−车半宽−余量)，再用硬路缘（wall_margin_lane 中心
    余量）夹一层。区间内不修正；压在边界才回带到最近边界，不是追正中。
    旧实现固定 0.3 道带，比车道内可偏的物理量（约 0.23 道）还宽——带内
    不修正会把车放到压线位置，带外又追 0.3 道边，和计划层 0.5 道死区
    两把尺子打架（20261027 局 134222_1 s1 空路满杆的现场目标 0.30、
    车位 0.47，两把尺子都判「不用修」）。"""
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
    lo, hi = _lane_interval(c, p, road)
    maneuver = not (lo <= ego <= hi)
    x = ego if not maneuver else min(max(ego, lo), hi)
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
           committed: bool = False,
           commit_x_m: float | None = None) -> SelectionReport:
    """纯选择：四层字典序 + 路点（无状态；驻留由 LayeredSelector 做）。

    ``coarse_lane``：粗层选定的车道（相对 z=0 中心线，道，右正）；只影响
    L4 回中兜底，机会层（奖励车/擦边/金币）仍由细层决定。None=纯回中。

    ``committed``：并线承诺期（路线已定目标道）。机会层与预置偏移全部关闭，
    只按承诺道回中——承诺期的目标不能再被奖励车/擦边/金币改道（契约 §4/§5）。

    ``commit_x_m``：本拍承诺道中心（选择帧米）。给了它，**得分机会层**（奖励
    车/金币/预置）出本道跨度（±半道）的目标不再直接拽车跨车道线——邻道目标
    改发 ``lane_request``（道，相对承诺道），本拍目标只贴到安全区间边界（同向准备、
    不起程）。同道内的偏移照旧直追（吃偏一侧的币线是得分动作，不是变道）。
    擦边/让位与回中不受它约束：那是安全微调，永远逐拍实时。
    None=旧行为：机会目标连续拽。
    """
    if not road.ok:
        return SelectionReport(0, (), ego, "hold:no_road", False, False)
    iv = _lane_interval(commit_x_m, p, road) if commit_x_m is not None else None
    req: float | None = None
    if not committed:
        # 字典序裁决的是「本拍跟谁」：出跨度的机会只留下请求、不占目标位。
        # 若让它占位，后面的擦边/让位整拍不被调用（一辆本拍撞不到的邻道奖励
        # 车取消掉让位动作＝撞车）。
        hit, pend = _pick_reward(rewards, cars, road, ego, p, commit_x_m)
        if pend is not None:
            req = pend
        if hit is None:
            hit = _pick_pass(cars, road, ego, p)
        if hit is None:
            hit, pend = _pick_coin(coins, cars, road, ego, p, commit_x_m)
            if req is None and pend is not None:
                req = pend
        if hit is not None:
            x, t, why = hit
            layer = 1 if why == "reward" else 2 if why == "pass" else 3
            return SelectionReport(
                layer, _waypoints(layer, x, t, cars, coins, road, ego, p),
                x, why, True, True)
    x, why, maneuver = _center_target(road, ego, p, coarse_lane)
    pre = None if committed else _preposition_target(
        rewards, cars, coins, road, ego, p, commit_x_m)
    if pre is not None:
        pre_x, pre_req = pre
        if req is None:
            req = pre_req
        cap = p.preposition_bias_lane * p.lane_w_m
        x = x + max(-cap, min(cap, pre_x - x))
        x = road.clamp(x, p.wall_margin_lane * p.lane_w_m)
    if iv is not None and req is not None:
        # 越线请求挂着：本拍目标贴向请求侧的区间边界——同向准备、不起程。
        x = iv[1] if req > 0 else iv[0]
    if iv is not None:
        x = _edge_of(x, iv)
    if committed:
        return SelectionReport(4, (Waypoint(p.horizon_s, x, "commit"),),
                               x, "commit", True, maneuver)
    return SelectionReport(4, (Waypoint(p.horizon_s, x, "center"),),
                           x, why, True, maneuver, req)


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
               committed: bool = False,
               commit_x_m: float | None = None) -> SelectionReport:
        raw = select(road, rewards, cars, coins, ego, self.p,
                     coarse_lane=coarse_lane, committed=committed,
                     commit_x_m=commit_x_m)
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
                        "coin_hold", True, True, raw.lane_request)
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
                               raw.why, raw.road_ok, raw.maneuver,
                               raw.lane_request)
        return held
