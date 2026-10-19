# -*- coding: utf-8 -*-
"""场景快照契约：决策层每拍组装一次，供轨迹评估与 trace 观测面消费。

坐标：**走廊中心在自车站为 0、右正、米**（与 `selection.RoadFrame` 同帧）。
对象只带评估必需的量（横位/到站时间/枚数/不确定度/年龄/来源/纵深），
不含像素、不含纵向速度。
"""
from __future__ import annotations

from dataclasses import dataclass

from maaracing_master.plugins.speedrush.planner import step
from maaracing_master.plugins.speedrush.selection import (
    BonusSource, CarSource, CoinSource, RoadFrame)

SCENE_SCHEMA = 1
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
                      None if o.z_m is None else round(o.z_m, 2)]
                     for o in self.objs],
        }


def _sigma(age_ms: float, sigma0_m: float, rate_mps: float) -> float:
    return sigma0_m + rate_mps * max(age_ms, 0.0) / 1000.0


def build_scene(*, fid: int, ego_m: float, road: RoadFrame,
                rewards: list[BonusSource], cars: list[CarSource],
                coins: list[CoinSource], executed_lane: float, origin: float,
                lane_w_m: float, frame_age_ms: float,
                corridor_age_ms: float | None,
                max_obj_age_s: float, corridor_age_max_s: float,
                sigma_obj0_m: float, sigma_obj_rate_mps: float,
                cam_z_datum_m: float) -> Scene:
    """把同拍源表换算成场景帧并做年龄/σ 处理。"""
    objs: list[SceneObj] = []
    for b in rewards:
        age = getattr(b, "age_ms", 0.0) or 0.0
        if age > max_obj_age_s * 1000.0:
            continue
        objs.append(SceneObj("reward", b.x_m - origin, b.t_miss_s, 1.0,
                             _sigma(age, sigma_obj0_m, sigma_obj_rate_mps),
                             age, getattr(b, "src", "pixel")))
    for c in cars:
        age = getattr(c, "age_ms", 0.0) or 0.0
        if age > max_obj_age_s * 1000.0:
            continue
        x = c.x_m - origin
        if c.z_m is not None:
            center = road.center_at(c.z_m - cam_z_datum_m)
            if center is not None:
                x -= center
        objs.append(SceneObj("car", x, c.t_meet_s, 1.0,
                             _sigma(age, sigma_obj0_m, sigma_obj_rate_mps),
                             age, getattr(c, "src", "pixel"), c.z_m))
    for c in coins:
        age = getattr(c, "age_ms", 0.0) or 0.0
        if age > max_obj_age_s * 1000.0:
            continue
        objs.append(SceneObj("coin", c.x_m - origin, c.t_miss_s,
                             float(c.observed_count),
                             _sigma(age, sigma_obj0_m, sigma_obj_rate_mps),
                             age, getattr(c, "src", "group")))
    stale = corridor_age_ms is not None \
        and corridor_age_ms > corridor_age_max_s * 1000.0
    return Scene(SCENE_SCHEMA, fid, ego_m, road, tuple(objs), lane_w_m,
                 frame_age_ms, corridor_age_ms, stale)


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
            "fd": round(self.frame_delta_m, 3),
            "unc": self.corridor_uncertain,
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


def _envelope(scene: Scene, near_z_m: float, wall_margin_m: float,
              widen_m: float) -> tuple[float, float] | None:
    lo, hi = -float("inf"), float("inf")
    any_band = False
    for b in scene.road.bands:
        if b.right <= 0.0 or b.z0 > near_z_m or b.z1 < 0.0:
            continue
        if b.left is None or b.right is None:
            continue
        any_band = True
        lo = max(lo, b.left + wall_margin_m - widen_m)
        hi = min(hi, b.right - wall_margin_m + widen_m)
    if not any_band or hi < lo:
        return None
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
    """候选时稳（替代旧目标迟滞）：分层立即切，同层 margin+dwell。"""

    def __init__(self, switch_margin: float, dwell_s: float,
                 epsilon: float):
        self.margin = switch_margin
        self.dwell = dwell_s
        self.eps = max(epsilon, 1e-9)
        self._idx: int | None = None
        self._key: tuple | None = None
        self._streak = 0.0

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

    def choose(self, cands: list[_Cand], dt: float) -> _Cand:
        best = cands[0]
        inc = next((c for c in cands if c.idx == self._idx and c.feasible),
                   None)
        if inc is None:
            self._idx, self._key, self._streak = best.idx, self._q(best.key), 0.0
            return best
        if best.layer < inc.layer:
            self._idx, self._key, self._streak = best.idx, self._q(best.key), 0.0
            return best
        kb, ki = self._q(best.key), self._q(inc.key)
        if best.layer == inc.layer and kb < ki and \
                self._gap(kb, ki) > self.margin:
            self._streak += dt
            if self._streak >= self.dwell:
                self._idx, self._key, self._streak = best.idx, kb, 0.0
                return best
        else:
            self._streak = 0.0
        self._key = ki
        return inc


def plan_motion(scene: Scene, ego: EgoState, motion, selection,
                plant, gate: PlanGate | None = None) -> MotionPlan:
    """场景 → 候选轨迹 → 四层字典序 → PlanGate。纯函数（gate 除外）。"""
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
    env = _envelope(scene, motion.near_z_m,
                    selection.wall_margin_lane * lane_w,
                    abs(frame_delta) if uncertain else 0.0)
    if env is None:
        uncertain = True
    ego_half = selection.ego_half_lane * lane_w
    car_half = selection.car_half_lane * lane_w
    graze = motion.graze_gap_lane * lane_w
    coin_half = motion.coin_collect_half_lane * lane_w
    reward_half = ego_half + car_half
    center0 = scene.road.center_at(0.0) or 0.0

    def sig(t: float) -> float:
        return motion.sigma_ego0_m + motion.sigma_ego_rate_mps * t

    shortened = False
    if sig(horizon) > motion.sigma_ego_max_m > 0:
        n = max(1, int((motion.sigma_ego_max_m - motion.sigma_ego0_m)
                       / max(motion.sigma_ego_rate_mps, 1e-9) / dt))
        horizon = n * dt
        shortened = True

    cars = [o for o in scene.objs if o.kind == "car"]
    rewards = [o for o in scene.objs if o.kind == "reward"]
    coins = [o for o in scene.objs if o.kind == "coin"]
    ts = [k * dt for k in range(n + 1)]
    us = list(motion.steer_levels) + [ego.steer_norm]
    cands: list[_Cand] = []
    for idx, u in enumerate(us):
        x, v, steer = scene.ego_lane, ego.v_lane_s, ego.steer_norm
        xs_lane = [x]
        for _ in range(n):
            x, v, steer = step(x, v, steer, u, dt, plant)
            xs_lane.append(x)
        xs_m = tuple(v * lane_w for v in xs_lane)

        def solve_clear(t: float) -> float:
            xx = _x_at(xs_m, t, dt)
            d = float("inf")
            for c in cars:
                if c.t_arrive_s is not None and c.t_arrive_s > horizon:
                    continue
                tc = 0.0 if c.t_arrive_s is None else c.t_arrive_s
                if c.t_arrive_s is None and (
                        (c.z_m is not None and c.z_m > motion.near_z_m)
                        or abs(c.x_m - scene.ego_x_m) > graze):
                    continue                    # 远车/无 z：不进碰撞约束
                if c.t_arrive_s is not None and abs(tc - t) > dt * 1.5:
                    continue
                d = min(d, abs(xx - c.x_m)
                        - (ego_half + car_half + c.sigma_m + sig(t)
                           + motion.collision_margin_m))
            return d

        corridor_ok = env is None or all(
            env[0] <= xx <= env[1] for xx in xs_m)
        min_clear = min((solve_clear(t) for t in ts), default=float("inf"))
        feasible = corridor_ok and min_clear >= 0.0

        reward_hits, reward_value = 0, 0.0
        for r in rewards:
            if r.t_arrive_s is None or r.t_arrive_s > horizon:
                continue
            xr = _x_at(xs_m, r.t_arrive_s, dt)
            tol = reward_half + r.sigma_m + sig(r.t_arrive_s)
            if abs(xr - r.x_m) <= tol:
                reward_hits += 1
                reward_value += max(0.0, tol - abs(xr - r.x_m))
        pass_count, pass_value = 0, 0.0
        for c in cars:
            tc = 0.0 if c.t_arrive_s is None else c.t_arrive_s
            if tc > horizon:
                continue
            d = abs(_x_at(xs_m, tc, dt) - c.x_m)
            if ego_half + car_half + c.sigma_m + sig(tc) <= d <= graze:
                pass_count += 1
                pass_value += graze - d
        coin_count, coin_error = 0.0, 0.0
        for c in coins:
            if c.t_arrive_s is None or c.t_arrive_s > horizon:
                continue
            xr = _x_at(xs_m, c.t_arrive_s, dt)
            err = abs(xr - c.x_m)
            tol = coin_half + c.sigma_m + sig(c.t_arrive_s)
            if err <= tol:
                coin_count += c.count
                coin_error += err
        if coin_count > 0:
            coin_error /= max(coin_count, 1e-9)
        center_dev = sum(abs(xx - center0) for xx in xs_m) / len(xs_m)
        effort = abs(u) * horizon
        if reward_hits:
            layer = 1
        elif pass_count:
            layer = 2
        elif coin_count:
            layer = 3
        else:
            layer = 4
        key = (not feasible, -reward_hits, -reward_value,
               -pass_count, -pass_value, -coin_count, coin_error,
               center_dev, effort, idx)
        i = max(1, min(n, int(round(motion.preview_s / dt))))
        cands.append(_Cand(
            idx, u, xs_m, xs_m[i], xs_lane[i], feasible,
            min_clear, reward_hits, reward_value, pass_count, pass_value,
            coin_count, coin_error, center_dev, effort, layer, key))

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
        valid = False
        corridor_all_bad = env is not None and not any(
            all(env[0] <= xx <= env[1] for xx in c.xs_m) for c in cands)
        if corridor_all_bad:
            chosen = min(cands, key=lambda c: abs(c.x_preview_m - scene.ego_x_m))
            reason = "plan:infeasible_corridor"
        else:
            chosen = max(cands, key=lambda c: c.min_clear_m)
            reason = "plan:infeasible_collision"
    return MotionPlan(
        PLAN_SCHEMA, valid, reason, chosen.x_preview_m,
        chosen.v_preview_lane * lane_w, chosen.u, chosen.layer, chosen.key,
        len(feasible), chosen.idx, tuple(c.xs_m for c in cands),
        (chosen.x_preview_m - frame_delta) / lane_w,
        chosen.v_preview_lane, frame_delta, uncertain, dt)
