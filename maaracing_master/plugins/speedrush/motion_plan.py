# -*- coding: utf-8 -*-
"""场景快照契约：决策层每拍组装一次，供轨迹评估与 trace 观测面消费。

坐标：**走廊中心在自车站为 0、右正、米**（与 `selection.RoadFrame` 同帧）。
对象只带评估必需的量（横位/到站时间/枚数/不确定度/年龄/来源/纵深），
不含像素、不含纵向速度。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.planner import heading_for_error, step
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
    """计划输入的自车状态：**朝向是状态，速度是它的推论**。"""

    x_lane: float                 # planner 道单位（右正）
    heading: float                # 相对车道的车头角（rad，右正）
    v_mps: float | None           # 在线比例（≈车速）；None=还没估出来
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
    psi_feedforward: float        # 选中的车头角指令（rad）
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
    corridor_viol_m: float = 0.0   # 被选候选的硬边界侵入（米，0=可行）
    corridor_soft_viol_m: float = 0.0  # 软边界侵入（米；可行候选也常 >0）

    def as_trace(self) -> dict:
        return {
            "valid": self.valid, "reason": self.reason,
            "x": round(self.x_preview_m, 3),
            "v": round(self.v_preview_mps, 3),
            "pc": round(self.psi_feedforward, 4),
            "layer": self.layer, "n_ok": self.n_feasible,
            "chosen": self.chosen, "dt": self.dt_s,
            "key": [round(v, 4) if isinstance(v, float) else v
                    for v in self.score_key],
            "val": round(self.value_total, 2),
            "cc": round(self.collision_cost, 2),
            "fd": round(self.frame_delta_m, 3),
            "unc": self.corridor_uncertain,
            "cv": round(self.corridor_viol_m, 3),
            "cvs": round(self.corridor_soft_viol_m, 3),
            "cand": [[round(v, 3) for v in c] for c in self.cand_x],
        }


@dataclass(frozen=True)
class _Cand:
    idx: int
    psi_cmd: float        # 候选的车头角指令（rad）
    xs_m: tuple[float, ...]
    x_preview_m: float
    v_preview_lane: float
    feasible: bool
    min_clear_m: float
    center_dev_m: float
    effort: float
    layer: int
    key: tuple
    value: float = 0.0
    collision_cost: float = 0.0
    corridor_viol_m: float = 0.0
    corridor_soft_viol_m: float = 0.0


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

    @staticmethod
    def _urgent(best: _Cand, inc: _Cand) -> bool:
        """立即切换的安全类判据：最优候选碰撞风险更低，或现任得分目标
        已失去（最优回中/换层接手）。新冒出的得分机会不算紧急。"""
        if best.collision_cost < inc.collision_cost - 1e-9:
            return True
        return inc.layer != 4 and best.layer == 4

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
        # 立即切换只给「安全类」：更少碰撞风险，或现任得分目标消失（回到
        # 回中/别的层）。新冒出来的得分机会（奖励/擦边/金币，价值 90~240
        # 都高于 immediate=100）不得单帧拽杆——2026-10-30 人工局里计划层
        # 擦边段 36 段有 25 段只活 1 拍、金币段中位 2 拍，杆值跟着一拍一
        # 换；检测闪断不是目标出现。得分类升级改走 dwell（0.2s）确认。
        urgent = self._urgent(best, inc)
        if kb[1] < ki[1] - self.immediate and urgent:
            self._idx, self._key, self._streak = best.idx, kb, 0.0
            self._last = best
            return best
        # 反向同一条：驻留只防「两个都该做」之间的抖动，不防现任自己失效。
        # 擦边车出窗后，现任满杆候选的价值掉到 0，但 key 里的超限罚仍挂着；
        # 旧逻辑只等「最佳更好」，于是继续按满杆 0.2~0.3s（20261027 局
        # 9.1~9.4s 空路左打、12.7~12.9s 目标在右却满左）。现任是得分目标
        # 而最优已回中（目标消失）/ 现任越限，本拍就让位。
        demoted = urgent and ki[1] > kb[1] + self.immediate
        if len(ki) > 2 and len(kb) > 2 \
                and isinstance(ki[2], (int, float)) \
                and isinstance(kb[2], (int, float)) \
                and ki[2] > kb[2] + 1e-6:
            demoted = True
        if demoted:
            self._idx, self._key, self._streak = best.idx, kb, 0.0
            self._last, self._miss_s = best, 0.0
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
                intent_lane: float) -> MotionPlan:
    """场景 + 决策意图 → 候选轨迹 → 可行性 → 选最贴意图且最省动作的一条。

    **去哪由决策层定**（``intent_lane``，planner 车道系，右正）。计划只回答
    「怎么去」：在车头角候选空间里滚同一套 ``step``，按可行性（硬边界/反应时
    内到站车）→ 安全成本（碰撞/有距无速）→ 离意图的偏差 → 动作量 选一条。
    计划不再有自己的机会价值（金币/奖励车/擦边/预置）——那是选择层的判断，
    两套价值会各自算出一个目标，再按「谁本拍有效」轮流打方向。并线承诺期
    也一样：决策的意图就是承诺道，计划只负责走过去。"""
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
    if abs(frame_delta) > motion.frame_delta_hold_m:
        # 场景/自车已不在同一横向参考系：按场景走廊判「贴墙」会把车按到
        # 幻影墙上。计划退回无效，交给 PD 用同一参考系的目标保持。
        return MotionPlan(
            PLAN_SCHEMA, False, "plan:frame_shift", scene.ego_x_m, 0.0, 0.0,
            4, (True,), 0, -1, (), ego.x_lane, 0.0, frame_delta, True)
    if ego.v_mps is None or ego.v_mps <= 1.0:
        # 没有车速就没有预测：候选轨迹全都一样（横移 = v·tanψ），比较也没有
        # 意义。旧实现拿常数兜底，等于假装知道车会怎么动；这里如实交回位置
        # 环（它不需要车速），等在线比例估出来再接管。
        return MotionPlan(
            PLAN_SCHEMA, False, "plan:no_speed", scene.ego_x_m, 0.0, 0.0,
            4, (True,), 0, -1, (), ego.x_lane, 0.0, frame_delta, True)
    widen = 0.0
    if uncertain:
        widen = min(abs(frame_delta), motion.uncertain_widen_max_m)
    ego_half = selection.ego_half_lane * lane_w
    # 软边界=想守的余量（wall_margin_lane）；硬边界=车身物理边缘+小间隙。
    # 只有硬边界越界才判不可行；软边界越界按米计代价，让"贴线但不撞"的
    # 候选还能被价值层使用，而不是整层全灭后选满杆。
    env = _envelope(scene, motion.near_z_m,
                    selection.wall_margin_lane * lane_w, widen)
    env_hard = _envelope(scene, motion.near_z_m,
                         ego_half + motion.corridor_hard_clearance_m, widen)
    if env is None:
        uncertain = True
    car_half = selection.car_half_lane * lane_w
    # 大车按真实宽度判（有距无速域与碰撞都用它自己的半宽）
    graze_max_w = motion.graze_max_width_m

    def _half(c: SceneObj) -> float:
        return c.half_w_m if c.half_w_m is not None else car_half

    def _large(c: SceneObj) -> bool:
        return c.half_w_m is not None and c.half_w_m * 2.0 > graze_max_w
    # 无到站时间的车只有在横向确实近（一车宽量级）时才当障碍——远处同帧的
    # 车不该被当成贴身静态墙（曾经的 0.75 道口径）。
    near_off_m = motion.near_untimed_lane * lane_w
    # 意图在场景帧的位置：决策给的是 planner 车道系，frame_delta 正是两帧的
    # 平移量（scene = planner*lane_w + frame_delta）。
    center0 = intent_lane * lane_w + frame_delta

    def sig(t: float) -> float:
        return motion.sigma_ego0_m + motion.sigma_ego_rate_mps * t

    shortened = False
    if sig(horizon) > motion.sigma_ego_max_m > 0:
        n = max(1, int((motion.sigma_ego_max_m - motion.sigma_ego0_m)
                       / max(motion.sigma_ego_rate_mps, 1e-9) / dt))
        horizon = n * dt
        shortened = True

    cars = [o for o in scene.objs if o.kind == "car"]
    ts = [k * dt for k in range(n + 1)]
    # 候选命令 = **绝对**车头角指令（rad）。档位不随当前朝向平移：偏移量档
    # 在车已经带着 0.05 rad 时给不出「反打到 −0.03」这一档，而回带刹车恰恰
    # 需要它（台架：冲过路心时所有可取候选都还在向右）。当前朝向单独一条
    # 候选，结构同旧实现的「绝对杆值档 + 当前杆值」。
    psi0 = ego.heading
    n_half = max(1, n // 2)

    # 有距无速域是否在场：在场时解除 L4 维护性动作上限（这是避让，不是追路心）
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

    def _h(x: float) -> float:
        return max(-plant.heading_max_rad, min(plant.heading_max_rad, x))

    seqs: list[tuple[float | None, tuple[float, ...] | None,
                     float | None]] = []
    for lv in motion.heading_levels:
        seqs.append((_h(lv), None, None))
    seqs.append((_h(psi0), None, None))                      # 保持当前朝向
    for lv in motion.heading_levels:
        if abs(lv) < motion.reverse_min_rad:
            continue
        c = _h(lv)
        seqs.append((c, (c,) * n_half + (0.0,) * (n - n_half), None))
    c = _h(psi0)
    seqs.append((c, (c,) * n_half + (0.0,) * (n - n_half), None))
    # 意图跟踪候选（2026-10-31 单一意图）：与执行层同一条律逐步重算车头角，
    # 目标是决策意图本身。开环档位在 1.3~2s 到站时相邻档横距差约 1m，计分带
    # 会被整段跳过，所以必须有一条闭环落点候选。候选下标追加在开环之后。
    seqs.append((None, None, center0 / lane_w))
    cands: list[_Cand] = []
    v_ego = ego.v_mps or 0.0
    v_lat_ego = (v_ego * math.tan(ego.heading) / lane_w) if v_ego > 0 else 0.0
    for idx, (cmd0, seq, target) in enumerate(seqs):
        x, psi = scene.ego_lane, ego.heading
        if target is not None:
            cmd0 = max(-plant.heading_max_rad,
                       min(plant.heading_max_rad,
                           heading_for_error(target - x, v_ego, plant)))
        cmd = cmd0
        xs_lane = [x]
        vs_lane = [(v_ego * math.tan(psi) / lane_w) if v_ego > 0 else 0.0]
        for k in range(n):
            if target is not None:
                # 目标位候选：与控制器同一条律（误差 → 期望横速 → 朝向）
                cmd = max(-plant.heading_max_rad,
                          min(plant.heading_max_rad,
                              heading_for_error(target - x, v_ego, plant)))
            elif seq is not None:
                cmd = seq[k]
            x, psi = step(x, psi, cmd, v_ego, dt, plant)
            xs_lane.append(x)
            vs_lane.append((v_ego * math.tan(psi) / lane_w)
                           if v_ego > 0 else 0.0)
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
                if not c.near or abs(c.x_m - scene.ego_x_m) > near_off_m:
                    continue
                clears.append(min((clearance(c, t) for t in ts),
                                  default=float("inf")))
            elif motion.collision_min_lead_s <= c.t_arrive_s <= horizon:
                clears.append(clearance(c, c.t_arrive_s))
        collision_cost = sum(
            motion.cost_collision + (-d) * motion.cost_collision_depth_m
            for d in clears if d < 0.0)
        min_clear = min(clears, default=float("inf"))
        # 走廊侵入深度（米，检查时域均值）：软=想守的余量，硬=物理边界。
        # 硬侵入 0=可行。可行集为空时硬侵入是逃生判据——布尔「在/不在」
        # 一旦全灭就退化成任意候选；软侵入只进价值，不再一票否决。
        soft_viol = 0.0
        hard_viol = 0.0
        chk = [xx for xx, tt in zip(xs_m, ts)
               if tt <= motion.corridor_check_s + 1e-9]
        if chk:
            n_chk = max(1, len(chk))
            if env is not None:
                soft_viol = sum(max(0.0, env[0] - xx, xx - env[1])
                                for xx in chk) / n_chk
            if env_hard is not None:
                hard_viol = sum(max(0.0, env_hard[0] - xx, xx - env_hard[1])
                                for xx in chk) / n_chk
        corridor_ok = hard_viol <= 0.0

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
            if cmd0 * toward > 0.0 or (v_lat_ego * toward > 0.0
                                       and cmd0 * toward >= 0.0):
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
        value = -(collision_cost + range_cost)
        soft_cost = soft_viol * motion.corridor_soft_cost_per_m
        # 中心目标带死区：带内不修正，只在带外惩罚超出量。带宽与选择层
        # 同一把尺子——车道内可偏 0.5−半宽−余量；motion.center_band_lane
        # 只作上限，不能再比物理余量宽（旧 0.3 道比可偏 0.23 道还宽）。
        band_m = min(motion.center_band_lane * lane_w,
                     max(0.0, (0.5 - selection.ego_half_lane
                               - selection.lane_clearance_lane) * lane_w))
        center_dev = sum(max(0.0, abs(xx - center0) - band_m)
                         for xx in xs_m) / len(xs_m)
        # 动作成本 = 车头角指令相对当前朝向的改动量：带内无修正时优先保持朝向。
        effort = abs(cmd0 - psi0) * horizon
        # 层只描述意图的性质：意图在中心带内=维护（4），要跨道=动作（2）。
        # 执行层用它决定维护性杆幅上限与并线承诺的控制权，不再代表"机会种类"。
        maintenance = abs(center0 - scene.ego_x_m) <= band_m
        layer = 4 if maintenance else 2
        # 维护时限制动作：不得用大车头角去追偏掉的路心。
        # 判的是**车头角本身**（它决定横速），不是相对当前朝向的改动量：
        # 车正在大角度回带时，唯一可取的动作恰恰是反打刹车——按改动量判会
        # 把它排除，只许继续朝外打。有距无速域在场时例外（那是避让）。
        overcap = 1e6 if (maintenance and not range_hazard_active
                          and abs(cmd0) > motion.center_heading_rad) else 0.0
        # key 顺序：可行 > 安全成本（碰撞/有距无速）> 维护杆幅 > 离意图偏差 >
        # 动作 > 软余量代价。安全成本在杆幅前——为避撞动用满杆是允许的；
        # 软余量代价在杆幅后——否则贴线 3 分的小差就能让满杆盖过受限候选。
        # 意图偏差在安全之后：安全优先，其次才是走到决策要的道。
        key = (not feasible, -value, overcap, center_dev, effort,
               soft_cost, idx)
        i = max(1, min(n, int(round(motion.preview_s / dt))))
        cands.append(_Cand(
            idx, cmd0, xs_m, xs_m[i], vs_lane[i], feasible,
            min_clear, center_dev, effort, layer, key,
            value, collision_cost, hard_viol, soft_viol))

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
        # 可行集为空 = 自车已压在**硬**边界内（贴墙行驶的日常状态，不是异常）。
        # 此时目标是「最快脱离侵入」，不是「位移最小」：后者会挑反向满舵去
        # 抵消逃离速度，把车钉在墙上（2026-10-23 局 192643_p2 fid 3653~3670，
        # 车在左缘而计划满舵左，连续 0.85 s 撞护栏）。
        # 但「最快」不等于「最大杆」：侵入差 1cm 的两个候选不该一个满杆一个
        # 轻打。把侵入按 corridor_escape_tol_m 分桶，同桶内按价值、再按动作
        # 选——平滑回带由这里保证。
        valid = False
        best_viol = min(c.corridor_viol_m for c in cands)
        tol = max(motion.corridor_escape_tol_m, 1e-6)
        near = [c for c in cands
                if c.corridor_viol_m <= best_viol + tol]
        chosen = min(near, key=lambda c: (
            int((c.corridor_viol_m - best_viol) / tol + 1e-9),
            -c.value, c.effort, c.idx))
        reason = "plan:infeasible_corridor"
    return MotionPlan(
        PLAN_SCHEMA, valid, reason, chosen.x_preview_m,
        chosen.v_preview_lane * lane_w, chosen.psi_cmd, chosen.layer,
        chosen.key,
        len(feasible), chosen.idx, tuple(c.xs_m for c in cands),
        (chosen.x_preview_m - frame_delta) / lane_w,
        chosen.v_preview_lane, frame_delta, uncertain, dt,
        chosen.value, chosen.collision_cost, chosen.corridor_viol_m,
        chosen.corridor_soft_viol_m)
