# -*- coding: utf-8 -*-
"""行为决策层（2026-10-18 走廊重建：四层字典序）。

**它是什么**：每 tick 吃 `WorldObservation`（+ tick 时长 + 执行回执 + 车流 +
走廊），把目标换算进选择层坐标，交给 `selection.select` 做严格分层：

    奖励车（撞） > 普通车（擦边） > 金币（落线） > 走廊中心带（带内不修正）

层与层之间不求和；主目标之后最多插 3 个 3 秒路点，把顺路的金币和必须错开
的车带上。输出过 slew 限速后交给 planner。

**已删除**（2026-10-18）：走廊收益场加性打分、轨迹候选集与采样器否决、
栅格绝对否决、场目标迟滞三件套、承诺制/目标锁定/legacy 目标链、车道号
成员投票。它们不是被调参压住，而是被「分层选择 + 中心带」取代。

**FSM**：CRUISE 选择跟场 / ABORT 有界回稳（fatal 信号触发）/
CONSERVE 保持+禁变道 / FAULT 纯直行。

**输入输出**：每 tick 吐 `DecisionOutput`（reason/valid_until 齐备可回放）。
时钟用 tick 累计（`time.monotonic` 不进本模块——回放与实机同一确定性）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from maaracing_master.plugins.speedrush.config import DecisionConfig
from maaracing_master.plugins.speedrush.corridor import Corridor
from maaracing_master.plugins.speedrush.depth_geo import CAM_Z_DATUM_M
from maaracing_master.plugins.speedrush.motion_plan import build_scene
from maaracing_master.plugins.speedrush.selection import (
    BonusSource, CarSource, CoinSource, LayeredSelector, Obj, RoadBand,
    RoadFrame, SelectionParams, SelectionReport)
from maaracing_master.plugins.speedrush.traffic import CarView, PassEvent
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_BONUS, Calib, load_calib, t_meet_car_s, x_lane_of)

_SCHEMA = 5  # v5：删除 move_allowed（无消费者）；v4=maneuver 权限位
_EPS = 1e-9


@dataclass(frozen=True)
class Scored:
    """一次金币候选评定（原 plan.py 住户，选择域退役随迁本模块；场只消费
    t_miss_s，score 字段为审计面）。"""

    group: "CoinGroup | None"
    score: float
    t_miss_s: float
    kind: str = "coin"
    car: "CarView | None" = None


@dataclass(frozen=True)
class ValidationReport:
    """校验层输出（致命 = 一票否决，D2）。reasons 只装非致命注记。"""

    fatal: str | None
    reasons: tuple[str, ...] = ()


class ValidationWatch:
    """§四 致命信号器：只做信号与合取，不做状态转移。"""

    def __init__(self, cfg: DecisionConfig):
        self.v = cfg.validate
        self._empty_t = 0.0
        self._last_cy: dict[int, tuple[int, int]] = {}   # id → (fid, cy) 真实观测

    def reset(self) -> None:
        self._empty_t = 0.0
        self._last_cy.clear()

    def check(self, obs: WorldObservation, dt_s: float) -> ValidationReport:
        h = obs.health
        # 信号·帧过旧：卡屏/断供下不许盲决策（过渡窗除外——过场本就无新帧）
        if not h.frame_fresh and not h.stage_transition:
            return ValidationReport("frame_stale")
        # 信号·目标流枯竭：五信号**合取**才计时（空旷路面/过渡/半死不活都不算）
        conj = (h.perception_alive and h.frame_fresh and h.geometry_valid
                and not h.stage_transition)
        if conj and not h.target_presence:
            self._empty_t += dt_s
            if self._empty_t >= self.v.t_empty_s:
                return ValidationReport("target_empty")
        else:
            self._empty_t = 0.0
        # 信号·距离序倒挂：同 id 真实观测间 cy 跳变超物理上限（按失联 tick 数放大）
        for t in obs.targets:
            if t.last_seen_fid != obs.frame_id:
                continue  # 保持输出不比（外推值天然"跳"）
            prev = self._last_cy.get(t.id)
            if prev is not None:
                span = max(obs.frame_id - prev[0], 1)
                if abs(t.cy - prev[1]) > self.v.cy_jump_max_px * span:
                    return ValidationReport("cy_jump")
            self._last_cy[t.id] = (obs.frame_id, t.cy)
        for g in obs.far_targets:
            if g.last_seen_fid == obs.frame_id:
                self._last_cy[g.id] = (obs.frame_id, g.cy)
        reasons: list[str] = []
        if obs.boundary is None:
            reasons.append("straight_check_inactive")  # v1 边界未接线：如实注记
        return ValidationReport(None, tuple(reasons))


class Scorer:
    """§三 评分（coin-only）：收益 × 时间折扣 − 横向位移代价。

    偏序纪律（验收 3）：同 conf 下枚数单调、同枚数下越近越高、越偏越贵；
    存续组（conf_min=0）自动被折扣到 ≤ 代价被拒——不虚增。
    """

    def __init__(self, cfg: DecisionConfig, cal: Calib):
        self.c = cfg.scoring
        self.cal = cal
        self.hz = cfg.control.frame_rate_hz
        # 采集帧率（Hz，fid 时基）：引擎逐拍从 obs.fid_rate_hz 喂入（单线程
        # owner 实例通道纪律，与 _views/_events 同族）；None=预热未达/旧供数，
        # car_t_meet_s 退回旧行率式（已知 TTC 高估方向的旧消费口径，行为不
        # 静默换式）
        self.fid_hz: float | None = None

    def car_t_meet_s(self, rel: float, cy: float) -> float:
        """车流到自车行秒数（地面流积分式，world_model.t_meet_car_s 单一
        公式真源；前置三件套①）。rel 单位 px/fid，换算秒吃 self.fid_hz；
        供数缺位（None）退回旧行率式——单位/时间基准契约检查属回放验收
        （计划 §2①），换式不把旧单位错误带进新式。"""
        if self.fid_hz is None:
            rate = rel * self.hz
            return max(0.0, self.cal.v_ego - cy) / rate if rate > _EPS \
                else math.inf
        return t_meet_car_s(rel, cy, self.cal, self.fid_hz)

    def metric_t(self, m, v) -> float | None:
        """米制到站时刻（分流语义见 MetricCarState docstring）：
        v_close>0 → z/v_close；v_close≤0 → None（掠过/远离=占用）；
        v_close None（锚新估计窗未开）→ 桥接值（闸 metric_bridge.enabled）
        → 像素回退（有视图才退得动）。原 lane_score 住户，20261015 退役随迁
        （走廊收益场接线同源消费）。"""
        if m.v_close_mps is not None:
            return m.t_meet_s
        if getattr(m, "bridged", False) and m.t_meet_s is not None:
            return m.t_meet_s
        if v is None:
            return None
        return self.car_t_meet_s(v.rel_approach, v.cy) \
            if v.rel_approach > 0 else None

    def pixel_instruments(self, v) -> tuple[float, float | None]:
        """像素仪器（回退路径）：连续横位 + 地面流 t_meet（pacing=非正 rel
        → None=占用）。不再有车道成员投票：x_lane 就是连续读数。"""
        return v.x_lane, (self.car_t_meet_s(v.rel_approach, v.cy)
                          if v.rel_approach > 0 else None)

    def score(self, g: CoinGroup, obs: WorldObservation) -> Scored | None:
        if g.conf_min <= _EPS:
            return None  # 存续组（本帧无成员观测）：不配得分
        if g.conf_min < self.c.conf_floor:
            return None  # 置信折扣直接归零的下界（低于地板不做低信决策）
        # 置信折扣分段线性：floor→hi 爬坡、hi 以上满权——实测正常检测值
        # （coin conf 中位 0.87，commit 8f59837）不该被罚；线性 ramp 到 1 的
        # 旧式把 0.87 打成 0.74，§七.3 基线证实这是"119/123 被门拦"的主因。
        if g.conf_min >= self.c.conf_hi:
            conf_factor = 1.0
        else:
            conf_factor = ((g.conf_min - self.c.conf_floor)
                           / (self.c.conf_hi - self.c.conf_floor))
        # 组下限（RULES 2026-10-14 定案）：观测 1~2 枚是残缺视图（距离/遮挡/置信
        # 门削剩的头部），价值按整组 ≥3 枚计，不按观测枚数折算——n=1 的组按 30
        # 分基值再被车道惩罚一压连选分门槛都过不了（20261014 实测 score 拒
        # 106~119 次/局，金币候选 ok 每局 1 拍）。残缺半空组按 90 计（组内无
        # 空/地标志，宁可低估）。
        value = (self.c.coin_value_per_unit
                 * max(g.observed_count, self.c.coin_group_min_count) * conf_factor)
        k = self._flow_k(obs)
        t_miss = math.inf if k is None else self._t_miss_ground_s(g.cy_max, k)
        life = math.exp(-t_miss / self.c.life_tau_s) if math.isfinite(t_miss) else 0.0
        score = value * life - self.c.shift_cost_per_lane * abs(g.x_center)
        return Scored(group=g, score=score, t_miss_s=t_miss)

    def _flow_k(self, obs: WorldObservation) -> float | None:
        """全场地面流系数（2026-10-02 换基）：静止世界里同一帧的所有地面目标
        共享行速率场 r(cy) = k·(cy−y_h)²——针孔投影下恒速前进的必然形状，
        不是近似选择。k 取全体健康 track（有速率测量、行号在归一适用域内）
        的中位：单个 coin track 的断链/换 id 只伤它自己的时间累积账，伤不了
        全场横截面这本账；磁暴提速时 k 逐帧重估自动跟变。本帧无任何健康
        测量 → None，调用方按无限远折扣（与逐轨断供同一安全默认）。"""
        hs = []
        for t in obs.targets:
            d = t.cy - self.cal.y_h
            if t.rel_approach > _EPS and d >= self.cal.min_denom:
                hs.append(t.rel_approach * self.hz / (d * d))
        if not hs:
            return None
        hs.sort()
        return hs[len(hs) // 2]

    def _t_miss_ground_s(self, cy: float, k: float) -> float:
        """地面目标行 cy → 到自车接地点行的恒速到站时间（流模型积分式）。

        线性式 (v_ego−cy)/r(cy) 会低估不到站时间的一半量级——行速率随行号
        二次增长，越近越快，剩余行程不是匀速的；对 1/r 沿行积分才是真时间。"""
        a = 1.0 / (cy - self.cal.y_h)
        b = 1.0 / (self.cal.v_ego - self.cal.y_h)
        return max(0.0, (a - b) / k)


class DecisionEngine:
    """FSM + 迟滞（§二/§三/§五）。单线程 owner 使用（与 _log_grp 同一线程纪律）。"""

    def __init__(self, cfg: DecisionConfig, cal: Calib | None = None):
        self.cfg = cfg
        self.cal = cal if cal is not None else load_calib()
        self.watch = ValidationWatch(cfg)
        self.scorer = Scorer(cfg, self.cal)
        # 四层选择器（2026-10-18 走廊重建）：唯一选择器。轨迹采样器、收益场
        # 加性打分、栅格否决已删除——它们不是被调参压住，而是被分层规则取代。
        s = cfg.selection
        self.selector = LayeredSelector(SelectionParams(
            lane_w_m=self.cal.lane_w_m,
            horizon_s=s.horizon_s, max_waypoints=s.max_waypoints,
            graze_gap_lane=s.graze_gap_lane,
            center_band_lane=s.center_band_lane,
            opportunity_tol_lane=s.opportunity_tol_lane,
            t_move_base_s=s.t_move_base_s,
            t_move_per_lane_s=s.t_move_per_lane_s,
            reaction_s=s.reaction_s, margin_s=s.margin_s,
            wall_margin_lane=s.wall_margin_lane,
            center_lookahead_m=s.center_lookahead_m,
            switch_dwell_s=s.switch_dwell_s,
            reward_tol_lane=s.reward_tol_lane,
            car_half_lane=s.car_half_lane,
            ego_half_lane=s.ego_half_lane))
        self._sel_report: SelectionReport | None = None
        self._sel_maneuver = False
        self._sel_objs: dict = {"reward": [], "car": [], "coin": []}
        self._sel_dbg: dict | None = None
        # 车米制状态实例通道（C 线；update 每拍刷新，reset 清——与 _views 同族）
        self._metric_cars: tuple = ()
        self._fid = 0
        self._fps: float | None = None
        self._scene = None
        self.reset()

    def reset(self) -> None:
        """阶段切换/保守态出口/接线重入（重置面：全清，id 语义归上游）。"""
        self.watch.reset()
        self._state = DecisionState.CRUISE
        self._boundary = None
        # 输出连续性：上一拍下发目标（None=首拍直通；CONSERVE/ABORT/FAULT 清空）
        self._target_prev: float | None = None
        # 可观测面：执行分支
        self._exec_branch = "follow"
        self._maneuver = False
        self._sel_report = None
        self._sel_maneuver = False
        self._sel_objs = {"reward": [], "car": [], "coin": []}
        self._sel_dbg = None
        self.selector.reset()
        self._conserve_t = 0.0
        self._ok_t = 0.0
        self._abort_next = DecisionState.CRUISE
        self._abort_ref: float | None = None   # ABORT 速率落稳参考（_enter_abort 重置）
        self._dt_last: float = 0.05            # 上一拍 dt（慢拍计时基；reset 兜底值）
        self._fault = False
        self._last_report_ok = True
        self._corridor: Corridor | None = None
        self._reach_goal: float | None = None
        self._bonus_src = 0                 # 上一拍奖励车源数（cr_bonus_src 观测面）
        self._scene = None

    # ---------- 主入口 ----------

    def update(self, obs: WorldObservation, dt_s: float,
               executed_lane: float | None = None,
               traffic: tuple[tuple[CarView, ...],
                              tuple[PassEvent, ...]] | None = None,
               road_offset: float | None = None,
               corridor: Corridor | None = None) -> DecisionOutput:
        """traffic=(在途车辆视图, 本拍落定事件)（traffic.TrafficObserver 直供）。

        缺省 None=无车流观测，行为与 coin-only v1 **逐拍相同**（V0/V1 兼容红线）。
        逐拍视图放实例通道（单线程 owner 纪律，与 _log_grp 同族）——helper 签名不扩散。

        ``road_offset``：保留在签名里兼容既有调用（本层不消费，路心由
        corridor 带给出）。``corridor``：同拍分带走廊，选择层唯一的空间证据；
        None=无路，持守当前横位。"""
        self._dt_last = dt_s
        self._fid = obs.frame_id
        self._fps = obs.fid_rate_hz
        self._views, self._events = traffic if traffic is not None else ((), ())
        # 车米制状态实例通道（C 线 2026-10-12，闸 metric_cars.enabled）：车源
        # 链消费（米制优先、像素回退）；闸关=空（回退路径）。
        self._metric_cars = obs.metric_cars if self.cfg.metric_cars.enabled else ()
        # 采集帧率逐拍喂入评分器（car_t_meet_s 的 px/fid→秒换算锚；None=
        # 预热未达，评分器自决退回旧口径）
        self.scorer.fid_hz = obs.fid_rate_hz
        self._boundary = obs.boundary
        self._corridor = corridor
        # 优先级：致命信号 > ABORT/保守/故障 > 选择层。本 tick 先处理转移，
        # 再按落定的状态发输出。
        report = self.watch.check(obs, dt_s)
        # 恢复计数以"上一报告非致命"为健康面之一（保守/故障出口读它，不再重算）
        self._last_report_ok = report.fatal is None
        reason = "hold"

        if report.fatal is not None:
            # D2：致命信号一票否决——先有界回稳，无移动直进保守态
            if self._state is DecisionState.ABORT:
                self._enter_abort(f"validate_fail:{report.fatal}")
                self._abort_next = DecisionState.CONSERVE
            else:
                self._enter_conserve()
            reason = f"validate_fail:{report.fatal}"
        elif self._state is DecisionState.FAULT:
            reason = self._tick_fault(obs, dt_s)
        elif self._state is DecisionState.CONSERVE:
            reason = self._tick_conserve(dt_s, obs)
        elif self._state is DecisionState.ABORT:
            reason = self._tick_abort(dt_s, executed_lane)
        else:  # CRUISE
            reason = self._tick_cruise(obs, executed_lane)

        if self._state is not DecisionState.CRUISE:
            # 非巡航态：选择层目标不生效，观测列如实清空（不把陈旧值当本拍）
            self._exec_branch = "hold"
            self._scene = None

        # 输出目标：选择层已给出本拍唯一目标；目标缺席时对齐自车当前位。
        # 输出再过 slew 限速，单拍层切换不直接变成打舵指令。
        goal = (self._reach_goal / self.cal.lane_w_m
                if self._reach_goal is not None else None)
        if self._state is not DecisionState.CRUISE:
            t_raw = executed_lane if executed_lane is not None else goal
            self._target_prev = None       # 保守/回稳/故障出口：恢复拍直通
        elif self._exec_branch == "hold":
            t_raw = goal if goal is not None else executed_lane
        else:
            t_raw = goal if goal is not None else executed_lane
        if t_raw is None:
            t_raw = 0.0
        slew = self.cfg.hysteresis.target_slew_lane_s * dt_s
        if self._target_prev is None or slew <= 0.0:
            t_out = t_raw
        else:
            t_out = self._target_prev + max(
                -slew, min(slew, t_raw - self._target_prev))
        if self._state is DecisionState.CRUISE:
            self._target_prev = t_out
        x_out = t_out
        self._maneuver = bool(self._state is DecisionState.CRUISE
                              and self._sel_maneuver)
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state,
            x_target=x_out if self._state is not DecisionState.CONSERVE else None,
            reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks(),
            maneuver=self._maneuver)

    @property
    def maneuver(self) -> bool:
        """上一拍机动权限位（trace/GUI 观测面）。"""
        return self._maneuver

    @property
    def goal(self) -> float | None:
        """上一拍输出用目标（路心系道）。"""
        return (None if self._reach_goal is None
                else self._reach_goal / self.cal.lane_w_m)

    @property
    def exec_branch(self) -> str:
        """上一拍执行分支：follow / hold。"""
        return self._exec_branch



    def _age_ms(self, last_fid: int | None) -> float:
        """帧号差 → 毫秒（帧率未预热时=0，不臆造年龄）。"""
        if last_fid is None or not self._fps or self._fps <= 0:
            return 0.0
        return max(0, self._fid - last_fid) / self._fps * 1000.0

    def _coin_sources(self, obs: WorldObservation,
                      executed_lane: float) -> list[CoinSource]:
        """币组 → 选择/收益场源（路心系米）。确认窗与置信地板同源。"""
        lane_w = self.cal.lane_w_m
        coins: list[CoinSource] = []
        for g in obs.coin_groups:
            if g.x_center != g.x_center:
                continue                        # 存续组 nan 心：不入账
            if self.cfg.corridor_reach.coin_confirm_ticks > 1 \
                    and obs.frame_id - g.first_seen_fid \
                    < self.cfg.corridor_reach.coin_confirm_ticks:
                continue                        # 确认窗内不进场（单帧闪烁自卫）
            s = self.scorer.score(g, obs)
            if s is None:
                continue                        # 置信地板（质量门语义一致）
            coins.append(CoinSource(
                x_m=(g.x_center + executed_lane) * lane_w,
                observed_count=g.observed_count, conf=g.conf_min,
                t_miss_s=s.t_miss_s,
                age_ms=self._age_ms(g.last_seen_fid)))
        return coins

    def _bonus_sources(self, obs: WorldObservation,
                       executed_lane: float) -> list[BonusSource]:
        """奖励车源（近场 + 远场粗源），闸关=空列表。"""
        bonuses: list[BonusSource] = []
        if self.cfg.corridor_reach.bonus_value:
            lane_w = self.cal.lane_w_m
            for b in obs.targets:
                if b.kind != KIND_BONUS:
                    continue
                if obs.frame_id - b.first_seen_fid < self.cfg.traffic.min_obs_ticks:
                    continue                    # track 年龄门槛：单帧噪声进不了评分
                if b.rel_approach <= 0.0:
                    continue                    # 不接近=永远撞不上
                if b.x_lane is None or b.x_lane != b.x_lane:
                    continue                    # 横位缺位/存续 nan：不入账
                t_miss = self.scorer.car_t_meet_s(b.rel_approach, b.cy)
                if not math.isfinite(t_miss):
                    continue                    # 到站仪器无解：不追
                bonuses.append(BonusSource(
                    x_m=(b.x_lane + executed_lane) * lane_w,
                    t_miss_s=t_miss, conf=b.conf,
                    age_ms=self._age_ms(b.last_seen_fid)))
            bonuses.extend(self._reach_bonus_far_sources(obs, executed_lane))
        self._bonus_src = len(bonuses)
        return bonuses

    def _reach_bonus_far_sources(self, obs: WorldObservation,
                                 executed_lane: float) -> list[BonusSource]:
        """远场奖励车粗源（R5 §10.1）：far_targets（横位缺位、弱读带）走
        x_lane_of 同一公式真源读粗横位，夹进走廊 band edges 并集（米→道，
        误差被路宽兜住），到站走 car_t_meet_s 同一仪器链，每源宽容差
        bonus_far_tol_m。门同式：年龄门槛/不接近不收/到站有限。"""
        lane_w = self.cal.lane_w_m
        edges: list[float] = []
        if self._corridor is not None:
            for band in self._corridor.bands:
                if band.left.x_m is not None:
                    edges.append(band.left.x_m)
                if band.right.x_m is not None:
                    edges.append(band.right.x_m)
        span = (min(edges) / lane_w, max(edges) / lane_w) if edges else None
        out: list[BonusSource] = []
        for b in obs.far_targets:
            if b.kind != KIND_BONUS:
                continue
            if obs.frame_id - b.first_seen_fid < self.cfg.traffic.min_obs_ticks:
                continue                        # track 年龄门槛：同近场源
            if b.rel_approach <= 0.0:
                continue                        # 不接近=永远撞不上
            d = b.cy - self.cal.y_h
            if d <= 0:
                continue                        # 地平线上：公式分母退化（构造端已弃，防御）
            # 粗横位吃框底边（cy+h/2=地面触点代理）：平台是抬升物体，框中心
            # 比触点更贴地平线，X/H 按 ground 平面投影会系统性放大横位；
            # 到站仍吃框心（与 rel_approach 同一 tracked 点，同近场源）
            x_coarse = x_lane_of(b.cx, b.cy + b.h // 2, self.cal)
            if not math.isfinite(x_coarse):
                continue                        # 粗读无解：不入账
            if span is not None:
                x_coarse = min(max(x_coarse, span[0]), span[1])   # 夹进走廊路缘
            t_miss = self.scorer.car_t_meet_s(b.rel_approach, b.cy)
            if not math.isfinite(t_miss):
                continue                        # 到站仪器无解：不追
            out.append(BonusSource(
                x_m=(x_coarse + executed_lane) * lane_w,
                t_miss_s=t_miss, conf=b.conf, src="far"))  # tol_m 缺省=bonus_tol_m（近远场同源）
        return out

    def _reach_car_sources(self, executed_lane: float) -> list[CarSource]:
        """车米制仪器链（与 lane_score.benefits 同一优先级与仪器：米制优先、
        像素回退、已过自车行不占道；路心系米）。出界车不排除——连续坐标下
        几何归属自然处理远车（lane_score 的出界丢弃是道格覆盖问题，本链没有）。"""
        lane_w = self.cal.lane_w_m
        off = executed_lane * lane_w
        by_id = {v.id: v for v in self._views}
        seen: set[int] = set()
        out: list[CarSource] = []

        def enter(x: float | None, tm: float | None,
                  t_ov: float | None = None,
                  bridged: bool = False,
                  z_m: float | None = None,
                  age_ms: float = 0.0, src: str = "pixel") -> None:
            if x is not None and x == x:
                out.append(CarSource(x_m=x + off, t_meet_s=tm,
                                     t_overtake_s=t_ov, bridged=bridged,
                                     z_m=z_m, age_ms=age_ms, src=src))

        for m in self._metric_cars:
            v = by_id.get(m.target_id)
            if v is not None and v.cy >= self.cal.v_ego:
                seen.add(m.target_id)           # 已过自车行：不占道
                continue
            bridged = getattr(m, "bridged", False)
            if m.z_m is not None:
                x = (m.x_m if m.x_m is not None
                     else (v.x_lane * lane_w
                           if v is not None else None))
                tm = self.scorer.metric_t(m, v)
                # R3 前车仪器（2026-10-16 设计稿 §2.1）：t_overtake 只吃米制
                # t_meet（z/v_close，单一公式真源 MetricCarState；v_close>0=
                # 被接近的前车——设计稿原文 v_close<0 系符号笔误，与 L0 契约
                # 及实机 trace 对账（符号一致率 70.8%、近距离车 87% 为正）均
                # v>0=接近，数值同为 z/|v_close|）。**不走 tm**（metric_t 在
                # 估计窗未开时回退像素，估计窗未开=弃权不追）；t≤0（z 已过
                # datum）/v_close 缺位/桥接段=None=仪器缺位弃权
                t_ov = m.t_meet_s if (m.t_meet_s is not None
                                      and m.t_meet_s > 0.0
                                      and not bridged) else None
            elif v is not None:
                x, tm = self.scorer.pixel_instruments(v)
                t_ov = None                     # 像素回退车无米制锚：旧错身项
                x = None if x is None else x * lane_w
                # 速度像素桥（闸 metric_bridge.enabled）：桥接 t 与像素同式
                # 真源，优先取桥接值（携带 bridged 标记供不确定度分权）
                if bridged and m.t_meet_s is not None:
                    tm = m.t_meet_s
            else:
                continue                        # 无视图的远轨必有锚
            seen.add(m.target_id)
            enter(x, tm, t_ov, bridged=bridged, z_m=m.z_m,
                  age_ms=(m.anchor_age_ms or 0.0), src="metric")
        for vid, v in by_id.items():
            if vid in seen or v.cy >= self.cal.v_ego:
                continue                        # 米制路径已入账，不双计
            x, tm = self.scorer.pixel_instruments(v)
            enter(None if x is None else x * lane_w, tm,
                  age_ms=(v.age_ticks / self._fps * 1000.0
                          if self._fps else 0.0))
        return out

    @property
    def bonus_src_count(self) -> int:
        """上一拍奖励车源数（近场+远场粗源之和；0=源为空——R5 漏斗观测面）。"""
        return self._bonus_src

    @property
    def metric_cars_snap(self) -> list[dict]:
        """上一拍车米制状态快照（观测面，trace mcars 列数据源；R3 归因面）：
        逐车横向位/纵向距/到站/接近速/锚龄——机会漏点（可达门 vs 碰撞判据）
        的离线复算输入。t_ov 沿 _reach_car_sources 同式（≤0/桥接段=仪器缺位
        弃权）。b/bwhy=速度像素桥标记与进入原因（闸 metric_bridge.enabled，
        默认关恒 False/None）。空表=metric_cars 关或无车。"""
        return [{"id": m.target_id, "src": m.source.value,
                 "x": None if m.x_m is None else round(m.x_m, 2),
                 "z": None if m.z_m is None else round(m.z_m, 2),
                 "t_ov": None if (m.t_meet_s is None or m.t_meet_s <= 0.0
                                  or getattr(m, "bridged", False))
                 else round(m.t_meet_s, 3),
                 "v": None if m.v_close_mps is None else round(m.v_close_mps, 2),
                 "age": None if m.anchor_age_ms is None
                 else round(m.anchor_age_ms, 1),
                 "b": bool(getattr(m, "bridged", False)),
                 "bwhy": getattr(m, "bridge_reason", None)}
                for m in self._metric_cars]

    def _tick_cruise(self, obs: WorldObservation,
                     executed: float | None) -> str:
        """CRUISE：四层选择器是唯一选择来源。"""
        return self._tick_cruise_layered(obs, executed)

    # ---------- 四层选择（2026-10-18 走廊重建） ----------

    def _road_frame(self, executed: float, fid: int):
        """走廊分带 → 选择层帧（中心在自车站为 0，米）+ 自车横位。

        origin 一次平移：把「自车系 + executed 平移」的走廊带、目标和自车
        都换到同一个以走廊中心为原点的坐标，避免下游再各自换算。返回
        (RoadFrame, ego_m, origin)；无双侧带=None。
        """
        if self._corridor is None:
            return None
        lane_w = self.cal.lane_w_m
        shift = executed * lane_w
        raw: list[tuple] = []
        for b in self._corridor.bands:
            if b.left.x_m is None or b.right.x_m is None:
                continue
            raw.append((b.z0, b.z1, b.left.x_m + shift, b.right.x_m + shift,
                        b.left, b.right))
        if not raw:
            return None

        def _at(z: float, pick) -> float:
            best = min(raw, key=lambda b: 0.0 if b[0] <= z <= b[1]
                       else min(abs(z - b[0]), abs(z - b[1])))
            if best[0] <= z <= best[1] and best[1] > best[0]:
                w = (z - best[0]) / (best[1] - best[0])
                return pick(best, 0.0) + (pick(best, 1.0) - pick(best, 0.0)) * w
            return pick(best, 0.0 if z < best[0] else 1.0)

        origin = _at(0.0, lambda b, k: 0.5 * (b[2] + b[3]))
        bands = []
        for z0, z1, l, r, lb, rb in raw:
            ages = [a for a in (self._age_ms(lb.last_fid),
                                self._age_ms(rb.last_fid)) if a is not None]
            bands.append(RoadBand(
                z0, z1, l - origin, r - origin,
                src=(lb.src if lb.src == rb.src else "mixed"),
                age_ms=(max(ages) if ages else None),
                conf=min(lb.conf, rb.conf)))
        bands = tuple(bands)
        left0 = _at(0.0, lambda b, k: b[2]) - origin
        right0 = _at(0.0, lambda b, k: b[3]) - origin
        width = (right0 - left0) if right0 > left0 else (
            self._corridor.width_lane * lane_w
            if self._corridor.width_lane else None)
        road = RoadFrame(bands, width, left0, right0, True)
        return road, shift - origin, origin

    def _tick_cruise_layered(self, obs: WorldObservation,
                             executed: float | None) -> str:
        """四层选择器接管 CRUISE：奖励车 > 擦边 > 金币 > 中心带。"""
        if executed is None or self._corridor is None:
            self._reach_goal = None
            self._exec_branch = "hold"
            self._scene = None
            self._sel_report = None
            self._sel_maneuver = False
            self._sel_objs = {"reward": [], "car": [], "coin": []}
            self._sel_dbg = None
            return "sel:hold:no_road"
        rf = self._road_frame(executed, obs.frame_id)
        if rf is None:
            self._reach_goal = None
            self._exec_branch = "hold"
            self._scene = None
            self._sel_report = None
            self._sel_maneuver = False
            self._sel_objs = {"reward": [], "car": [], "coin": []}
            self._sel_dbg = None
            return "sel:hold:no_road"
        road, ego_m, origin = rf
        reward_src = self._bonus_sources(obs, executed)
        car_src = self._reach_car_sources(executed)
        coin_src = self._coin_sources(obs, executed)
        rewards = [Obj("reward", b.x_m - origin, b.t_miss_s,
                       conf=b.conf)
                   for b in reward_src]
        cars = [Obj("car",
                    c.x_m - origin - (road.center_at(
                        c.z_m - CAM_Z_DATUM_M)
                        if c.z_m is not None else 0.0),
                    c.t_meet_s)
                for c in car_src]
        coins = [Obj("coin", c.x_m - origin, c.t_miss_s,
                     count=c.observed_count, conf=c.conf)
                 for c in coin_src]
        corr_ages = [b.age_ms for b in road.bands if b.age_ms is not None]
        lw = self.cal.lane_w_m
        # 带中心帧 → 规划器路心帧：两套估计常年差 0.2~4.5m，不统一会让
        # frame_delta 恒超阈值、走廊按差额加宽到失效。选择器仍吃旧帧。
        center_lane = self._corridor.center_lane
        frame_shift = 0.0 if center_lane is None else \
            (-center_lane * lw) - ego_m
        self._scene = build_scene(
            fid=obs.frame_id, ego_m=ego_m, road=road,
            rewards=reward_src, cars=car_src, coins=coin_src,
            executed_lane=executed, origin=origin, lane_w_m=self.cal.lane_w_m,
            frame_age_ms=obs.frame_age_ms,
            corridor_age_ms=(max(corr_ages) if corr_ages else None),
            max_obj_age_s=self.cfg.motion.max_obj_age_s,
            corridor_age_max_s=self.cfg.motion.corridor_age_max_s,
            sigma_obj0_m=self.cfg.motion.sigma_obj0_m,
            sigma_obj_rate_mps=self.cfg.motion.sigma_obj_rate_mps,
            cam_z_datum_m=CAM_Z_DATUM_M, frame_shift_m=frame_shift)
        rep = self.selector.update(road, rewards, cars, coins, ego_m,
                                   self._dt_last)
        self._sel_report = rep
        self._sel_maneuver = rep.maneuver

        self._sel_objs = {
            "reward": [(round(o.x / lw, 3),
                        None if o.t is None else round(o.t, 3))
                       for o in rewards],
            "car": [(round(o.x / lw, 3),
                     None if o.t is None else round(o.t, 3))
                    for o in cars],
            "coin": [(round(o.x / lw, 3),
                      None if o.t is None else round(o.t, 3))
                     for o in coins],
        }
        c0 = road.center_at(0.0)
        c8 = road.center_at(self.cfg.selection.center_lookahead_m)
        self._sel_dbg = {
            "ego": round(ego_m / lw, 3),
            "target": round(rep.target_x_m / lw, 3),
            "c0": None if c0 is None else round(c0 / lw, 3),
            "c8": None if c8 is None else round(c8 / lw, 3),
            "left": None if road.left_m is None else round(road.left_m / lw, 3),
            "right": None if road.right_m is None else round(road.right_m / lw, 3),
            "band": self.cfg.selection.center_band_lane,
            "look": self.cfg.selection.center_lookahead_m,
            "wp": [[round(w.t, 3), round(w.x / lw, 3), w.src]
                   for w in rep.waypoints],
            "rw": self._sel_objs["reward"],
            "car": self._sel_objs["car"],
            "coin": self._sel_objs["coin"],
        }
        self._reach_goal = rep.target_x_m
        if rep.layer == 0:
            self._exec_branch = "hold"
            return "sel:hold:no_road"
        self._exec_branch = "follow"
        return f"sel:{rep.why}"

    @property
    def scene(self):
        """上一拍场景快照（CRUISE+有路才有；None=无路/非巡航）。"""
        return self._scene

    @property
    def selection_report(self) -> SelectionReport | None:
        """上一拍四层选择结果（trace 观测面）。"""
        return self._sel_report

    @property
    def selection_objects(self) -> dict:
        """上一拍选择器看到的机会（道，右正；t 秒）——调试图渲染面。"""
        return self._sel_objs

    @property
    def selection_debug(self) -> dict | None:
        """上一拍选择帧快照（ego/中心/路点/机会）——trace sel_dbg 列。"""
        return self._sel_dbg


    def _tick_abort(self, dt_s: float, executed: float | None) -> str:
        # 有界完成：无反馈 1 tick 即认为回稳发出（v1）；有反馈等横向速率归零信号
        # （19:44 复盘补装——v1 注释写了这个信号但从未实现，丢目标一律白冻 2s），
        # 超时同样强制落位（回稳不能变成第二次变道）。
        self._change_t += dt_s
        if executed is None:
            settled = True
        else:
            v = (abs(executed - self._abort_ref) / dt_s
                 if self._abort_ref is not None and dt_s > 0 else None)
            self._abort_ref = executed
            settled = ((v is not None
                        and v <= self.cfg.control.abort_settle_v_lane_s)
                       or self._change_t >= self.cfg.control.t_change_max_s)
        if settled:
            # 有界回稳落定：按发起方指定去向（取消→CRUISE+冷却；校验打断→CONSERVE）
            if self._abort_next is DecisionState.CONSERVE:
                self._abort_next = DecisionState.CRUISE   # 消费掉一次性去向
                self._enter_conserve()
                return "abort:settled_conserve"
            self._state = DecisionState.CRUISE
            return "abort:settled"
        return "aborting"

    def _tick_conserve(self, dt_s: float, obs: WorldObservation) -> str:
        self._conserve_t += dt_s
        if self._conserve_t >= self.cfg.validate.t_conserve_max_s:
            self._state = DecisionState.FAULT
            self._fault = True
            self._ok_t = 0.0
            return "fault:conserve_max"
        # 恢复计数：合取健康面（非致命报告且观测不降级）连续满 T_recover → 出口全清
        if self._last_report_ok and not self._degraded(obs):
            self._ok_t += dt_s
            if self._ok_t >= self.cfg.validate.t_recover_s:
                self.reset()          # §二出口重置面：全清后冷启动
                return "conserve_recovered"
        else:
            self._ok_t = 0.0
        return "conserving"

    def _tick_fault(self, obs: WorldObservation, dt_s: float) -> str:
        # FAULT 出口：连续双 T_recover 确认恢复才降回（§二；reset 后冷启动回巡航）
        if self._last_report_ok and not self._degraded(obs):
            self._ok_t += dt_s
            if self._ok_t >= 2 * self.cfg.validate.t_recover_s:
                self.reset()
                return "fault_recovered"
        else:
            self._ok_t = 0.0
        return "fault"

    # ---------- 选择与辅助 ----------

    def _degraded(self, obs: WorldObservation) -> bool:
        """恢复判定：与致命信号同一合取的"健康面"——alive+fresh+presence 或过渡。"""
        h = obs.health
        return not (h.perception_alive and h.frame_fresh)

    def _enter_abort(self, why: str) -> None:
        self._state = DecisionState.ABORT
        self._change_t = 0.0
        self._abort_ref = None      # 速率参考从 ABORT 首拍重新起算

    def _enter_conserve(self) -> None:
        self._state = DecisionState.CONSERVE
        self._conserve_t = 0.0
        self._ok_t = 0.0

    def request_conserve(self, reason: str = "external") -> None:
        """外部安全回执（motion 计划连续不可行）：进入既有 CONSERVE。"""
        self._enter_conserve()

    def _validity_ticks(self) -> int:
        return max(2, int(round(0.1 * self.cfg.control.frame_rate_hz)))
