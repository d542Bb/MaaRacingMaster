# -*- coding: utf-8 -*-
"""行为决策层（2026-10-18 换血：场即选择器，算场→跟场）。

**它是什么**：每 tick 吃 `WorldObservation`（+ tick 时长 + 执行回执 + 车流 +
路心锚/走廊/栅格），走廊收益场（corridor_reach）沿走廊采样候选横位逐位打分
（金币/错身/擦边/奖励车收益 − 碰撞/窄缝大负罚 − 软净空罚 − 位移代价），胜出位
每拍滚动为目标，轨迹采样器执行。承诺制（意图/完成判据/缺席记忆）、目标锁定
（σ/紧急回避/否则决）、legacy 目标链（选目标打分/超车目标/冷却）于本日退役
（外部评审 + 用户定案：补丁堆的根源是规划器消费对象不消费空间；空间优先级由
场净空项承担，历史结论由 commit 指针回溯）。

**稳定化手段**（与旧承诺制的分工）：场逐拍滚动（报告在场拍更新目标）+ 轨迹
采样器拒绝不安全轨迹 + 输出低通；报告缺席拍冻结上一拍场选点（预算内），
超预算冻结自车当前位。

**FSM**：CRUISE 场跟随 / ABORT 有界回稳（fatal 信号触发）/
CONSERVE 保持+禁变道 / FAULT 纯直行。采样被拒不进状态动作（blocked=原地
持守是场跟随的自然语义，破判进 ABORT 会制造 abort 震荡——贴身墙场景实测）。

**输入输出**：每 tick 吐 `DecisionOutput`（reason/valid_until 齐备可回放）。
时钟用 tick 累计（`time.monotonic` 不进本模块——回放与实机同一确定性）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from maaracing_master.plugins.speedrush.config import DecisionConfig
from maaracing_master.plugins.speedrush.corridor import Corridor
from maaracing_master.plugins.speedrush.corridor_reach import (
    LANE_HALF, BonusSource, CarSource, CoinSource, CorridorBandSpan,
    ReachParams, ReachReport, evaluate)
from maaracing_master.plugins.speedrush.depth_geo import DrivableGrid
from maaracing_master.plugins.speedrush.latsample import (
    LatTrajectorySampler, SampledTraj, eval_traj)
from maaracing_master.plugins.speedrush.traffic import CarView, PassEvent
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_BONUS, Calib, load_calib, t_meet_car_s, x_lane_of)

_SCHEMA = 3  # v3：场即选择器换血（reanchor_lane/target_id 契约退役，2026-10-18）
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
        self.ov = cfg.overtake
        self.cal = cal
        self.hz = cfg.control.frame_rate_hz
        # 采集帧率（Hz，fid 时基）：引擎逐拍从 obs.fid_rate_hz 喂入（单线程
        # owner 实例通道纪律，与 _views/_events 同族）；None=预热未达/旧供数，
        # car_t_meet_s 退回旧行率式（已知 TTC 高估方向的旧消费口径，行为不
        # 静默换式）
        self.fid_hz: float | None = None
        # P̂_limit 在计划贴窗 d̂_min=d_hold 处取值（step 2 计划保证值暂替，
        # 真实 plant 前推留 planner §十一）：对全体候选是常数，排序由 t/偏移决定。
        self._p_hold = 1.0 / (1.0 + math.exp(
            (cfg.overtake.d_hold_lane - cfg.overtake.p_center_lane)
            / cfg.overtake.p_scale_lane))
        self._e_overtake = self.ov.value_base + self._p_hold * self.ov.value_limit

    @staticmethod
    def cand_x(view: CarView) -> float:
        """决策用横向：车道成员位成立用成员位（整道中心，抗近场单拍读数
        噪声/膨胀——V0 病灶①，前置三件套②），未成立退回单拍读数不装懂。
        安全几何（veto 判距/回避车体判距）不经过这里，仍吃原始物理位。
        鸭子视图（TrackedTarget/测试 _Car）无成员位字段时同退回单拍。"""
        m = getattr(view, "x_memb", None)
        return view.x_lane if m is None else m

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
        """像素仪器（回退路径）：cand_x（成员位优先）+ 地面流 t_meet
        （pacing=非正 rel → None=占用）。原 lane_score 住户，退役随迁。"""
        return self.cand_x(v), (self.car_t_meet_s(v.rel_approach, v.cy)
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
        # 分基值再被车道惩罚一压连 min_score 都过不了（20261014 实测 score 拒
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
        # 轨迹采样器（阶段二 §2.3）：参数复用既有安全/运动面——横向可达域=
        # v_lat_max、危险窗=lat_veto_horizon_s、接触界=lat_veto_gap_lane。
        self.traj = LatTrajectorySampler(
            v_lat_max=cfg.planner.v_lat_max, v_ego_row=self.cal.v_ego,
            horizon_s=cfg.validate.lat_veto_horizon_s,
            gap_lane=cfg.validate.lat_veto_gap_lane,
            grid_margin_lane=cfg.grid_veto.margin_lane,
            grid_car_half_m=cfg.grid_veto.car_half_lane_m,
            k_grid_unknown=cfg.grid_veto.k_unknown,
            t_meet_fn=self.scorer.car_t_meet_s,
        ) if cfg.mode.trajectory_sampling else None
        # 车米制状态实例通道（C 线；update 每拍刷新，reset 清——与 _views 同族）
        self._metric_cars: tuple = ()
        self._metric_by_id: dict = {}    # 逐车米制索引（采样器消费面）
        # 走廊收益场参数面（corridor_reach 闸，2026-10-14 设计稿 §3.2-§3.4）：
        # enabled 开才构造（车道换算件在边界注入，本类内无车道单位）
        self._reach_params = self._build_reach_params() \
            if cfg.corridor_reach.enabled else None
        self.reset()

    def reset(self) -> None:
        """阶段切换/保守态出口/接线重入（重置面：全清，id 语义归上游）。"""
        self.watch.reset()
        self._state = DecisionState.CRUISE
        self._boundary = None
        self._x_smooth = 0.0
        self._conserve_t = 0.0
        self._ok_t = 0.0
        self._abort_next = DecisionState.CRUISE
        self._abort_ref: float | None = None   # ABORT 速率落稳参考（_enter_abort 重置）
        self._dt_last: float = 0.05            # 上一拍 dt（慢拍计时基；reset 兜底值）
        self._fault = False
        self._last_report_ok = True
        # 轨迹模式状态（阶段二）：初态速度来自执行回执差分 EMA（回执缺位 0）
        self._prev_exec: float | None = None
        self._vd = 0.0
        self._traj_prev = None                 # 上一拍中选轨迹（调试/续接面）
        self._traj_last_dt: float | None = None
        self._ro: float | None = None
        self._rwidth: float | None = None
        self._grid: DrivableGrid | None = None
        self._grid_hold: DrivableGrid | None = None
        self._corridor: Corridor | None = None
        self._traj_diag: dict | None = None   # 逐拍采样诊断（trace 消费）
        # 走廊收益场逐拍态：报告/目标跨阶段清——参数面不跨阶段（__init__ 构造）
        self._reach_report: ReachReport | None = None
        self._reach_goal: float | None = None
        # 场跟随（2026-10-18 换血）：报告缺席计时与单拍输出冻结声明
        # （超预算冻结自车当前位），update() 输出段读后即清
        self._absence_t = 0.0
        self._hold_output = False

    # ---------- 主入口 ----------

    def update(self, obs: WorldObservation, dt_s: float,
               executed_lane: float | None = None,
               traffic: tuple[tuple[CarView, ...],
                              tuple[PassEvent, ...]] | None = None,
               road_offset: float | None = None,
               road_width: float | None = None,
               grid: DrivableGrid | None = None,
               corridor: Corridor | None = None) -> DecisionOutput:
        """traffic=(在途车辆视图, 本拍落定事件)（traffic.TrafficObserver 直供）。

        缺省 None=无车流观测，行为与 coin-only v1 **逐拍相同**（V0/V1 兼容红线）。
        逐拍视图放实例通道（单线程 owner 纪律，与 _log_grp 同族）——helper 签名不扩散。

        ``road_offset``/``road_width``（阶段二 §一）：路心锚与 W 兜底宽度，仅
        轨迹模式消费；ro 缺席拍轨迹诚实降级回 legacy 输出形成（不臆造路心系）。
        ``grid``（S2-B）：同拍可行驶栅格，仅轨迹模式且 grid_veto 闸开时进
        轨迹评分；闸关/None=不触栅格代价，与关闸前逐拍一致。
        ``corridor``（第 6 步）：同拍分带走廊，场的采样域与净空障碍面消费
        （evaluate 走廊带）；None=无证据，场诚实弃权。"""
        self._dt_last = dt_s               # 慢拍计时基（缺席预算累计）
        self._views, self._events = traffic if traffic is not None else ((), ())
        # 车米制状态实例通道（C 线 2026-10-12，闸 metric_cars.enabled）：场
        # 车源链消费（米制优先、像素回退）；闸关=空（回退路径）。
        self._metric_cars = obs.metric_cars if self.cfg.metric_cars.enabled else ()
        # 逐车米制索引：轨迹采样器危险窗截断吃同源通道
        self._metric_by_id = {m.target_id: m for m in self._metric_cars}
        _metric_map = self._metric_by_id if self.cfg.metric_cars.fusion else None
        if self.traj is not None:
            self.traj.metric_map = _metric_map
        # 采集帧率逐拍喂入评分器（car_t_meet_s 的 px/fid→秒换算锚；None=
        # 预热未达，评分器自决退回旧口径）
        self.scorer.fid_hz = obs.fid_rate_hz
        self._boundary = obs.boundary
        self._corridor = corridor
        self._grid_hold = grid
        self._ro = road_offset
        self._rwidth = road_width
        self._grid = grid if self.cfg.grid_veto.enabled else None
        # 走廊收益评定（corridor_reach 闸开才算；**先于状态分派**——场跟随
        # 同拍消费不隔拍，shadow 列同拍落）
        self._reach_report = (self._reach_evaluate(obs, executed_lane)
                              if self._reach_params is not None else None)
        # 优先级：致命信号 > ABORT/保守/故障 > 场跟随。本 tick 先处理转移，
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

        # 执行回执差分 → 初态横向速度 EMA（轨迹模式初态续接的测量面；回执
        # 缺位保持旧值，饱和在 v_lat_max 内）
        if executed_lane is not None and dt_s > 0:
            if self._prev_exec is not None:
                lim = self.cfg.planner.v_lat_max
                inst = max(-lim, min(lim, (executed_lane - self._prev_exec) / dt_s))
                self._vd += (1.0 - math.exp(-dt_s / 0.2)) * (inst - self._vd)
            self._prev_exec = executed_lane

        # 输出目标：轨迹模式=每拍重采样、输出选中轨迹的前瞻点（§2.3 滚动重评）；
        # 回执缺位/ro 缺席/非巡航态 → x_smooth 位置常量路径。
        # 输出冻结拍（场缺席超预算）：不采样轨迹（滚动目标是塌回点，采样即执行
        # 过期方向），x_target 冻结 executed——绕开平滑器防漂移。
        hold_output = self._hold_output
        self._hold_output = False           # 单拍声明，读后即清
        xt = None
        pause_hold = None
        if hold_output and executed_lane is not None:
            pause_hold = executed_lane
        self._traj_diag = None
        if self.traj is not None and executed_lane is not None \
                and self._ro is not None \
                and not hold_output \
                and self._state is DecisionState.CRUISE:
            d_t = self._traj_d_target(obs, executed_lane)
            if d_t is not None:
                rep = self.traj.update(
                    d0=executed_lane, vd0=self._vd, ad0=0.0, d_target=d_t,
                    cars=self._views, width=self._rwidth, ro=self._ro,
                    hz=self.cfg.control.frame_rate_hz,
                    grid=self._grid, lane_w_m=self.cal.lane_w_m)
                # 逐拍采样诊断（S2-B 验收观测面）：否决分项与中选终点——
                # trace 消费，无此列则实机否决频次不可验
                self._traj_diag = {
                    "ok": rep.ok, "why": rep.why, "n_eval": rep.n_eval,
                    "n_grid": rep.n_reject_grid,
                    "n_col": rep.n_reject_collision,
                    "n_reach": rep.n_reject_reach,
                    "n_bound": rep.n_reject_bound,
                    "best": None if rep.best is None
                    else [round(rep.best.d1, 3), rep.best.T]}
                if rep.ok:
                    self._traj_prev = rep.best
                    self._traj_last_dt = d_t
                    xt = eval_traj(rep.best.coeffs,
                                   min(self.cfg.planner.lookahead_tau_s,
                                       rep.best.T))
                # 采样被拒：诊断列照记，无状态动作（场跟随下 blocked=原地持守
                # 是自然语义——场目标即最优位，执行被拒时走 goal 路径持守；
                # ABORT 有界回稳只在 fatal 时进入，破判不进——破判进 ABORT
                # 会制造 abort 震荡，贴身墙场景实测）

        goal = (self._reach_goal / self.cal.lane_w_m
                if self._reach_goal is not None else 0.0)
        alpha = 1.0 - math.exp(-dt_s / self.cfg.hysteresis.tau_filter_s)
        if pause_hold is not None:
            # 冻结拍直通 executed 绕开平滑器（场缺席超预算）：x_target 冻结
            # 自车当前位，_x_smooth 同步到冻结位，恢复拍无跳变。
            self._x_smooth = pause_hold
        else:
            self._x_smooth += alpha * ((xt if xt is not None else goal)
                                       - self._x_smooth)
        x_out = pause_hold if pause_hold is not None \
            else (xt if xt is not None else self._x_smooth)
        move_allowed = self._state is DecisionState.CRUISE
        if not self.cfg.mode.allow_all_moves:
            move_allowed = False
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state,
            x_target=x_out if self._state is not DecisionState.CONSERVE else None,
            move_allowed=move_allowed, reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks())

    @property
    def traj_diag(self) -> dict | None:
        """上一拍轨迹采样诊断（S2-B 验收观测面，trace 消费；None=非轨迹拍）：
        ok/why/n_eval/否决分项（n_grid 栅格/n_col 街车/n_reach 可达域/n_bound
        边界）/中选 [d1, T]。"""
        return self._traj_diag

    @property
    def traj_plan(self) -> "SampledTraj | None":
        """上一拍中选轨迹本体（调试图渲染面；coeffs 体积大，不进 trace）：
        d1/T/cost/coeffs，None=非轨迹拍或全灭。只读透传，消费方不得改。"""
        return self._traj_prev

    def _traj_d_target(self, obs: WorldObservation,
                       executed: float) -> float | None:
        """轨迹目标（路心系，道）：场跟随=道收益持守道（x_goal 每拍滚动重评，
        米制→道换算在边界）；报告缺席=诚实降级 None（轨迹链回 legacy 输出
        形成，不臆造路径）。"""
        return (self._reach_goal / self.cal.lane_w_m
                if self._reach_goal is not None else None)

    # ---------- 走廊收益场（corridor_reach 闸，2026-10-14 设计稿） ----------

    def _build_reach_params(self) -> ReachParams:
        """走廊收益场参数面（进决策边界换算，车道单位不外漏）：need=timing
        变道耗时模型（天然以道计，|Δx|/lane_w 换算）；位移价/归属半宽/错身带
        沿既有语义换算成米制（单一真源不复制）；tau/margin=feas 同源
        （plan._feasible 的 tau_resp+margin，时序窗病理的反面编码处）。
        R3 贴身猎车（§2.2）：总闸+分开关都开才挂贴身参数——窗沿/P 曲线/极限
        动作分全部由 overtake 节与 hug_half_width_lane 在边界换算成米制
        （派生关系：lo/hi = d_hold ∓ half），闸关=不带字段=逐位回今天。
        R2 前瞻外方向权（§2.4）：far_direction 开才挂——远权/作用域上界/
        聚合语义随参数入表；E 曲线复用 R3 贴身同一条（无 R3 参数时无 E）。"""
        lane_w = self.cal.lane_w_m
        c = self.cfg.corridor_reach
        hug_kw = {}
        if self.cfg.metric_bridge.enabled:
            # 速度像素桥不确定度（闸 metric_bridge.enabled，闸关不传=None=
            # 逐位回今天）：威胁降权 + 桥接车可达门裕度放宽
            hug_kw["bridge_weight"] = self.cfg.metric_bridge.weight
            hug_kw["bridge_margin_add_s"] = self.cfg.metric_bridge.margin_add_s
        if c.overtake_hunting and c.hug_value:
            ov = self.cfg.overtake
            center_m = ov.d_hold_lane * lane_w
            half_m = c.hug_half_width_lane * lane_w
            hug_kw = dict(hug_d_lo_m=center_m - half_m,
                          hug_d_hi_m=center_m + half_m,
                          hug_p_center_m=ov.p_center_lane * lane_w,
                          hug_p_scale_m=ov.p_scale_lane * lane_w,
                          hug_value_limit=ov.value_limit)
            if c.far_direction:
                hug_kw.update(far_car_weight=c.far_car_weight,
                              hunt_horizon_s=c.hunt_horizon_s,
                              far_aggregation=c.far_aggregation)
        return ReachParams(
            step_m=c.step_m, horizon_s=c.horizon_s, crash_t_s=c.crash_t_s,
            crash_penalty=c.crash_penalty, pass_value=c.pass_value,
            far_coin_weight=c.far_coin_weight, tie_margin=c.tie_margin,
            shift_cost_per_m=self.cfg.scoring.shift_cost_per_lane / lane_w,
            coin_value_per_unit=self.cfg.scoring.coin_value_per_unit,
            coin_group_min_count=self.cfg.scoring.coin_group_min_count,
            attribution_half_m=LANE_HALF * lane_w, pass_band_m=lane_w,
            tau_s=self.cfg.timing.tau_resp_s,
            margin_s=self.cfg.timing.margin_s,
            need_of_dx_m=lambda dx_m: self.cfg.timing.lane_change_duration_s(
                abs(dx_m) / lane_w),
            bonus_value=c.bonus_car_value if c.bonus_value else 0.0,
            hug_visual=c.hug_visual,
            bonus_tol_m=c.bonus_tol_m,
            clearance_ref_m=c.clearance_ref_lane * lane_w,
            clearance_penalty=c.clearance_penalty,
            squeeze_min_gap_m=c.squeeze_min_gap_lane * lane_w,
            **hug_kw)

    def _reach_evaluate(self, obs: WorldObservation,
                        executed_lane: float | None) -> ReachReport | None:
        """一拍走廊收益评定（闸开才算；shadow 落 trace cr_* 对照列，control
        同拍消费）。坐标换算在**进决策边界一次**（路心系米）：走廊缘=自车系
        米 + executed 平移；coin=(x_center+executed)×lane_w；车仪器链与
        lane_score 同源（米制优先、像素回退）。executed/走廊缺席=诚实弃权
        None（与 lane_score 同语义）。"""
        if executed_lane is None or self._corridor is None:
            return None
        lane_w = self.cal.lane_w_m
        shift = executed_lane * lane_w          # 自车系→路心系平移（米）
        bands = [CorridorBandSpan(
            b.z0, b.z1,
            None if b.left.x_m is None else b.left.x_m + shift,
            None if b.right.x_m is None else b.right.x_m + shift)
            for b in self._corridor.bands]
        coins = []
        for g in obs.coin_groups:
            if g.x_center != g.x_center:
                continue                        # 存续组 nan 心：不入账
            s = self.scorer.score(g, obs)
            if s is None:
                continue                        # 置信地板（质量门语义一致）
            coins.append(CoinSource(
                x_m=(g.x_center + executed_lane) * lane_w,
                observed_count=g.observed_count, conf=g.conf_min,
                t_miss_s=s.t_miss_s))
        # 奖励车源（R5 层 1，设计稿 §9.1）：几何与到站复用 plan.py bonus 候选
        # 同源设计（撞上去，维护者裁定 2026-10-05）——视觉体位 x_lane、
        # car_t_meet_s 到站、track 年龄门槛、不接近不收；闸关=空列表=逐位回今天
        bonuses: list[BonusSource] = []
        if self.cfg.corridor_reach.bonus_value:
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
                    t_miss_s=t_miss, conf=b.conf))
            # 远场粗源（R5 §10，设计稿 §10.1）：奖励车整个可见生命在远场
            # （§9.4 归因），obs.targets 循环见不到——扩到 far_targets 用
            # x_lane_of 同一公式真源读粗横位（X/H 比值与距离无关，发散的是
            # 噪声放大），夹进走廊已知路缘兜住误差；宽容差 tol_m=模糊处理
            # （维护者口径：远场大可以模糊，不必给出精确值）
            bonuses.extend(self._reach_bonus_far_sources(obs, executed_lane))
        return evaluate(bands, coins, self._reach_car_sources(executed_lane),
                        shift, self._reach_params, bonuses=bonuses)

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
                t_miss_s=t_miss, conf=b.conf))  # tol_m 缺省=bonus_tol_m（近远场同源）
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
                  bridged: bool = False) -> None:
            if x is not None and x == x:
                out.append(CarSource(x_m=x + off, t_meet_s=tm,
                                     t_overtake_s=t_ov, bridged=bridged))

        for m in self._metric_cars:
            v = by_id.get(m.target_id)
            if v is not None and v.cy >= self.cal.v_ego:
                seen.add(m.target_id)           # 已过自车行：不占道
                continue
            bridged = getattr(m, "bridged", False)
            if m.z_m is not None:
                x = (m.x_m if m.x_m is not None
                     else (self.scorer.cand_x(v) * lane_w
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
            enter(x, tm, t_ov, bridged=bridged)
        for vid, v in by_id.items():
            if vid in seen or v.cy >= self.cal.v_ego:
                continue                        # 米制路径已入账，不双计
            x, tm = self.scorer.pixel_instruments(v)
            enter(None if x is None else x * lane_w, tm)
        return out

    @property
    def reach_report(self) -> "ReachReport | None":
        """上一拍走廊收益评定（corridor_reach 闸开才有值；trace cr_* 对照列
        的数据源，不进决策——行为切换在 control 闸）。None=闸关/弃权拍。"""
        return self._reach_report

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
        """CRUISE 场跟随（2026-10-18 换血：场即选择器，算场→跟场——承诺制
        （意图/完成判据/缺席记忆）与目标锁定（σ/紧急回避/否则决）退役，
        稳定化=场逐拍滚动 + 轨迹采样器，横向安全由场净空项+采样器 veto
        接管）。报告在场 → 目标=胜出位每拍滚动；报告缺席 → 冻结上一拍场
        选点（不塌回 0，防段状缺席期目标振荡），超预算 → 冻结自车当前位
        直到报告恢复（依据：缺席段短——证据闪图谱实测 8~14% 拍；冻结目标
        受采样器 veto+栅格 veto+致命信号三重兜底）。"""
        rep = self._reach_report
        if rep is not None:
            self._absence_t = 0.0
            self._reach_goal = rep.best_x_m
            return "follow:reach"
        if executed is None:
            self._reach_goal = None
            return "hold:reach_hold"
        self._absence_t += self._dt_last
        if self._absence_t > self.cfg.corridor_reach.absence_budget_s:
            self._hold_output = True       # 超预算：冻结自车当前位（不跟陈旧场）
            return "pause:reach_absent"
        return "hold:reach_absent"         # 预算内：冻结上一拍选点（goal 不清）

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

    def _degraded(self, obs: WorldObservation) -> bool:
        """恢复判定：与致命信号同一合取的"健康面"——alive+fresh+presence 或过渡。"""
        h = obs.health
        return not (h.perception_alive and h.frame_fresh)

    def _enter_abort(self, why: str) -> None:
        self._state = DecisionState.ABORT
        self._change_t = 0.0
        self._abort_ref = None      # 速率参考从 ABORT 首拍重新起算
        self._blocked_s = 0.0       # 集已终止，阻塞计时随集走

    def _enter_conserve(self) -> None:
        self._state = DecisionState.CONSERVE
        self._conserve_t = 0.0
        self._ok_t = 0.0
        self._target_gid = None
        self._target_kind = None
        self._cur_score = -math.inf
        self._intent = None            # 保守态硬清意图（致命收场不留追逐态）

    def _validity_ticks(self) -> int:
        return max(2, int(round(0.1 * self.cfg.control.frame_rate_hz)))
