# -*- coding: utf-8 -*-
"""场景快照契约：决策层每拍组装一次，供轨迹评估与 trace 观测面消费。

坐标：**走廊中心在自车站为 0、右正、米**（与 `selection.RoadFrame` 同帧）。
对象只带评估必需的量（横位/到站时间/枚数/不确定度/年龄/来源/纵深），
不含像素、不含纵向速度。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.planner import step
from maaracing_master.plugins.speedrush.selection import (
    BonusSource, CarSource, CoinSource, RoadFrame)

SCENE_SCHEMA = 3
PLAN_SCHEMA = 1


@dataclass(frozen=True)
class SceneObj:
    kind: str                     # reward / car / coin
    x_m: float                    # 场景帧（走廊中心=0，右正）
    t_arrive_s: float | None      # 本拍到站时间；None=不接近/仪器缺位
    count: float = 1.0            # 金币枚数
    sigma_m: float = 0.0          # 横向不确定度（年龄膨胀后）
    age_ms: float = 0.0           # 最后观测年龄（只用于有效性与 sigma）
    src: str = ""                 # metric / pixel / far / group
    z_m: float | None = None
    # 有效域：缺失值不能和精确值同权消费。
    timed: bool = True            # 有到站时间（车：t 非 None）
    depth: bool = False           # 有纵深（只有车可能为 true）
    in_corridor: bool = True      # 横位在走廊内（越界值不可用于命中）
    near: bool = False            # 纵向近（t=None 车当障碍的唯一许可）
    half_w_m: float | None = None  # 车半宽（米，来自跟踪宽度；None=回退标称）
    coarse: bool = False          # 粗源（奖励车远场夹边值等）


@dataclass(frozen=True)
class EgoState:
    x_lane: float                 # planner 道单位（右正）
    v_lane_s: float
    steer_norm: float


@dataclass(frozen=True)
class Scene:
    schema: int
    fid: int
    ego_x_m: float                # 自车横位（场景帧，米）
    road: RoadFrame
    objs: tuple[SceneObj, ...]
    lane_w_m: float
    frame_age_ms: float
    corridor_age_ms: float | None
    stale: bool

    @property
    def ego_lane(self) -> float:
        return self.ego_x_m / self.lane_w_m

    def as_trace(self) -> dict:
        """紧凑 trace 面：对象逐条 8 元组。"""
        return {
            "schema": self.schema,
            "fid": self.fid,
            "ego_m": round(self.ego_x_m, 3),
            "lane_w": round(self.lane_w_m, 3),
            "frame_age_ms": round(self.frame_age_ms, 1),
            "corridor_age_ms": (None if self.corridor_age_ms is None
                                else round(self.corridor_age_ms, 1)),
            "stale": self.stale,
            "n": len(self.objs),
            "road": {
                "w": None if self.road.width_m is None
                else round(self.road.width_m, 3),
                "l": None if self.road.left_m is None
                else round(self.road.left_m, 3),
                "r": None if self.road.right_m is None
                else round(self.road.right_m, 3),
                "bands": [[round(b.z0, 2), round(b.z1, 2),
                           None if b.left is None else round(b.left, 3),
                           None if b.right is None else round(b.right, 3),
                           b.src,
                           None if b.age_ms is None else round(b.age_ms, 1),
                           round(b.conf, 3)]
                          for b in self.road.bands],
            },
            "objs": [[o.kind, round(o.x_m, 3),
                      None if o.t_arrive_s is None else round(o.t_arrive_s, 3),
                      round(o.count, 1), round(o.sigma_m, 3),
                      round(o.age_ms, 1), o.src,
                      None if o.z_m is None else round(o.z_m, 2),
                      o.timed, o.depth, o.in_corridor, o.near,
                      None if o.half_w_m is None else round(o.half_w_m, 3),
                      o.coarse]
                     for o in self.objs],
        }


def _sigma(age_ms: float, sigma0_m: float, rate_mps: float) -> float:
    return sigma0_m + rate_mps * max(age_ms, 0.0) / 1000.0


def _shift_road(road: RoadFrame, shift_m: float) -> RoadFrame:
    if shift_m == 0.0:
        return road
    return replace(
        road,
        bands=tuple(replace(
            b,
            left=None if b.left is None else b.left + shift_m,
            right=None if b.right is None else b.right + shift_m)
            for b in road.bands),
        left_m=None if road.left_m is None else road.left_m + shift_m,
        right_m=None if road.right_m is None else road.right_m + shift_m)


def shift_scene(scene: Scene, shift_m: float) -> Scene:
    """整帧平移（走廊带/对象/自车一起动，相对几何不变）。"""
    if shift_m == 0.0:
        return scene
    return replace(
        scene,
        ego_x_m=scene.ego_x_m + shift_m,
        road=_shift_road(scene.road, shift_m),
        objs=tuple(replace(o, x_m=o.x_m + shift_m) for o in scene.objs))


def _in_corridor(road: RoadFrame, x: float, z_m: float | None) -> bool:
    """横位是否落在走廊内；无纵深时用当前站路缘。"""
    left, right = road.left_m, road.right_m
    if z_m is not None:
        for b in road.bands:
            if b.z0 <= z_m <= b.z1 and b.left is not None \
                    and b.right is not None:
                left, right = b.left, b.right
                break
    if left is None or right is None:
        return True
    return left <= x <= right


def build_scene(*, fid: int, ego_m: float, road: RoadFrame,
                rewards: list[BonusSource], cars: list[CarSource],
                coins: list[CoinSource], executed_lane: float, origin: float,
                lane_w_m: float, frame_age_ms: float,
                corridor_age_ms: float | None,
                max_obj_age_s: float, corridor_age_max_s: float,
                sigma_obj0_m: float, sigma_obj_rate_mps: float,
                cam_z_datum_m: float, frame_shift_m: float = 0.0) -> Scene:
    """把同拍源表换算成场景帧并做年龄/σ 处理。

    ``frame_shift_m`` 把选择帧平移到规划器路心帧：选择帧原点=供数门 master
    路心，残量（master 在带平移系里的位置）由此补齐；不平移会让
    ``frame_delta`` 常年超阈值、走廊加宽到失效。"""
    def _aged_t(t: float | None, age_ms: float) -> float | None:
        """丢检目标的时间按龄前移：源里的到站时间是最后观测拍的，不是本拍。"""
        if t is None:
            return None
        return t - age_ms / 1000.0

    objs: list[SceneObj] = []
    for b in rewards:
        age = getattr(b, "age_ms", 0.0) or 0.0
        if age > max_obj_age_s * 1000.0:
            continue
        t = _aged_t(b.t_miss_s, age)
        if t is not None and t <= 0.0:
            continue                       # 已到站/已过：不再当未来目标
        x = b.x_m - origin
        src = getattr(b, "src", "pixel")
        objs.append(SceneObj(
            "reward", x, t, 1.0,
            _sigma(age, sigma_obj0_m, sigma_obj_rate_mps),
            age, src, None, timed=t is not None, depth=False,
            in_corridor=_in_corridor(road, x, None),
            coarse=(src == "far")))
    for c in cars:
        age = getattr(c, "age_ms", 0.0) or 0.0
        if age > max_obj_age_s * 1000.0:
            continue
        x = c.x_m - origin
        if c.z_m is not None:
            center = road.center_at(c.z_m - cam_z_datum_m)
            if center is not None:
                x -= center
        z_scene = None if c.z_m is None else c.z_m - cam_z_datum_m
        objs.append(SceneObj(
            "car", x, c.t_meet_s, 1.0,
            _sigma(age, sigma_obj0_m, sigma_obj_rate_mps),
            age, getattr(c, "src", "pixel"), c.z_m,
            timed=c.t_meet_s is not None,
            depth=c.z_m is not None,
            in_corridor=_in_corridor(road, x, z_scene),
            near=bool(getattr(c, "near", False)),
            half_w_m=getattr(c, "half_w_m", None)))
    for c in coins:
        age = getattr(c, "age_ms", 0.0) or 0.0
        if age > max_obj_age_s * 1000.0:
            continue
        t = _aged_t(c.t_miss_s, age)
        if t is not None and t <= 0.0:
            continue
        x = c.x_m - origin
        objs.append(SceneObj(
            "coin", x, t, float(c.observed_count),
            _sigma(age, sigma_obj0_m, sigma_obj_rate_mps),
            age, getattr(c, "src", "group"), None,
            timed=t is not None, depth=False,
            in_corridor=_in_corridor(road, x, None)))
    stale = corridor_age_ms is not None \
        and corridor_age_ms > corridor_age_max_s * 1000.0
    return shift_scene(
        Scene(SCENE_SCHEMA, fid, ego_m, road, tuple(objs), lane_w_m,
              frame_age_ms, corridor_age_ms, stale),
        frame_shift_m)


@dataclass(frozen=True)
class MotionPlan:
    schema: int
    valid: bool
    reason: str                    # plan:ok / no_scene / infeasible_* / unsure
    x_preview_m: float             # 场景帧（走廊中心=0）
    v_preview_mps: float
    u_feedforward: float
    layer: int                     # 1~4
    score_key: tuple
    n_feasible: int
    chosen: int
    cand_x: tuple[tuple[float, ...], ...]   # trace only（米，场景帧）
    x_preview_lane: float          # planner 道帧（已扣 frame_delta）
    v_preview_lane_s: float
    frame_delta_m: float
    corridor_uncertain: bool
    dt_s: float = 0.05
    value_total: float = 0.0
    collision_cost: float = 0.0
    corridor_viol_m: float = 0.0   # 被选候选的走廊侵入深度（米，0=可行）

    def as_trace(self) -> dict:
        return {
            "valid": self.valid, "reason": self.reason,
            "x": round(self.x_preview_m, 3),
            "v": round(self.v_preview_mps, 3),
            "u": round(self.u_feedforward, 3),
            "layer": self.layer, "n_ok": self.n_feasible,
            "chosen": self.chosen, "dt": self.dt_s,
            "key": [round(v, 4) if isinstance(v, float) else v
                    for v in self.score_key],
            "val": round(self.value_total, 2),
            "cc": round(self.collision_cost, 2),
            "fd": round(self.frame_delta_m, 3),
            "unc": self.corridor_uncertain,
            "cv": round(self.corridor_viol_m, 3),
            "cand": [[round(v, 3) for v in c] for c in self.cand_x],
        }


@dataclass(frozen=True)
class _Cand:
    idx: int
    u: float
    xs_m: tuple[float, ...]
    x_preview_m: float
    v_preview_lane: float
    feasible: bool
    min_clear_m: float
    reward_hits: int
    reward_value: float
    pass_count: int
    pass_value: float
    coin_count: float
    coin_error_m: float
    center_dev_m: float
    effort: float
    layer: int
    key: tuple
    value: float = 0.0
    collision_cost: float = 0.0
    corridor_viol_m: float = 0.0


def _envelope(scene: Scene, near_z_m: float, wall_margin_m: float,
              widen_m: float) -> tuple[float, float] | None:
    lo, hi = -float("inf"), float("inf")
    any_band = False
    for b in scene.road.bands:
        if b.z0 > near_z_m or b.z1 < 0.0:
            continue
        if b.left is None or b.right is None:
            continue
        any_band = True
        lo = max(lo, b.left + wall_margin_m - widen_m)
        hi = min(hi, b.right - wall_margin_m + widen_m)
    if not any_band:
        return None
    # 交集为空如实返回（lo>hi）：调用方会把所有候选判走廊不可行，
    # 不再当作“无约束”静默放行。
    return lo, hi


def _x_at(xs: tuple[float, ...], t: float, dt: float) -> float:
    if t <= 0:
        return xs[0]
    i = t / dt
    k = int(i)
    if k >= len(xs) - 1:
        return xs[-1]
    w = i - k
    return xs[k] + (xs[k + 1] - xs[k]) * w


class PlanGate:
    """候选时稳：价值差超 margin 且保持 dwell 才换，防逐拍翻面。"""

    def __init__(self, switch_margin: float, dwell_s: float,
                 epsilon: float, geom_margin: float = 0.03,
                 immediate_margin: float = 100.0,
                 infeasible_grace_s: float = 0.1):
        self.margin = switch_margin
        self.immediate = immediate_margin
        self.geom_margin = geom_margin
        self.dwell = dwell_s
        self.eps = max(epsilon, 1e-9)
        # 现任短暂不可行不立刻让位：供数来源逐拍切换时可行集闪一下，
        # 立即换会把计划输出打成每拍一个状态。宽限期内保持上一候选。
        self.infeasible_grace = infeasible_grace_s
        self._idx: int | None = None
        self._key: tuple | None = None
        self._streak = 0.0
        self._last: _Cand | None = None
        self._miss_s = 0.0

    def _q(self, key: tuple) -> tuple:
        return tuple(round(v / self.eps) * self.eps
                     if isinstance(v, float) else v for v in key)

    @staticmethod
    def _gap(a: tuple, b: tuple) -> float:
        for x, y in zip(a, b):
            if x == y:
                continue
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return abs(float(x) - float(y))
            return 1.0
        return 0.0

    def snapshot(self) -> tuple[int | None, float]:
        return self._idx, self._streak

    def reset(self) -> None:
        """清掉候选时稳记忆（相位切换时用）：门不得把上一相位的候选带进来。"""
        self._idx, self._key, self._streak = None, None, 0.0
        self._last, self._miss_s = None, 0.0

    def restore(self, idx: int | None, streak: float) -> None:
        self._idx, self._streak, self._key = idx, float(streak), None
        self._last, self._miss_s = None, 0.0

    def choose(self, cands: list[_Cand], dt: float) -> _Cand:
        """价值差超过 margin 并保持 dwell 才换现任；现任不可行立即让位。"""
        best = cands[0]
        inc = next((c for c in cands if c.idx == self._idx and c.feasible),
                   None)
        if inc is None:
            # 现任缺位/不可行：先看是不是单拍闪断。宽限期内保持上一候选，
            # 只有连续超过 infeasible_grace 或价值差达到立即门槛才让位。
            if self._last is not None:
                self._miss_s += dt
                immediate = (self._key is not None
                             and best.key[1] < self._key[1] - self.immediate)
                if not immediate and self._miss_s < self.infeasible_grace:
                    return self._last
            self._idx, self._key, self._streak = best.idx, self._q(best.key), 0.0
            self._last, self._miss_s = best, 0.0
            return best
        self._miss_s = 0.0
        kb, ki = self._q(best.key), self._q(inc.key)
        # 大价值差（避开一次碰撞/抓住高值窗口）不等 dwell——检测常有一拍
        # 闪烁，等 0.2s 会错过避撞窗口。
        if kb[1] < ki[1] - self.immediate:
            self._idx, self._key, self._streak = best.idx, kb, 0.0
            self._last = best
            return best
        better = False
        if kb[1] < ki[1] - self.margin:
            better = True                       # 价值显著更高
        elif abs(kb[1] - ki[1]) <= self.eps:
            # 同价值时比较几何/动作键；中心修正量级远小于价值分，
            # 用独立的小门槛，否则车会永远卡在最初的直行候选上。
            better = kb[2:] < ki[2:] \
                and self._gap(kb[2:], ki[2:]) > self.geom_margin
        if better:
            self._streak += dt
            if self._streak >= self.dwell:
                self._idx, self._key, self._streak = best.idx, kb, 0.0
                self._last = best
                return best
        else:
            self._streak = 0.0
        self._key = ki
        self._last = inc
        return inc


def plan_motion(scene: Scene, ego: EgoState, motion, selection,
                plant, gate: PlanGate | None = None, *,
                coarse_lane: float | None = None,
                committed: bool = False) -> MotionPlan:
    """场景 → 候选轨迹 → 四层字典序 → PlanGate。纯函数（gate 除外）。

    ``coarse_lane``：粗层目标道（相对 z=0 中心线，道）；只平移无机会时的
    中心吸引项——奖励车/擦边/金币的价值仍按场景对象计。

    ``committed``：并线承诺期（路线已定目标道）。机会层（奖励车/擦边/金币）
    与预置偏移关闭，只按承诺道做中心吸引——目标道不能被计划静默替换。"""
    lane_w = scene.lane_w_m
    if scene.stale:
        return MotionPlan(
            PLAN_SCHEMA, False, "plan:stale", scene.ego_x_m, 0.0, 0.0, 4,
            (True,), 0, -1, (), ego.x_lane, 0.0, 0.0, True)
    dt = motion.dt_s
    n = max(1, int(round(motion.horizon_s / dt)))
    horizon = n * dt
    frame_delta = scene.ego_x_m - ego.x_lane * lane_w
    uncertain = abs(frame_delta) > motion.frame_delta_max_m
    widen = 0.0
    if uncertain:
        widen = min(abs(frame_delta), motion.uncertain_widen_max_m)
    env = _envelope(scene, motion.near_z_m,
                    selection.wall_margin_lane * lane_w, widen)
    if env is None:
        uncertain = True
    ego_half = selection.ego_half_lane * lane_w
    car_half = selection.car_half_lane * lane_w
    graze = motion.graze_gap_lane * lane_w
    # 大车不作擦边目标（按真实宽度判）；碰撞仍吃它自己的半宽
    graze_max_w = motion.graze_max_width_m

    def _half(c: SceneObj) -> float:
        return c.half_w_m if c.half_w_m is not None else car_half

    def _large(c: SceneObj) -> bool:
        return c.half_w_m is not None and c.half_w_m * 2.0 > graze_max_w
    coin_half = motion.coin_collect_half_lane * lane_w
    reward_half = ego_half + car_half
    center0 = scene.road.center_at(0.0) or 0.0
    if coarse_lane is not None:
        center0 += coarse_lane * lane_w

    def sig(t: float) -> float:
        return motion.sigma_ego0_m + motion.sigma_ego_rate_mps * t

    shortened = False
    if sig(horizon) > motion.sigma_ego_max_m > 0:
        n = max(1, int((motion.sigma_ego_max_m - motion.sigma_ego0_m)
                       / max(motion.sigma_ego_rate_mps, 1e-9) / dt))
        horizon = n * dt
        shortened = True

    cars = [o for o in scene.objs if o.kind == "car"]
    rewards = [] if committed else [o for o in scene.objs
                                    if o.kind == "reward"]
    # 币门与选择层同源（coin_pick_tol_lane）：计划不得重新选择选择层够不着的
    # 机会——肩上的币组选择层拒了，计划层却会为它打满杆（20261026 局 s1#3
    # 8.1~8.5s：币在 −2.2 道，计划 L3 满杆左打，车压上左肩）。
    coin_reach_m = selection.coin_pick_tol_lane * lane_w
    coins = [] if committed else [
        o for o in scene.objs
        if o.kind == "coin" and abs(o.x_m - scene.ego_x_m) <= coin_reach_m]
    ts = [k * dt for k in range(n + 1)]
    us = list(motion.steer_levels) + [ego.steer_norm]
    # 有距无速域是否在场：在场时解除 L4 维护性杆幅上限（这是避让，不是追路心）
    range_hazard_active = False
    if motion.range_hazard_m > 0.0:
        for c in cars:
            if c.timed or not c.depth or not c.in_corridor \
                    or _large(c) or c.z_m is None:
                continue
            if motion.near_z_m < c.z_m <= motion.range_hazard_m \
                    and abs(c.x_m - scene.ego_x_m) <= lane_w * 0.65:
                range_hazard_active = True
                break
    cands: list[_Cand] = []
    for idx, u in enumerate(us):
        x, v, steer = scene.ego_lane, ego.v_lane_s, ego.steer_norm
        xs_lane = [x]
        vs_lane = [v]
        for _ in range(n):
            x, v, steer = step(x, v, steer, u, dt, plant)
            xs_lane.append(x)
            vs_lane.append(v)
        xs_m = tuple(v * lane_w for v in xs_lane)

        def clearance(c, t: float) -> float:
            # 0=物理包络相切；负=重叠深度。只用物理包络 + 对象 σ。
            return (abs(_x_at(xs_m, t, dt) - c.x_m)
                    - (ego_half + _half(c) + c.sigma_m))

        # 时变车只在到达时刻插值判一次；静态近车取全时域最小间距。
        # t=None 的车只有在“纵向确实近”时才当障碍（far/无纵深的不再是墙）。
        clears: list[float] = []
        for c in cars:
            if not c.in_corridor:
                continue
            if c.t_arrive_s is None:
                if not c.near or abs(c.x_m - scene.ego_x_m) > graze:
                    continue
                clears.append(min((clearance(c, t) for t in ts),
                                  default=float("inf")))
            elif motion.collision_min_lead_s <= c.t_arrive_s <= horizon:
                clears.append(clearance(c, c.t_arrive_s))
        collision_cost = sum(
            motion.cost_collision + (-d) * motion.cost_collision_depth_m
            for d in clears if d < 0.0)
        min_clear = min(clears, default=float("inf"))
        # 走廊侵入深度（米，检查时域均值）：0=全程在包络内。可行集为空时
        # 它是唯一的逃生判据——布尔「在/不在」一旦全灭就退化成任意候选。
        corridor_viol = 0.0
        if env is not None:
            chk = [xx for xx, tt in zip(xs_m, ts)
                   if tt <= motion.corridor_check_s + 1e-9]
            corridor_viol = sum(max(0.0, env[0] - xx, xx - env[1])
                                for xx in chk) / max(1, len(chk))
        corridor_ok = corridor_viol <= 0.0

        reward_hits = 0
        reward_hit_ids: set[int] = set()
        for r in rewards:
            # 越界/无时间/粗源（远场夹边）不作为命中目标
            if not r.timed or not r.in_corridor or r.coarse:
                continue
            if r.t_arrive_s is None or r.t_arrive_s > horizon \
                    or r.t_arrive_s < motion.hit_min_lead_s:
                continue
            xr = _x_at(xs_m, r.t_arrive_s, dt)
            # 命中判据只加对象定位 σ：自车 σ 加进命中半径会把“够不到”
            # 也算成命中（影子实测 9~14% 奖励车命中行物理上够不到）。
            tol = reward_half + r.sigma_m
            if abs(xr - r.x_m) <= tol:
                reward_hits += 1
                reward_hit_ids.add(id(r))
        reward_value = motion.value_reward * reward_hits
        # 近时目标不得“迎上去”：到站时间已短于碰撞提前量的车，横移躲不开，
        # 但仍可以**不主动往它所在的横向方向并**——换道进它的车道不是反应
        # 不及，是选择错误（2026-10-19 局 212207_p1 皮卡与 212252_p2 绿车）。
        late_block = False
        for c in cars:
            if not c.timed or not c.in_corridor or c.t_arrive_s is None:
                continue
            if not (0.0 < c.t_arrive_s < motion.collision_min_lead_s):
                continue
            if abs(c.x_m - scene.ego_x_m) <= (
                    ego_half + _half(c) + motion.collision_margin_m):
                continue                    # 已重叠：不是换道选择问题
            toward = 1.0 if c.x_m > scene.ego_x_m else -1.0
            # 杆指令朝它，或当前惯性朝它而杆不反打——都算“迎上去”。
            # 这不是软代价：0.35s 内到站的车横移躲不开，唯一正确的动作
            # 是不往它那侧并；允许它被擦车/金币价值盖过就是重复撞车。
            if u * toward > 0.0 or (ego.v_lane_s * toward > 0.0
                                    and u * toward >= 0.0):
                late_block = True
        # 普通车碰撞只扣价值，不再判不可行（RULES §4.3：碰撞只损失时间）。
        # 例外：0.35s 内的车不允许“主动迎上去”——那是换道选择，不是反应不及。
        feasible = corridor_ok and not late_block
        # 有距无速域（第二项）：本车道前方、纵深在圈内但无到站时间的车，
        # 按保守速度折算一个到站时刻，凡在该时刻仍与车体重叠的候选直接扣分。
        # 这是“别等到时间出现才动”的硬提示，不参与奖励。
        range_cost = 0.0
        range_h = motion.range_hazard_m
        if range_h > 0.0 and motion.range_hazard_speed_mps > 0.0:
            for c in cars:
                if c.timed or not c.depth or not c.in_corridor \
                        or _large(c) or c.z_m is None:
                    continue
                if not (motion.near_z_m < c.z_m <= range_h):
                    continue
                if abs(c.x_m - scene.ego_x_m) > lane_w * 0.65:
                    continue
                t_h = min(horizon, c.z_m / motion.range_hazard_speed_mps)
                overlap = (ego_half + _half(c) + c.sigma_m
                           + motion.collision_margin_m)
                d_h = abs(_x_at(xs_m, t_h, dt) - c.x_m)
                if d_h < overlap:
                    urgency = 1.0 - c.z_m / range_h
                    range_cost += (motion.cost_collision * urgency
                                   * (1.0 - d_h / overlap)
                                   * motion.range_hazard_gain)
        pass_count = 0
        for c in (() if committed else cars):
            # 无到站时间的车只当静态障碍，不算“擦车通过”：
            # t=None 的并列车会让 t=0 的初始距离冒充一次超车。
            # 越界读数与大车同理：不作擦边目标。
            if not c.timed or not c.in_corridor or _large(c):
                continue
            tc = c.t_arrive_s
            if tc is None or tc > horizon or tc < motion.hit_min_lead_s:
                continue
            d = abs(_x_at(xs_m, tc, dt) - c.x_m)
            if ego_half + _half(c) + c.sigma_m \
                    + motion.collision_margin_m <= d <= graze:
                pass_count += 1
        pass_value = motion.value_graze * pass_count
        coin_hits, coin_value, coin_error = 0.0, 0.0, 0.0
        coin_hit_ids: set[int] = set()
        for c in coins:
            if not c.in_corridor:
                continue
            if c.t_arrive_s is None or c.t_arrive_s > horizon \
                    or c.t_arrive_s < motion.hit_min_lead_s:
                continue
            xr = _x_at(xs_m, c.t_arrive_s, dt)
            err = abs(xr - c.x_m)
            tol = coin_half + c.sigma_m
            if err <= tol:
                coin_hits += 1
                coin_hit_ids.add(id(c))
                coin_value += (motion.value_coin_air if c.count >= 4
                               else motion.value_coin_group)
                coin_error += err
        if coin_hits > 0:
            coin_error /= coin_hits
        # 预置梯度：4s 内的金币/奖励车，按“末端离它多近”给连续小分——
        # 不等目标进入可达范围才动作（旧选择层 preposition 的价值版）。
        # 已被本候选命中的对象不重复计；不加普通车（近场擦车另算）。
        prep_value = 0.0
        prep_h = 0.0 if committed else motion.preposition_horizon_s
        if prep_h > 0.0:
            radius = max(motion.preposition_radius_lane * lane_w, 1e-6)
            for r in rewards:
                if not r.in_corridor or id(r) in reward_hit_ids \
                        or r.t_arrive_s is None \
                        or not (horizon < r.t_arrive_s <= prep_h):
                    continue
                x_ref = _x_at(xs_m, min(r.t_arrive_s, horizon), dt)
                prox = max(0.0, 1.0 - abs(x_ref - r.x_m) / radius)
                prep_value += motion.value_reward \
                    * motion.preposition_discount * prox \
                    * (0.5 if r.coarse else 1.0)   # 粗源位置只配半个权重
            for c in coins:
                if not c.in_corridor or id(c) in coin_hit_ids \
                        or c.t_arrive_s is None \
                        or not (horizon < c.t_arrive_s <= prep_h):
                    continue
                x_ref = _x_at(xs_m, min(c.t_arrive_s, horizon), dt)
                prox = max(0.0, 1.0 - abs(x_ref - c.x_m) / radius)
                prep_value += (motion.value_coin_air if c.count >= 4
                               else motion.value_coin_group) \
                    * motion.preposition_discount * prox
            # 普通车也预置：目标不是车心，而是候选所在侧的擦车距离，
            # 让车提前贴近超车线，而不是等车到眼前才反应。
            for c in cars:
                if not c.timed or not c.in_corridor or _large(c) \
                        or c.t_arrive_s is None \
                        or not (horizon < c.t_arrive_s <= prep_h):
                    continue
                x_ref = _x_at(xs_m, min(c.t_arrive_s, horizon), dt)
                side = 1.0 if x_ref >= c.x_m else -1.0
                target = c.x_m + side * graze
                prox = max(0.0, 1.0 - abs(x_ref - target) / radius)
                prep_value += motion.value_graze \
                    * motion.preposition_discount * prox
            # 有距无速（2026-10-19 局 204308_p1）：本车道前方有纵深但速度
            # 窗口未开的车，按距离持续给“往空侧并”的小分——不等到站时间。
            # 不参与擦车/命中奖励，只做提前横移；越近越强，出圈即停。
            range_h = motion.range_hazard_m
            if range_h > 0.0:
                left = scene.road.left_m if scene.road.left_m is not None \
                    else -lane_w
                right = scene.road.right_m if scene.road.right_m is not None \
                    else lane_w
                for c in cars:
                    if c.timed or not c.depth or not c.in_corridor \
                            or _large(c) or c.z_m is None:
                        continue
                    if not (motion.near_z_m < c.z_m <= range_h):
                        continue
                    if abs(c.x_m - scene.ego_x_m) > lane_w * 0.65:
                        continue
                    urgency = 1.0 - c.z_m / range_h
                    side = 1.0 if (right - c.x_m) >= (c.x_m - left) else -1.0
                    target = c.x_m + side * graze
                    target = max(left + graze * 0.5,
                                 min(right - graze * 0.5, target))
                    # 按到达该车的时刻取横位，不拿 2s 末端瞄——否则会为了
                    # “2 秒后离目标更近”而选轻打舵，正是晚并线的来源。
                    t_h = min(horizon, c.z_m
                              / max(motion.range_hazard_speed_mps, 1e-6))
                    x_ref = _x_at(xs_m, t_h, dt)
                    prox = max(0.0, 1.0 - abs(x_ref - target) / radius)
                    prep_value += motion.value_graze * urgency * prox \
                        * motion.preposition_discount * motion.range_hazard_gain
        value = (reward_value + pass_value + coin_value - collision_cost
                 + prep_value - range_cost)
        # 中心目标带死区：带内不修正（旧选择层同口径），只在带外惩罚超出量。
        band_m = motion.center_band_lane * lane_w
        center_dev = sum(max(0.0, abs(xx - center0) - band_m)
                         for xx in xs_m) / len(xs_m)
        # 动作成本按“相对当前杆”的改动量计：带内无修正时优先保持正在进行的动作。
        effort = abs(u - ego.steer_norm) * horizon
        if reward_hits:
            layer = 1
        elif pass_count:
            layer = 2
        elif coin_hits:
            layer = 3
        else:
            layer = 4
        # 无机会时限制维护性杆幅：避免计划用满舵去追一个偏掉的路心。
        # 有距无速域在场时例外——那是避让，不是追路心。
        overcap = 1e6 if (layer == 4 and not range_hazard_active
                          and abs(u) > motion.center_stick_max) else 0.0
        key = (not feasible, -value, overcap, center_dev, effort, idx)
        i = max(1, min(n, int(round(motion.preview_s / dt))))
        cands.append(_Cand(
            idx, u, xs_m, xs_m[i], vs_lane[i], feasible,
            min_clear, reward_hits, reward_value, pass_count, pass_value,
            coin_hits, coin_error, center_dev, effort, layer, key,
            value, collision_cost, corridor_viol))

    feasible = [c for c in cands if c.feasible]
    reason = "plan:ok"
    chosen: _Cand
    if feasible:
        feasible.sort(key=lambda c: c.key)
        chosen = gate.choose(feasible, dt) if gate is not None else feasible[0]
        valid = True
        if uncertain:
            reason = "plan:ok_uncertain"
        if shortened:
            reason = "plan:ok_shortened"
    else:
        # 可行集为空 = 自车已压在走廊边距内（贴墙行驶的日常状态，不是异常）。
        # 此时目标是「最快脱离侵入」，不是「位移最小」：后者会挑反向满舵去
        # 抵消逃离速度，把车钉在墙上（2026-10-23 局 192643_p2 fid 3653~3670，
        # 车在左缘而计划满舵左，连续 0.85 s 撞护栏）。
        valid = False
        chosen = min(cands, key=lambda c: (c.corridor_viol_m, -c.value,
                                           c.effort, c.idx))
        reason = "plan:infeasible_corridor"
    return MotionPlan(
        PLAN_SCHEMA, valid, reason, chosen.x_preview_m,
        chosen.v_preview_lane * lane_w, chosen.u, chosen.layer, chosen.key,
        len(feasible), chosen.idx, tuple(c.xs_m for c in cands),
        (chosen.x_preview_m - frame_delta) / lane_w,
        chosen.v_preview_lane, frame_delta, uncertain, dt,
        chosen.value, chosen.collision_cost, chosen.corridor_viol_m)
