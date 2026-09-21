# -*- coding: utf-8 -*-
"""行为决策层（v1 = coin-only 保守基线；设计稿 v2 §二/§三/§五）。

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
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import Calib, load_calib

_SCHEMA = 1
_EPS = 1e-9


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
    """一次评分的完整依据（reason 码与迟滞比较都要它）。"""

    group: CoinGroup
    score: float
    t_miss_s: float


class Scorer:
    """§三 评分（coin-only）：收益 × 时间折扣 − 横向位移代价。

    偏序纪律（验收 3）：同 conf 下枚数单调、同枚数下越近越高、越偏越贵；
    存续组（conf_min=0）自动被折扣到 ≤ 代价被拒——不虚增。
    """

    def __init__(self, cfg: DecisionConfig, cal: Calib):
        self.c = cfg.scoring
        self.cal = cal
        self.hz = cfg.control.frame_rate_hz

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

    # ---------- 主入口 ----------

    def update(self, obs: WorldObservation, dt_s: float,
               executed_lane: float | None = None) -> DecisionOutput:
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
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state, target_id=self._target_gid,
            x_target=self._x_smooth if self._state is not DecisionState.CONSERVE else None,
            move_allowed=move_allowed, reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks())

    # ---------- 状态处理 ----------

    def _tick_cruise(self, obs: WorldObservation) -> str:
        cand = self._select(obs, current=None)
        if cand is None:
            self._target_gid = None
            return "hold:no_candidate" if not self._cool_t else "hold:cooling"
        if cand.group.validity_until_fid <= obs.frame_id:
            return "hold:no_candidate"
        self._target_gid = cand.group.group_id
        self._cur_score = cand.score
        self._demand_lane = abs(cand.group.x_center)
        self._change_t = 0.0
        self._deadzone_ticks = 0
        self._state = DecisionState.CHANGE
        return "select:score_win"

    def _tick_change(self, obs: WorldObservation, dt_s: float,
                     executed: float | None) -> str:
        self._change_t += dt_s
        g = self._group_of(obs)
        if g is None or g.validity_until_fid <= obs.frame_id:
            # 目标失联过宽限（组级）：取消计划，有界回稳（§二消失语义）
            self._enter_abort("target_lost")
            return "cancel:target_lost"
        # 移动中改判（§三加性切换门生效处）：更优组超过 当前分+margin 才换目标，
        # 换的是"去哪"（x_goal/需求重捕），不是重新起变道——迟滞防的就是拉扯。
        better = self._select(obs, current=self._cur_score)
        if better is not None and better.group.group_id != self._target_gid:
            self._target_gid = better.group.group_id
            self._cur_score = better.score
            self._demand_lane = abs(better.group.x_center)
            self._deadzone_ticks = 0
            return "switch:score_win"
        if executed is not None:
            # 反馈路完成判据：已执行位移吃掉需求 ×(1−ε) 且目标在场
            if abs(executed) >= self._demand_lane * (1 - 1e-6) - self.cfg.hysteresis.dead_zone_lane:
                return self._finish_change("converged:feedback")
            # 越界复核（对当前目标）：归一发散现形 → 取消
            if abs(g.x_center) + g.x_span >= self.cfg.validate.x_lane_abs_max:
                self._enter_abort("x_bound")
                return "cancel:x_bound"
            return "moving:feedback"
        # v1 无反馈降级：目标 x 读数连续 2 tick 进死区视为收敛，超时强制落位
        if abs(g.x_center) <= self.cfg.hysteresis.dead_zone_lane:
            self._deadzone_ticks += 1
            if self._deadzone_ticks >= 2:
                return self._finish_change("converged:proxy")
        else:
            self._deadzone_ticks = 0
        if self._change_t >= self.cfg.control.t_change_max_s:
            return self._finish_change("change_timeout")
        return "moving:proxy"

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
        """候选=金币组；门=最低分 + 切换加性阈 + 时机成立（§三公式）。"""
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
        self._cur_score = -math.inf

    def _finish_change(self, why: str) -> str:
        self._state = DecisionState.CRUISE
        self._cool_t = self.cfg.hysteresis.t_cool_s   # 完成也要冷却（§三：三收尾同权）
        self._target_gid = None
        self._cur_score = -math.inf
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
            g = self._group_of(obs)
            if g is not None:
                return g.x_center
        return 0.0

    def _validity_ticks(self) -> int:
        return max(2, int(round(0.1 * self.cfg.control.frame_rate_hz)))
