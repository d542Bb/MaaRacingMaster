# -*- coding: utf-8 -*-
"""行为决策层（v1 = coin-only 保守基线；v2 = 超车候选并秤，阶段 C 设计稿 §二/§四）。

`mode.allow_overtake`（默认 false）关闸时行为与 v1 **逐拍相同**（兼容性红线，
test_overtake_gate_closed 上锁）；开闸后超车与金币两类候选同一分式形状比较
（期望分×时间折扣−横向代价），完成判据按 kind 分派——金币走需求/死区收敛路，
超车走 pass 事件落定路（移动目标永不到"正中间"，维护者口径第 4 条）。

**输入输出**：每 tick 吃 `WorldObservation`（+ tick 时长 + 可选规划层横向反馈），
吐 `DecisionOutput`（D1：只吃结构化契约；D3：reason/valid_until 齐备可回放）。
时钟用 tick 累计（`time.monotonic` 不进本模块——回放与实机同一确定性）。

**阶段一升级（2026-10-02，外部对标后的最小换血；第五轮 trace 对账后修标定）**：
决策输出仍是「位置常量」，但三处投影先修——①可行性门从**执行回执**起算耗时
（need=|目标−executed|，替换自车瞬移假设；回执缺位维持旧口径 0 起算，兼容红线）；
②CHANGE 逐拍重评按 **收益制分形**：金币是及格制（到不了=零分），窗口剩余对
剩余耗时连拍破判即弃追；超车是 pass 制（车反正要过，弃追只会贴得更远），只判
**逃逸**（散布超贴窗距离且不收缩，首拍立基线）。③街车静态参与判距的 RSS 形状
几何安全 veto（LateralSafety，独立判据不进评分；危险窗按逐车到站时间截断；
CV 外推已撤销——读数噪声统计上不可分辨真漂移，第六轮正中靶心案根因）；
veto 拦截语义=**机动使距离恶化才拦**（预测最小距跌破接触界且比当前距更近，
第七轮：紧急回避免被起点自否）。④本车道前车紧急回避（`_dodge_candidate`，
ttc<`ttc_dodge_s` 且横距<接触界 → 绕过冷却与评分强制变道到远离侧——冷却/
无候选盲持是追尾窗口；归 allow_overtake 闸管）。⑤贴邻侧别 σ 在选中拍按
自车位置锁定（`_hug_sigma_of`）：回避/追击中目标只跟车不翻边，不横穿车体。

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
from maaracing_master.plugins.speedrush.latsample import (
    LatTrajectorySampler, eval_traj)
from maaracing_master.plugins.speedrush.traffic import (
    OUTCOME_PASS, CarView, PassEvent)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import Calib, load_calib

_SCHEMA = 2  # v2：DecisionOutput.reanchor_lane（planner 设计稿 §二，2026-09-22 用户裁定）
# 阶段 C step 2 **不再升 schema**：超车能力扩的是决策层内部（Scored/选择/完成判据分派）
# 与入参通道（traffic= 关键字，默认 None 旧调用零扰动），DecisionOutput 出口契约不变。
_EPS = 1e-9
# 逐拍重评的防抖连拍数（读数噪声量级的单拍破判不撕计划；与死区代理的
# _deadzone_ticks>=2 同款自卫）
_REEVAL_STREAK = 2

KIND_CAND_COIN = "coin"
KIND_CAND_OVERTAKE = "overtake"


def _hug_sigma_of(ref: float, x_lane: float) -> float:
    """贴邻侧别：目标放在「ref 所在一侧、距目标 d_hold」处。ref 与目标同位
    （直接正后）退回旧内贴语义（σ=sign(x)）——两侧等价，取确定值即可。"""
    d = ref - x_lane
    if abs(d) <= _EPS:
        return 1.0 if x_lane >= 0 else -1.0
    return 1.0 if d > 0 else -1.0


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


class LateralSafety:
    """横向几何安全校验（阶段一 2026-10-02）：RSS 形状的外挂 veto。

    职责切分（behavior-design §四）不变：评分层选目标，本校验器只回答
    「这个横向机动会不会撞上街车」——独立几何判据，**不进收益评分**。

    几何：自车按横向速率上限从 executed 向 goal 全速扫掠（包络=最坏情况），
    **街车一律按静态位置参与判距**。曾经用帧间差分 EMA 做 CV 外推（第六轮
    撤销）：x_lane 读数噪声 ±0.3 道/拍，20Hz 差分的 EMA 速率实测 P90≈3.8
    道/s（物理不可能）——真漂移 0.2~0.4 道/s 被噪声淹没 10 倍，统计上不可
    分辨；外推等于让每辆车按噪声方向幻影瞬移，既误杀好追击、又把实际停在
    走廊里的车"外推飘走"造成正中靶心。静态判据 + **逐拍当前位置重查**等效
    于接近监视：真向我们漂的车几拍内当前位置就进走廊触发 veto，且不被噪声骗。

    **危险窗按逐车到站时间截断**：车过自车行之后就吹不到我们了——判距只在
    min(视野, 该车到站时间) 内做。到站时间与评分同口径（(v_ego−cy)/(rel·hz)），
    rel≤0（不接近）→ 永不到站 → 全视野。

    **回执缺位不判**（veto_reason 收 None start）：几何检查不容假起点——
    无回执时自车真实位置未知，按 0 起算的包络是毒化判据；缺位=无此保护，
    与 v1 相同（不倒退也不臆造）。nan 目标（存续组）同跳过。"""

    def __init__(self, cfg: DecisionConfig, cal: Calib):
        self.v = cfg.validate
        self.cal = cal
        self.v_lat_max = cfg.planner.v_lat_max
        self.hz = cfg.control.frame_rate_hz

    def _t_pass_s(self, v: "CarView") -> float:
        """该车到自车行的秒数（与超车评分同一接近口径）；不接近 → 永不到站。"""
        rate = v.rel_approach * self.hz
        return max(0.0, self.cal.v_ego - v.cy) / rate if rate > _EPS else math.inf

    def min_gap_lane(self, start: float, goal: float,
                     views: tuple["CarView", ...]) -> float:
        """自车扫掠包络 × 街车静态位置在危险窗内的最小车距（车道，中心距）。

        包络两段线性（到达 goal 前全速、之后驻停），段内 |差| 极值在端点或
        相交（=0）。views 空 → inf。"""
        gap_min = math.inf
        d = goal - start
        sgn = 1.0 if d >= 0 else -1.0
        t_star = abs(d) / self.v_lat_max if self.v_lat_max > _EPS else math.inf
        for v in views:
            gap_min = min(gap_min, self._car_gap(start, goal, sgn, t_star, v))
        return gap_min

    def _car_gap(self, start: float, goal: float, sgn: float, t_star: float,
                 v: "CarView") -> float:
        """单车危险窗内最小距：包络两段线性对静态车位的 |差| 极值。"""
        t_eff = min(self.v.lat_veto_horizon_s, self._t_pass_s(v))
        if t_star <= t_eff:
            return min(self._segment_min(start, v.x_lane, sgn * self.v_lat_max,
                                         0.0, t_star),
                       abs(goal - v.x_lane))
        return self._segment_min(start, v.x_lane, sgn * self.v_lat_max,
                                 0.0, t_eff)

    def veto_reason(self, start: float | None, goal: float,
                    views: tuple["CarView", ...]) -> str | None:
        if start is None or not views or goal != goal:
            return None
        d = goal - start
        sgn = 1.0 if d >= 0 else -1.0
        t_star = abs(d) / self.v_lat_max if self.v_lat_max > _EPS else math.inf
        for v in views:
            gap = self._car_gap(start, goal, sgn, t_star, v)
            # **机动使距离恶化才拦**（第七轮裁定）：紧急回避的起点就在目标车
            # 身上（这正是要逃的位置），未来距单调增大——不得自否；真正的
            # 危险是扫掠把距离压得比现在更近且跌破接触界（横穿/贴近）。
            if gap < self.v.lat_veto_gap_lane and gap < abs(start - v.x_lane):
                return "lat_veto"
        return None

    @staticmethod
    def _segment_min(e0: float, c0: float, ev: float, cv: float,
                     dur: float) -> float:
        """[0, dur] 内 |(e0+ev·t)−(c0+cv·t)| 的最小值：线性绝对值的极值只在
        端点或段内零点（相交）。"""
        if dur <= 0.0:
            return abs(e0 - c0)
        best = min(abs(e0 - c0), abs(e0 + ev * dur - c0 - cv * dur))
        rel = ev - cv
        if abs(rel) > _EPS:
            t_root = -(e0 - c0) / rel
            if 0.0 < t_root < dur:
                best = 0.0
        return best


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

    def hug_goal(self, x_lane: float, ref: float = 0.0) -> float:
        """贴邻目标（§四裁定）：在 ref（=自车位置）与目标同侧、距目标 d_hold 处。
        ref 缺省 0（自车道）时与旧「内贴」语义逐值同形——邻道车 σ=−sign(x)、
        目标在内侧；ref 进来后才有第七轮的「回避不横穿车体」能力（σ 选中拍
        锁定的基础式）。"""
        sigma = _hug_sigma_of(ref, x_lane)
        return x_lane + sigma * self.ov.d_hold_lane

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
        self.lateral = LateralSafety(cfg, self.cal)
        # 轨迹采样器（阶段二 §2.3；闸默认关=None=现役栈逐拍不变）：参数复用
        # 既有安全/运动面——横向可达域=v_lat_max、危险窗=lat_veto_horizon_s、
        # 接触界=lat_veto_gap_lane，与 LateralSafety 同源不另立真值。
        self.traj = LatTrajectorySampler(
            v_lat_max=cfg.planner.v_lat_max, v_ego_row=self.cal.v_ego,
            horizon_s=cfg.validate.lat_veto_horizon_s,
            gap_lane=cfg.validate.lat_veto_gap_lane,
        ) if cfg.mode.trajectory_sampling else None
        self.reset()

    def reset(self) -> None:
        """阶段切换/保守态出口/接线重入（重置面见 §二：全清，id 语义归上游）。"""
        self.watch.reset()
        self._state = DecisionState.CRUISE
        self._target_gid: int | None = None
        self._target_kind: str | None = None
        self._boundary = None
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
        self._abort_ref: float | None = None   # ABORT 速率落稳参考（_enter_abort 重置）
        self._late_streak = 0                  # 逐拍重评连破计数（_REEVAL_STREAK 判）
        self._last_disp: float | None = None   # 超车逃逸判据的散布基线（select/switch 重置）
        self._hug_sigma: float | None = None   # 贴邻侧别（选中拍按自车位置锁定，第七轮）
        self._fault = False
        self._last_report_ok = True
        self._reanchor_pending: float | None = None
        # 轨迹模式状态（阶段二）：初态速度来自执行回执差分 EMA（回执缺位 0）；
        # _traj_last_dt=上一拍轨迹目标（组转存续态保持，与 _current_goal 同语义）
        self._prev_exec: float | None = None
        self._vd = 0.0
        self._traj_last_dt: float | None = None
        self._ro: float | None = None
        self._rwidth: float | None = None

    # ---------- 主入口 ----------

    def update(self, obs: WorldObservation, dt_s: float,
               executed_lane: float | None = None,
               traffic: tuple[tuple[CarView, ...],
                              tuple[PassEvent, ...]] | None = None,
               road_offset: float | None = None,
               road_width: float | None = None) -> DecisionOutput:
        """traffic=(在途车辆视图, 本拍落定事件)（traffic.TrafficObserver 直供）。

        缺省 None=无车流观测，行为与 coin-only v1 **逐拍相同**（V0/V1 兼容红线）。
        逐拍视图放实例通道（单线程 owner 纪律，与 _log_grp 同族）——helper 签名不扩散。

        ``road_offset``/``road_width``（阶段二 §一）：路心锚与 W 兜底宽度，仅
        轨迹模式消费；ro 缺席拍轨迹诚实降级回 legacy 输出形成（不臆造路心系）。"""
        self._views, self._events = traffic if traffic is not None else ((), ())
        self._boundary = obs.boundary
        self._ro = road_offset
        self._rwidth = road_width
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
            reason = self._tick_cruise(obs, executed_lane)

        # 执行回执差分 → 初态横向速度 EMA（轨迹模式初态续接的测量面；回执
        # 缺位保持旧值，饱和在 v_lat_max 内）
        if executed_lane is not None and dt_s > 0:
            if self._prev_exec is not None:
                lim = self.cfg.planner.v_lat_max
                inst = max(-lim, min(lim, (executed_lane - self._prev_exec) / dt_s))
                self._vd += (1.0 - math.exp(-dt_s / 0.2)) * (inst - self._vd)
            self._prev_exec = executed_lane

        # 输出目标：轨迹模式=每拍重采样、输出选中轨迹的前瞻点（§2.3 滚动重评，
        # 破判连拍达 _REEVAL_STREAK 即 ABORT——防抖与 _miss_breach 同源纪律）；
        # 闸关/回执缺位/ro 缺席/非巡航变道态 → legacy x_smooth 位置常量路径。
        xt = None
        if self.traj is not None and executed_lane is not None \
                and self._ro is not None \
                and self._state in (DecisionState.CRUISE, DecisionState.CHANGE):
            d_t = self._traj_d_target(obs)
            if d_t is not None:
                rep = self.traj.update(
                    d0=executed_lane, vd0=self._vd, ad0=0.0, d_target=d_t,
                    cars=self._views, width=self._rwidth, ro=self._ro,
                    hz=self.cfg.control.frame_rate_hz)
                if rep.ok:
                    self._traj_prev = rep.best
                    self._traj_last_dt = d_t
                    xt = eval_traj(rep.best.coeffs,
                                   min(self.cfg.planner.lookahead_tau_s,
                                       rep.best.T))
                if not rep.ok and self._state is DecisionState.CHANGE:
                    self._late_streak += 1
                    if self._late_streak >= _REEVAL_STREAK:
                        self._enter_abort("traj_blocked")
                        reason = "cancel:traj_blocked"
                elif rep.ok and self._state is DecisionState.CHANGE:
                    self._late_streak = 0

        goal = self._current_goal(obs)
        alpha = 1.0 - math.exp(-dt_s / self.cfg.hysteresis.tau_filter_s)
        self._x_smooth += alpha * ((xt if xt is not None else goal)
                                   - self._x_smooth)
        move_allowed = self._state in (DecisionState.CRUISE, DecisionState.CHANGE)
        if not self.cfg.mode.allow_all_moves:
            move_allowed = False
        reanchor = self._reanchor_pending
        self._reanchor_pending = None      # 一次性：只在完成拍携带
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state, target_id=self._target_gid,
            x_target=(xt if xt is not None else self._x_smooth)
            if self._state is not DecisionState.CONSERVE else None,
            move_allowed=move_allowed, reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks(),
            reanchor_lane=reanchor)

    def _traj_d_target(self, obs: WorldObservation) -> float | None:
        """轨迹目标（路心系，道）：金币=ro+组心、超车=ro+锁定侧贴邻位、
        巡航=0（守轴）。组转存续态（nan/缺位）保持上一拍目标——与
        _current_goal 的"不外插 nan"同语义；无处可保持才 None（降级）。"""
        if self._state is DecisionState.CHANGE:
            if self._target_kind == KIND_CAND_OVERTAKE:
                v = next((x for x in self._views if x.id == self._target_gid), None)
                if v is not None:
                    return self._ro + self._overtake_goal(v.x_lane)
                return self._traj_last_dt
            g = self._group_of(obs)
            if g is not None and not math.isnan(g.x_center):
                return self._ro + g.x_center
            return self._traj_last_dt
        return 0.0

    # ---------- 状态处理 ----------

    def _tick_cruise(self, obs: WorldObservation, executed: float | None) -> str:
        # 紧急回避先于常规选择（第七轮）：冷却/无候选不是盲持等撞的理由。
        dodge = self._dodge_candidate(executed)
        if dodge is not None:
            v, goal = dodge
            self._target_gid = v.id
            self._target_kind = KIND_CAND_OVERTAKE
            self._cur_score = self.cfg.hysteresis.min_score   # 名义分：让位给更优改判
            self._hug_sigma = _hug_sigma_of(executed, v.x_lane)
            self._demand_lane = goal
            self._late_streak = 0
            self._last_disp = None
            self._change_t = 0.0
            self._deadzone_ticks = 0
            self._state = DecisionState.CHANGE
            return "select:dodge"
        cand = self._select(obs, current=None, executed=executed)
        if cand is None:
            self._target_gid = None
            self._target_kind = None
            self._hug_sigma = None
            return "hold:no_candidate" if not self._cool_t else "hold:cooling"
        if cand.kind == KIND_CAND_COIN \
                and cand.group.validity_until_fid <= obs.frame_id:
            return "hold:no_candidate"
        self._target_gid = cand.cand_id
        self._target_kind = cand.kind
        self._cur_score = cand.score
        self._late_streak = 0
        self._last_disp = None
        self._hug_sigma = (_hug_sigma_of(executed if executed is not None else 0.0,
                                         cand.car.x_lane)
                           if cand.kind == KIND_CAND_OVERTAKE else None)
        # 有符号需求（planner §二：反向不得过门）；超车候选的需求=选中拍锁定侧的
        # 贴邻目标，但超车完成走 pass 事件不走 demand 门（§四换轴），demand 只服务
        # 回执/trace
        self._demand_lane = (cand.group.x_center if cand.kind == KIND_CAND_COIN
                             else self._overtake_goal(cand.car.x_lane))
        self._change_t = 0.0
        self._deadzone_ticks = 0
        self._state = DecisionState.CHANGE
        return "select:score_win"

    def _tick_change(self, obs: WorldObservation, dt_s: float,
                     executed: float | None) -> str:
        if self._target_kind == KIND_CAND_OVERTAKE:
            return self._tick_change_overtake(obs, dt_s, executed)
        self._change_t += dt_s
        g = self._group_of(obs)
        if g is None or g.validity_until_fid <= obs.frame_id:
            # 目标失联过宽限（组级）：取消计划，有界回稳（§二消失语义）
            self._enter_abort("target_lost")
            return "cancel:target_lost"
        # 移动中改判（§三加性切换门生效处）：更优组超过 当前分+margin 才换目标，
        # 换的是"去哪"（x_goal/需求重捕），不是重新起变道——迟滞防的就是拉扯。
        better = self._select(obs, current=self._cur_score, executed=executed)
        if better is not None and \
                (better.kind, better.cand_id) != (KIND_CAND_COIN, self._target_gid):
            self._target_gid = better.cand_id
            self._target_kind = better.kind
            self._cur_score = better.score
            self._late_streak = 0
            self._last_disp = None   # 新目标新基线
            self._hug_sigma = (_hug_sigma_of(executed if executed is not None else 0.0,
                                             better.car.x_lane)
                               if better.kind == KIND_CAND_OVERTAKE else None)
            self._demand_lane = (better.group.x_center if better.kind == KIND_CAND_COIN
                                 else self._overtake_goal(better.car.x_lane))
            self._deadzone_ticks = 0
            return "switch:score_win"
        if executed is not None:
            # 反馈路完成判据（有符号）：executed 与需求同向且幅度吃掉需求×(1−ε) 且目标在场
            # ——反向移动不得借绝对值过门（planner 设计稿 §二，2026-09-22 用户裁定）
            if self._demand_consumed(executed):
                return self._finish_change("converged:feedback", reanchor=g.x_center)
            # 横向安全 veto（阶段一）：机动走廊被外推街车占据 → 立即有界回稳。
            # 安全判据不吃防抖连拍：误弃一个候选 ≪ 预测接触还继续扫的代价。
            veto = self.lateral.veto_reason(executed, g.x_center, self._views)
            if veto is not None:
                self._enter_abort(veto)
                return f"cancel:{veto}"
            # 逐拍重评（阶段一）：执行停滞/目标迫近时机会窗是否还够用——
            # 连拍破判即弃追，不追注定错过的变道（废变道病灶）。轨迹模式下
            # 由采样器破判接管（update 内 traj_blocked），此处跳过防双重计账。
            if self.traj is None:
                t_left = self._remaining_s(obs)
                if t_left is not None and self._miss_breach(g.x_center, executed, t_left):
                    self._late_streak += 1
                    if self._late_streak >= _REEVAL_STREAK:
                        self._enter_abort("infeasible")
                        return "cancel:infeasible"
                else:
                    self._late_streak = 0
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

    def _tick_change_overtake(self, obs: WorldObservation, dt_s: float,
                              executed: float | None) -> str:
        """超车计划路（阶段 C §三/§四）：完成=pass 事件落定（车已出画），
        失联/lost/ghost=ABORT 有界回稳；t_pass_max 兜底（车滞留不落的异常场）。
        不走 demand 收敛与死区代理——移动目标永远"走不到正中间"（维护者口径第 4 条）。
        阶段一：贴邻目标逐拍重评机会窗 + 横向安全 veto（含目标车自身的外推漂移）。"""
        self._change_t += dt_s
        v = next((x for x in self._views if x.id == self._target_gid), None)
        if v is None:
            ev = next((e for e in self._events if e.track_id == self._target_gid), None)
            if ev is not None and ev.outcome == OUTCOME_PASS:
                self._done_pass_ids.add(ev.track_id)
                # reanchor=None：超车完成拍没有"目标读数"可锚（车已出画）——
                # executed_lane 走路中心连续重锚（step 2.5），不吃事件锚
                return self._finish_change(
                    "overtake_pass", cool_s=self.cfg.overtake.t_cool_pass_s)
            self._enter_abort("target_lost")
            return "cancel:overtake_lost"
        better = self._select(obs, current=self._cur_score, executed=executed)
        if better is not None and \
                (better.kind, better.cand_id) != (KIND_CAND_OVERTAKE, self._target_gid):
            self._target_gid = better.cand_id
            self._target_kind = better.kind
            self._cur_score = better.score
            self._late_streak = 0
            self._last_disp = None   # 新目标新基线
            self._hug_sigma = (_hug_sigma_of(executed if executed is not None else 0.0,
                                             better.car.x_lane)
                               if better.kind == KIND_CAND_OVERTAKE else None)
            self._demand_lane = (better.group.x_center if better.kind == KIND_CAND_COIN
                                 else self._overtake_goal(better.car.x_lane))
            self._deadzone_ticks = 0
            return "switch:score_win"
        goal = self._overtake_goal(v.x_lane)
        veto = self.lateral.veto_reason(executed, goal, self._views)
        if veto is not None:
            self._enter_abort(veto)
            return f"cancel:{veto}"
        if executed is not None and self.traj is None:
            # 逃逸判据（第五轮）仅 legacy 计划形态需要——轨迹模式滚动重评
            # 天然跟车，破判路由采样器接管（update 内 traj_blocked）。
            disp = abs(goal - executed)
            if self._escape_breach(disp):
                self._late_streak += 1
                if self._late_streak >= _REEVAL_STREAK:
                    self._enter_abort("infeasible")
                    return "cancel:infeasible"
            else:
                self._late_streak = 0
            self._last_disp = disp
        if self._change_t >= self.cfg.overtake.t_pass_max_s:
            return self._finish_change("pass_timeout")
        return "moving:overtake"

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

    def _select(self, obs: WorldObservation, current, executed: float | None):
        """候选=金币组（+ allow_overtake 时的超车机会）；门=最低分 + 切换加性阈 +
        时机成立（§三公式；阶段 C §二：两类分数同分式形状，直接同秤比较）。

        阶段一（2026-10-02）：need 从**执行回执**起算（need=|目标−executed|，
        替换自车瞬移假设——执行滞后中位 0.45/纸 p90 0.9 车道的肇因）；回执缺位
        维持 0 起算旧口径（v1 兼容红线）。回执在场时另过横向安全 veto（假起点
        不判几何）。"""
        if self._cool_t > 0 or not self.cfg.mode.allow_all_moves:
            return None
        start = executed if executed is not None else 0.0
        best: Scored | None = None
        for g in obs.coin_groups:
            s = self.scorer.score(g, obs)
            if s is None or s.score < self.cfg.hysteresis.min_score:
                continue
            # 时机：从执行位出发的变道耗时 + 响应延迟 + 余量 < 错过时间
            need = self.cfg.timing.lane_change_duration_s(g.x_center - start)
            if need + self.cfg.timing.tau_resp_s + self.cfg.timing.margin_s >= s.t_miss_s:
                continue
            if executed is not None and \
                    self.lateral.veto_reason(executed, g.x_center, self._views):
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
                # 可行性门（19:44 复盘补装，与金币路同形）：从执行位到内贴目标的
                # 耗时+响应+余量必须 < 相对接近时间——"来不及贴"的车不追
                #（远距离白超 d_min 中位 1.11 的主犯；阶段一补执行回执口径）
                goal = self.scorer.hug_goal(v.x_lane, ref=start)
                need = self.cfg.timing.lane_change_duration_s(goal - start)
                if (need + self.cfg.timing.tau_resp_s
                        + self.cfg.timing.margin_s) >= s.t_miss_s:
                    continue
                # 横向安全 veto（阶段一）：机动走廊撞外推街车 → 拦下，不进评分
                if executed is not None and \
                        self.lateral.veto_reason(executed, goal, self._views):
                    continue
                # 空间闸门（维护者裁定 2026-09-22）：决策默认左右对称，禁用某方向
                # 必须有"那侧没空间"的显式证据（缘距读数）；证据不足=两侧都放行。
                if not self._side_has_space(v.x_lane):
                    continue
                if best is None or s.score > best.score:
                    best = s
        if best is None:
            return None
        if current is not None and best.score < current + self.cfg.hysteresis.switch_margin:
            return None
        return best

    def _remaining_s(self, obs: WorldObservation) -> float | None:
        """当前金币目标机会窗剩余（秒）=全场流积分到 cy_max（2026-10-02 换基
        口径）。无速率证据（全场无流）→ None=不判重评（缺证据不弃计划，
        与评分的无限远折扣同族安全默认，方向相反：评分侧缺证据压收益，
        重评侧缺证据不动计划）。超车路不走窗口判据（见 _escape_breach）。"""
        g = self._group_of(obs)
        if g is None:
            return None
        k = self.scorer._flow_k(obs)
        if k is None:
            return None
        return self.scorer._t_miss_ground_s(g.cy_max, k)

    def _miss_breach(self, x_target: float, executed: float, t_left: float) -> bool:
        """金币窗口破判（及格制：到不了=零分，弃追即止损）：从执行位出发的
        剩余耗时（变道模型 + 响应 + 余量）≥ 窗剩余。已在死区内不判——到达位
        附近没得可错过，收敛自会收尾。"""
        disp = abs(x_target - executed)
        if disp <= self.cfg.hysteresis.dead_zone_lane:
            return False
        t = self.cfg.timing
        return (t.lane_change_duration_s(disp) + t.tau_resp_s + t.margin_s) >= t_left

    def _escape_breach(self, disp: float) -> bool:
        """超车逃逸判据（第五轮 1742 案裁定）：超车是 **pass 制不是到点制**——
        车反正要从身边过，贴到哪都至少是 30 分的一次 pass，弃追向中回拉只会
        贴得更远。唯一纯浪费的形态是散布已超贴窗距离还不收缩：目标在逃或
        原地踏步（横漂后停住同判），追下去只是拖着车白占车道。首拍只记基线
        不判（无趋势可言）。"""
        if self._last_disp is None:
            return False
        return disp > self.cfg.overtake.d_hold_lane and disp >= self._last_disp

    def _overtake_goal(self, x_lane: float) -> float:
        """锁定侧的贴邻目标：σ 在选中拍按自车位置定死（第七轮裁定）——本车道
        前车回避不得横穿车体；邻道车（自车总在中轴侧）与旧 hug_goal 同值。
        σ 缺失（理论不可达，防御）退回 ref=0 旧语义。"""
        if self._hug_sigma is None:
            return self.scorer.hug_goal(x_lane)
        return x_lane + self._hug_sigma * self.cfg.overtake.d_hold_lane

    def _dodge_candidate(self, executed: float | None):
        """本车道前车紧急回避（第七轮 2029 局）：冷却/无候选盲持期间，同车道
        逼近的车是纯碰撞等待（near_dx 实测 0.00~0.04 道、pass d_min 0.23）。
        绕过评分与冷却强制起变道，目标=远离车体一侧的贴邻位；走廊仍过 veto
        （另一辆车占着回避侧就换下一辆）。回执缺位不判（横向几何纪律）。
        归 allow_overtake 闸管：V0/V1 兼容红线（关闸时行为逐拍不变）。"""
        if not self.cfg.mode.allow_overtake or executed is None or not self._views:
            return None
        hz = self.cfg.control.frame_rate_hz
        cands: list[tuple[float, CarView, float]] = []
        for v in self._views:
            if v.rel_approach <= _EPS:
                continue
            ttc = (self.cal.v_ego - v.cy) / (v.rel_approach * hz)
            if not (0.0 <= ttc < self.cfg.overtake.ttc_dodge_s):
                continue                      # 不到站/还在远处：正常机制管
            if abs(v.x_lane - executed) >= self.cfg.validate.lat_veto_gap_lane:
                continue                      # 车体不搭我道：不作回避对象
            goal = v.x_lane + _hug_sigma_of(executed, v.x_lane) * self.cfg.overtake.d_hold_lane
            if self.lateral.veto_reason(executed, goal, self._views) is not None:
                continue                      # 回避侧被占：换下一辆/下一侧
            cands.append((ttc, v, goal))
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        return cands[0][1], cands[0][2]

    def _side_has_space(self, x_lane: float) -> bool:
        """候选侧的路缘空间检查。左缘读数 left_edge_lane（负值，|·|=左缘距）、
        右缘 right_edge_lane。None=该侧无证据 → **不否决**（对称默认）。
        贴邻目标在候选的内侧（|hug|<|x|），但车本身占位，闸门按候选侧缘距判。"""
        b = None
        # boundary 摘要只在观测层有双侧稳定时给缘距；单侧 None 自动放行
        if self._boundary is not None:
            b = self._boundary
        edge = (b.right_edge_lane if x_lane >= 0 else b.left_edge_lane) \
            if b is not None else None
        if edge is None:
            return True
        need = self.cfg.overtake.d_hold_lane + self.cfg.overtake.space_margin_lane
        return abs(edge) >= need

    def _degraded(self, obs: WorldObservation) -> bool:
        """恢复判定：与致命信号同一合取的"健康面"——alive+fresh+presence 或过渡。"""
        h = obs.health
        return not (h.perception_alive and h.frame_fresh)

    def _enter_abort(self, why: str) -> None:
        self._state = DecisionState.ABORT_CHANGE
        self._change_t = 0.0
        self._abort_ref = None      # 速率参考从 ABORT 首拍重新起算
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
        需求≈0（原地起变道）时退化为 |executed| ≤ 死区。
        轨迹模式需求=滚动 d_target（路心系，_traj_last_dt）——executed 已是
        路心系回执，与 A1 系的 _demand_lane 不同参考系（阶段二 §2.3）。"""
        dz = self.cfg.hysteresis.dead_zone_lane
        if self.traj is not None and self._traj_last_dt is not None:
            d = self._traj_last_dt
        else:
            d = self._demand_lane
        if abs(d) <= dz:
            return abs(executed) <= dz
        return executed * d > 0 and abs(executed) >= abs(d) * (1 - 1e-6) - dz

    def _finish_change(self, why: str, reanchor: float | None = None,
                       cool_s: float | None = None) -> str:
        self._state = DecisionState.CRUISE
        # 完成也要冷却（§三：三收尾同权）；超车 pass 落定用短冷却（19:44 复盘：
        # pass 高频事件，一律 1s 烧掉 ~30% 对局时间且诱发左右横跳）
        self._cool_t = self.cfg.hysteresis.t_cool_s if cool_s is None else cool_s
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
                    return self._overtake_goal(v.x_lane)   # 时变轨迹（σ 锁定侧跟车）
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
