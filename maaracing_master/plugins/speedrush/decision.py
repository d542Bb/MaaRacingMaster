# -*- coding: utf-8 -*-
"""行为决策层（v1 = coin-only 保守基线；v2 = 超车候选并秤，阶段 C 设计稿 §二/§四）。

`mode.allow_overtake`（默认 false）关闸时行为与 v1 **逐拍相同**（兼容性红线，
test_overtake_gate_closed 上锁）；开闸后超车与金币两类候选同一分式形状比较
（期望分×时间折扣−横向代价），完成判据按 kind 分派——金币走需求/死区收敛路，
超车走 pass 事件落定路（移动目标永不到"正中间"，维护者口径第 4 条）。

**输入输出**：每 tick 吃 `WorldObservation`（+ tick 时长 + 可选规划层横向反馈），
吐 `DecisionOutput`（D1：只吃结构化契约；D3：reason/valid_until 齐备可回放）。
时钟用 tick 累计（`time.monotonic` 不进本模块——回放与实机同一确定性）。

**FSM**（§二矩阵；转移优先级在 `_priority_order` 注释处落实）：
CRUISE 选道 / CHANGE 移动 / ABORT_CHANGE 有界回稳 / CONSERVE 保持+禁变道 / FAULT 纯直行。
- 进保守态前必先经 ABORT_CHANGE 收尾（fatal 打断 CHANGE 时），CONSERVE **不强制回中**
  （回中只在边界余量对称时由规划层做——本层 v1 连 boundary 都不消费其数值）；
- CONSERVE 出口清空目标、迟滞、低通、冷却（§二重置面）；
- 停止/阶段结束归 module 层（engine.reset() 供接线）。

**v1 降级路径（如实声明，不是遗漏）**：
- 无规划反馈（`executed_lane=None`，step 6 才有真值）时 CHANGE 完成判据降级为
  "目标组 x 读数连续 ≥2 tick 进死区"，另有 `t_change_max_s` 超时兜底强制落位——
  审查 §二.2 反对的"裸目标 x_lane"在此只作为**无反馈下的代理 + 超时保险**双件用，
  有反馈时完成判据走反馈路（executed ≥ demand×(1−ε) 且目标在场）。
- 移动中改判走 §三加性切换门（更优 + margin 才换目标；换"去哪"不换"动不动"），
  "取消进行中的移动"仅由失联/越界/致命信号触发。
- 直道信号（boundary）v1 未接主循环，校验器只对**已可得**的三路信号（帧过旧/
  枯竭合取/距离序倒挂）作致命判定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from maaracing_master.plugins.speedrush.config import DecisionConfig
from maaracing_master.plugins.speedrush.traffic import (
    OUTCOME_PASS, CarView, PassEvent)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import Calib, load_calib

_SCHEMA = 2  # v2：DecisionOutput.reanchor_lane（planner 设计稿 §二，2026-09-22 用户裁定）
# 阶段 C step 2 **不再升 schema**：超车能力扩的是决策层内部（Scored/选择/完成判据分派）
# 与入参通道（traffic= 关键字，默认 None 旧调用零扰动），DecisionOutput 出口契约不变。
_EPS = 1e-9

KIND_CAND_COIN = "coin"
KIND_CAND_OVERTAKE = "overtake"


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


@dataclass(frozen=True)
class Scored:
    """一次评分的完整依据（reason 码与迟滞比较都要它）。

    阶段 C step 2：候选泛化为两类——coin（group=CoinGroup）与 overtake
    （car=CarView，group=None）；`kind` 判别，两类的 score 同一分式形状产出
    （期望分×时间折扣−横向代价），min_score/switch_margin 的绝对分语义不变。"""

    group: CoinGroup | None
    score: float
    t_miss_s: float
    kind: str = KIND_CAND_COIN
    car: CarView | None = None

    @property
    def cand_id(self) -> int:
        """跨类统一的候选标识（coin=组号 / overtake=track_id，两 id 空间由 kind 分隔）。"""
        if self.kind == KIND_CAND_COIN:
            return self.group.group_id
        return self.car.id


class Scorer:
    """§三 评分（coin-only）：收益 × 时间折扣 − 横向位移代价。

    偏序纪律（验收 3）：同 conf 下枚数单调、同枚数下越近越高、越偏越贵；
    存续组（conf_min=0）自动被折扣到 ≤ 代价被拒——不虚增。
    """

    def __init__(self, cfg: DecisionConfig, cal: Calib):
        self.c = cfg.scoring
        self.ov = cfg.overtake
        self.cal = cal
        self.hz = cfg.control.frame_rate_hz
        # P̂_limit 在计划贴窗 d̂_min=d_hold 处取值（step 2 计划保证值暂替，
        # 真实 plant 前推留 planner §十一）：对全体候选是常数，排序由 t/偏移决定。
        self._p_hold = 1.0 / (1.0 + math.exp(
            (cfg.overtake.d_hold_lane - cfg.overtake.p_center_lane)
            / cfg.overtake.p_scale_lane))
        self._e_overtake = self.ov.value_base + self._p_hold * self.ov.value_limit

    def score_car(self, view: CarView) -> Scored | None:
        """超车候选评分（阶段 C §二）：E(超车+极限期望)×时间折扣 − 内贴横向代价。

        候选带/接近方向自卫见 §四：本车道车（|x|<band_lo）与掠过中（<下沿）不作候选；
        d̂_min<窗下沿的"更贴"不奖励——窗中心是唯一目标（硬地板语义，设计稿 §〇.1）。"""
        ax = abs(view.x_lane)
        if not (self.ov.lane_band_lo <= ax < self.ov.lane_band_hi) \
                or view.rel_approach <= _EPS:
            return None
        rate = view.rel_approach * self.hz          # px/s（rel_approach 为 px/tick）
        t_meet = max(0.0, self.cal.v_ego - view.cy) / rate
        life = math.exp(-t_meet / self.c.life_tau_s)
        goal = self.hug_goal(view.x_lane)
        score = self._e_overtake * life \
            - self.c.shift_cost_per_lane * abs(goal)
        return Scored(group=None, score=score, t_miss_s=t_meet,
                      kind=KIND_CAND_OVERTAKE, car=view)

    def hug_goal(self, x_lane: float) -> float:
        """内贴目标（§四裁定）：σ 在选中拍锁定，此后目标只跟车不翻边。"""
        sigma = 1.0 if x_lane >= 0 else -1.0
        return x_lane - sigma * self.ov.d_hold_lane

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
        value = self.c.coin_value_per_unit * g.observed_count * conf_factor
        rate = self._approach_rate_px_s(g, obs)
        t_miss = math.inf if rate <= _EPS else max(0.0, self.cal.v_ego - g.cy_max) / rate
        life = math.exp(-t_miss / self.c.life_tau_s) if math.isfinite(t_miss) else 0.0
        score = value * life - self.c.shift_cost_per_lane * abs(g.x_center)
        return Scored(group=g, score=score, t_miss_s=t_miss)

    def _approach_rate_px_s(self, g: CoinGroup, obs: WorldObservation) -> float:
        by_id = {t.id: t for t in obs.targets}
        rs = [by_id[i].rel_approach for i in g.member_ids if i in by_id]
        if not rs:
            return 0.0
        return (sum(rs) / len(rs)) * self.hz


class DecisionEngine:
    """FSM + 迟滞（§二/§三/§五）。单线程 owner 使用（与 _log_grp 同一线程纪律）。"""

    def __init__(self, cfg: DecisionConfig, cal: Calib | None = None):
        self.cfg = cfg
        self.cal = cal if cal is not None else load_calib()
        self.watch = ValidationWatch(cfg)
        self.scorer = Scorer(cfg, self.cal)
        self.reset()

    def reset(self) -> None:
        """阶段切换/保守态出口/接线重入（重置面见 §二：全清，id 语义归上游）。"""
        self.watch.reset()
        self._state = DecisionState.CRUISE
        self._target_gid: int | None = None
        self._target_kind: str | None = None
        self._done_pass_ids: set[int] = set()   # 本阶段已落定 pass 的车（防重选中）；
        # track id 每阶段由新 Tracker 重卷，阶段边界随 reset 清空，语义自洽
        self._cur_score = -math.inf
        self._demand_lane = 0.0
        self._x_smooth = 0.0
        self._cool_t = 0.0
        self._change_t = 0.0
        self._conserve_t = 0.0
        self._ok_t = 0.0
        self._deadzone_ticks = 0
        self._abort_next = DecisionState.CRUISE
        self._fault = False
        self._last_report_ok = True
        self._reanchor_pending: float | None = None

    # ---------- 主入口 ----------

    def update(self, obs: WorldObservation, dt_s: float,
               executed_lane: float | None = None,
               traffic: tuple[tuple[CarView, ...],
                              tuple[PassEvent, ...]] | None = None) -> DecisionOutput:
        """traffic=(在途车辆视图, 本拍落定事件)（traffic.TrafficObserver 直供）。

        缺省 None=无车流观测，行为与 coin-only v1 **逐拍相同**（V0/V1 兼容性红线）。
        逐拍视图放实例通道（单线程 owner 纪律，与 _log_grp 同族）——helper 签名不扩散。"""
        self._views, self._events = traffic if traffic is not None else ((), ())
        # 优先级（§二矩阵）：致命信号 > ABORT/收敛/超时 > 选择。本 tick 先处理转移，
        # 再按落定的状态发输出。
        report = self.watch.check(obs, dt_s)
        # 恢复计数以"上一报告非致命"为健康面之一（保守/故障出口读它，不再重算）
        self._last_report_ok = report.fatal is None
        if self._cool_t > 0:
            self._cool_t = max(0.0, self._cool_t - dt_s)
        reason = "hold"

        if report.fatal is not None:
            # D2：致命信号一票否决——有移动计划先有界回稳，无计划直进保守态
            if self._state in (DecisionState.CHANGE, DecisionState.ABORT_CHANGE):
                self._enter_abort(f"validate_fail:{report.fatal}")
                self._abort_next = DecisionState.CONSERVE
            else:
                self._enter_conserve()
            reason = f"validate_fail:{report.fatal}"
        elif self._state is DecisionState.FAULT:
            reason = self._tick_fault(obs, dt_s)
        elif self._state is DecisionState.CONSERVE:
            reason = self._tick_conserve(dt_s, obs)
        elif self._state is DecisionState.ABORT_CHANGE:
            reason = self._tick_abort(dt_s, executed_lane)
        elif self._state is DecisionState.CHANGE:
            reason = self._tick_change(obs, dt_s, executed_lane)
        else:  # CRUISE
            reason = self._tick_cruise(obs)

        # 输出目标：保持/移动路都指向当前组的横向中心；无目标回落自车道 0
        goal = self._current_goal(obs)
        alpha = 1.0 - math.exp(-dt_s / self.cfg.hysteresis.tau_filter_s)
        self._x_smooth += alpha * (goal - self._x_smooth)
        move_allowed = self._state in (DecisionState.CRUISE, DecisionState.CHANGE)
        if not self.cfg.mode.allow_all_moves:
            move_allowed = False
        reanchor = self._reanchor_pending
        self._reanchor_pending = None      # 一次性：只在完成拍携带
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state, target_id=self._target_gid,
            x_target=self._x_smooth if self._state is not DecisionState.CONSERVE else None,
            move_allowed=move_allowed, reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks(),
            reanchor_lane=reanchor)

    # ---------- 状态处理 ----------

    def _tick_cruise(self, obs: WorldObservation) -> str:
        cand = self._select(obs, current=None)
        if cand is None:
            self._target_gid = None
            self._target_kind = None
            return "hold:no_candidate" if not self._cool_t else "hold:cooling"
        if cand.kind == KIND_CAND_COIN \
                and cand.group.validity_until_fid <= obs.frame_id:
            return "hold:no_candidate"
        self._target_gid = cand.cand_id
        self._target_kind = cand.kind
        self._cur_score = cand.score
        # 有符号需求（planner §二：反向不得过门）；超车候选的需求=选中拍内贴目标，
        # 但超车完成走 pass 事件不走 demand 门（§四换轴），demand 只服务回执/trace
        self._demand_lane = (cand.group.x_center if cand.kind == KIND_CAND_COIN
                             else self.scorer.hug_goal(cand.car.x_lane))
        self._change_t = 0.0
        self._deadzone_ticks = 0
        self._state = DecisionState.CHANGE
        return "select:score_win"

    def _tick_change(self, obs: WorldObservation, dt_s: float,
                     executed: float | None) -> str:
        if self._target_kind == KIND_CAND_OVERTAKE:
            return self._tick_change_overtake(obs, dt_s)
        self._change_t += dt_s
        g = self._group_of(obs)
        if g is None or g.validity_until_fid <= obs.frame_id:
            # 目标失联过宽限（组级）：取消计划，有界回稳（§二消失语义）
            self._enter_abort("target_lost")
            return "cancel:target_lost"
        # 移动中改判（§三加性切换门生效处）：更优组超过 当前分+margin 才换目标，
        # 换的是"去哪"（x_goal/需求重捕），不是重新起变道——迟滞防的就是拉扯。
        better = self._select(obs, current=self._cur_score)
        if better is not None and \
                (better.kind, better.cand_id) != (KIND_CAND_COIN, self._target_gid):
            self._target_gid = better.cand_id
            self._target_kind = better.kind
            self._cur_score = better.score
            self._demand_lane = (better.group.x_center if better.kind == KIND_CAND_COIN
                                 else self.scorer.hug_goal(better.car.x_lane))
            self._deadzone_ticks = 0
            return "switch:score_win"
        if executed is not None:
            # 反馈路完成判据（有符号）：executed 与需求同向且幅度吃掉需求×(1−ε) 且目标在场
            # ——反向移动不得借绝对值过门（planner 设计稿 §二，2026-09-22 用户裁定）
            if self._demand_consumed(executed):
                return self._finish_change("converged:feedback", reanchor=g.x_center)
            # 越界复核（对当前目标）：归一发散现形 → 取消
            if abs(g.x_center) + g.x_span >= self.cfg.validate.x_lane_abs_max:
                self._enter_abort("x_bound")
                return "cancel:x_bound"
            return "moving:feedback"
        # v1 无反馈降级：目标 x 读数连续 2 tick 进死区视为收敛，超时强制落位
        if abs(g.x_center) <= self.cfg.hysteresis.dead_zone_lane:
            self._deadzone_ticks += 1
            if self._deadzone_ticks >= 2:
                return self._finish_change("converged:proxy", reanchor=g.x_center)
        else:
            self._deadzone_ticks = 0
        if self._change_t >= self.cfg.control.t_change_max_s:
            return self._finish_change("change_timeout", reanchor=g.x_center)
        return "moving:proxy"

    def _tick_change_overtake(self, obs: WorldObservation, dt_s: float) -> str:
        """超车计划路（阶段 C §三/§四）：完成=pass 事件落定（车已出画），
        失联/lost/ghost=ABORT 有界回稳；t_pass_max 兜底（车滞留不落的异常场）。
        不走 demand 收敛与死区代理——移动目标永远"走不到正中间"（维护者口径第 4 条）。"""
        self._change_t += dt_s
        v = next((x for x in self._views if x.id == self._target_gid), None)
        if v is None:
            ev = next((e for e in self._events if e.track_id == self._target_gid), None)
            if ev is not None and ev.outcome == OUTCOME_PASS:
                self._done_pass_ids.add(ev.track_id)
                # reanchor=None：超车完成拍没有"目标读数"可锚（车已出画）——
                # executed_lane 继续积分，锚点面等 C7/planner §十一 的连续观测
                return self._finish_change("overtake_pass")
            self._enter_abort("target_lost")
            return "cancel:overtake_lost"
        better = self._select(obs, current=self._cur_score)
        if better is not None and \
                (better.kind, better.cand_id) != (KIND_CAND_OVERTAKE, self._target_gid):
            self._target_gid = better.cand_id
            self._target_kind = better.kind
            self._cur_score = better.score
            self._demand_lane = (better.group.x_center if better.kind == KIND_CAND_COIN
                                 else self.scorer.hug_goal(better.car.x_lane))
            self._deadzone_ticks = 0
            return "switch:score_win"
        if self._change_t >= self.cfg.overtake.t_pass_max_s:
            return self._finish_change("pass_timeout")
        return "moving:overtake"

    def _tick_abort(self, dt_s: float, executed: float | None) -> str:
        # 有界完成：无反馈 1 tick 即认为回稳发出（v1）；有反馈等横向速率归零信号，
        # 超时同样强制落位（回稳不能变成第二次变道）。
        self._change_t += dt_s
        if executed is None or self._change_t >= self.cfg.control.t_change_max_s:
            # 有界回稳落定：按发起方指定去向（取消→CRUISE+冷却；校验打断→CONSERVE）
            if self._abort_next is DecisionState.CONSERVE:
                self._abort_next = DecisionState.CRUISE   # 消费掉一次性去向
                self._enter_conserve()
                return "abort:settled_conserve"
            self._state = DecisionState.CRUISE
            self._target_gid = None
            self._target_kind = None
            self._cool_t = self.cfg.hysteresis.t_cool_s
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

    def _select(self, obs: WorldObservation, current):
        """候选=金币组（+ allow_overtake 时的超车机会）；门=最低分 + 切换加性阈 +
        时机成立（§三公式；阶段 C §二：两类分数同分式形状，直接同秤比较）。"""
        if self._cool_t > 0 or not self.cfg.mode.allow_all_moves:
            return None
        best: Scored | None = None
        for g in obs.coin_groups:
            s = self.scorer.score(g, obs)
            if s is None or s.score < self.cfg.hysteresis.min_score:
                continue
            # 时机：变道耗时 + 响应延迟 + 余量 < 错过时间
            need = self.cfg.timing.lane_change_duration_s(g.x_center)
            if need + self.cfg.timing.tau_resp_s + self.cfg.timing.margin_s >= s.t_miss_s:
                continue
            if best is None or s.score > best.score:
                best = s
        if self.cfg.mode.allow_overtake:
            for v in self._views:
                if v.id in self._done_pass_ids:
                    continue
                s = self.scorer.score_car(v)
                if s is None or s.score < self.cfg.hysteresis.min_score:
                    continue
                # 超车时机：车已在掠过带（t_meet≤响应延迟）不追——内贴已来不及
                if s.t_miss_s <= self.cfg.timing.tau_resp_s:
                    continue
                if best is None or s.score > best.score:
                    best = s
        if best is None:
            return None
        if current is not None and best.score < current + self.cfg.hysteresis.switch_margin:
            return None
        return best

    def _degraded(self, obs: WorldObservation) -> bool:
        """恢复判定：与致命信号同一合取的"健康面"——alive+fresh+presence 或过渡。"""
        h = obs.health
        return not (h.perception_alive and h.frame_fresh)

    def _enter_abort(self, why: str) -> None:
        self._state = DecisionState.ABORT_CHANGE
        self._change_t = 0.0
        self._abort_reason = why

    def _enter_conserve(self) -> None:
        self._state = DecisionState.CONSERVE
        self._conserve_t = 0.0
        self._ok_t = 0.0
        self._target_gid = None
        self._target_kind = None
        self._cur_score = -math.inf

    def _demand_consumed(self, executed: float) -> bool:
        """有符号完成判据：executed 与需求同向、幅度吃掉 |demand|×(1−ε) 减死区。
        需求≈0（原地起变道）时退化为 |executed| ≤ 死区。"""
        dz = self.cfg.hysteresis.dead_zone_lane
        d = self._demand_lane
        if abs(d) <= dz:
            return abs(executed) <= dz
        return executed * d > 0 and abs(executed) >= abs(d) * (1 - 1e-6) - dz

    def _finish_change(self, why: str, reanchor: float | None = None) -> str:
        self._state = DecisionState.CRUISE
        self._cool_t = self.cfg.hysteresis.t_cool_s   # 完成也要冷却（§三：三收尾同权）
        self._target_gid = None
        self._target_kind = None
        self._cur_score = -math.inf
        self._reanchor_pending = reanchor   # 完成拍携带有符号目标读数（契约 v2）
        return f"done:{why}"

    def _group_of(self, obs: WorldObservation) -> CoinGroup | None:
        if self._target_gid is None:
            return None
        for g in obs.coin_groups:
            if g.group_id == self._target_gid:
                return g
        return None

    def _current_goal(self, obs: WorldObservation) -> float:
        if self._state in (DecisionState.CRUISE, DecisionState.CHANGE):
            if self._target_kind == KIND_CAND_OVERTAKE:
                v = next((x for x in self._views if x.id == self._target_gid), None)
                if v is not None:
                    return self.scorer.hug_goal(v.x_lane)   # 时变轨迹（§四内贴）
                if self._state is DecisionState.CHANGE:
                    return self._x_smooth                    # 落定拍前不外插假读数
                return 0.0
            g = self._group_of(obs)
            if g is not None and not math.isnan(g.x_center):
                return g.x_center
            # 组转存续态（本帧无成员观测，x_center=nan）：CHANGE 中保持最后 goal
            # （= 当前平滑值），不外插 nan——回放抓到 nan 污染 x_smooth 的缺陷。
            if self._state is DecisionState.CHANGE:
                return self._x_smooth
        return 0.0

    def _validity_ticks(self) -> int:
        return max(2, int(round(0.1 * self.cfg.control.frame_rate_hz)))
