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
from maaracing_master.plugins.speedrush.corridor import UNKNOWN, Corridor
from maaracing_master.plugins.speedrush.corridor_reach import (
    LANE_HALF, BonusSource, CarSource, CoinSource, CorridorBandSpan,
    ReachParams, ReachReport, evaluate)
from maaracing_master.plugins.speedrush.depth_geo import (
    DrivableGrid, grid_center_lane)
from maaracing_master.plugins.speedrush.latsample import (
    LatTrajectorySampler, SampledTraj, eval_traj)
from maaracing_master.plugins.speedrush.traffic import (
    OUTCOME_PASS, OUTCOME_PRESUMED_PASS, CarView, PassEvent)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_BONUS, Calib, load_calib, t_meet_car_s, x_lane_of)
from maaracing_master.plugins.speedrush.plan import (
    KIND_CAND_BONUS, KIND_CAND_COIN, KIND_CAND_OVERTAKE, KIND_CAND_REACH,
    PlanSelector, Scored)

_SCHEMA = 2  # v2：DecisionOutput.reanchor_lane（planner 设计稿 §二，2026-09-22 用户裁定）
# 阶段 C step 2 **不再升 schema**：超车能力扩的是决策层内部（Scored/选择/完成判据分派）
# 与入参通道（traffic= 关键字，默认 None 旧调用零扰动），DecisionOutput 出口契约不变。
_EPS = 1e-9
# 逐拍重评的防抖连拍数（读数噪声量级的单拍破判不撕计划；与死区代理的
# _deadzone_ticks>=2 同款自卫）
_REEVAL_STREAK = 2

# KIND_CAND_* 与 Scored 已迁 plan.py（选择域住户，2026-10-05 计划层落地）；
# 本模块经 import 继续消费，旧引用面不变。


def _hug_sigma_of(ref: float, x_lane: float) -> float:
    """贴邻侧别：目标放在「ref 所在一侧、距目标 d_hold」处。ref 与目标同位
    （直接正后）退回旧内贴语义（σ=sign(x)）——两侧等价，取确定值即可。"""
    d = ref - x_lane
    if abs(d) <= _EPS:
        return 1.0 if x_lane >= 0 else -1.0
    return 1.0 if d > 0 else -1.0


@dataclass
class _Intent:
    """追逐意图（设计稿 §8.2 需求面承诺制：corridor_reach 集生命周期状态面，
    不新增 FSM 状态）。承诺的是「去 d0 所在的目的地带」而非某一组币——组会
    流过，带才是骑线对象（半宽=LANE_HALF，与归属半宽同源）。

    生灭：select:reach_win 换新、switch:reach_upgrade 整体替换；finish/abort
    保留（行为上不可达——所有读者都 gated 在 CHANGE+KIND_CAND_REACH，而
    reach 集必经 select 重建），done/cancel 拍 trace 取证用；conserve 硬清。
    计数器全部机检可复算（§8.4 证据列）：band_empty_s/pick_out_ticks=放弃门
    两条件、stay_ticks=次判据贴车序列、up_ticks=升级序列、absence_s=报告缺
    席时长、reanchors=重锚次数。"""

    d0: float                       # 目的地中心（路心系车道坐标，可重锚变换）
    basis_ids: frozenset[int]       # select 拍带内币组 id（审计起点，逐拍按带查）
    cs_center: float | None = None  # 走廊 center_lane 基线（重锚信号，道/车系）
    absence_s: float = 0.0
    band_empty_s: float = 0.0
    pick_out_ticks: int = 0
    stay_ticks: int = 0
    up_ticks: int = 0
    reanchors: int = 0


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
    min(视野, 该车到站时间) 内做。到站时间与评分同口径（Scorer.car_t_meet_s，
    2026-10-11 换基：构造时由引擎注入同一 callable，防第二份公式），
    rel≤0（不接近）→ 永不到站 → 全视野。

    **回执缺位不判**（veto_reason 收 None start）：几何检查不容假起点——
    无回执时自车真实位置未知，按 0 起算的包络是毒化判据；缺位=无此保护，
    与 v1 相同（不倒退也不臆造）。nan 目标（存续组）同跳过。"""

    def __init__(self, cfg: DecisionConfig, cal: Calib,
                 t_meet_fn=None):
        self.v = cfg.validate
        self.cal = cal
        self.v_lat_max = cfg.planner.v_lat_max
        self.hz = cfg.control.frame_rate_hz
        # 到站时间函数（引擎注入 Scorer.car_t_meet_s）；缺省=本类内旧行率式
        # （直构测试/旧装配的兼容退路，语义与旧实现逐位同形）
        self._t_meet = t_meet_fn if t_meet_fn is not None else self._t_pass_legacy
        # 车米制状态（V2 行为闸 metric_cars.fusion，引擎逐拍喂入的单线程
        # owner 实例通道，与 Scorer.fid_hz 同族纪律）：{track_id: MetricCarState}。
        # None=闸关/无供数，_t_pass_s 纯像素（与旧实现逐位同形）。
        self.metric_map: dict | None = None
        # 上一拍否决判定面快照（观测面，trace veto_cars 列）：参与否决的车与
        # 两判距——离线复核 veto 真伪用（20261016 缺口：此前 veto 拍无邻车几何
        # 可复核）。None=本拍调用未否决。
        self.last_veto_cars: list[dict] | None = None

    def _t_pass_legacy(self, rel: float, cy: float) -> float:
        rate = rel * self.hz
        return max(0.0, self.cal.v_ego - cy) / rate if rate > _EPS else math.inf

    def _t_pass_s(self, v: "CarView") -> float:
        """该车到自车行的秒数（与超车评分同一接近口径）；不接近 → 永不到站。
        米制融合（V2）：无锚/估计窗未开=像素；pacing（v_close≤0，米制口径
        永不到站）=全窗；两仪器都有正到站取**较晚**者——危险窗截断的物理口径
        是「过自车行才吹不到」，截断只许更晚不许更早（不因单一仪器低估而提前
        放行；早期截断=把还在逼近段的车当已错身， Unsafe 方向）。"""
        t = self._t_meet(v.rel_approach, v.cy)
        mm = self.metric_map
        if mm is None:
            return t
        m = mm.get(v.id)
        if m is None or m.v_close_mps is None:
            return t
        if m.t_meet_s is None:
            return math.inf
        return max(m.t_meet_s, t)

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
        self.last_veto_cars = None          # 每拍清账：本拍没否决=无快照
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
                # 判定面快照（观测面）：该车到站时间/两判距入 trace，离线复核
                tp = self._t_pass_s(v)
                self.last_veto_cars = [{
                    "id": v.id, "x": round(v.x_lane, 2),
                    "t_pass": round(tp, 2) if math.isfinite(tp) else None,
                    "gap": round(gap, 2),
                    "cur": round(abs(start - v.x_lane), 2)}]
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

    def score_car(self, view: CarView) -> Scored | None:
        """超车候选评分（阶段 C §二）：E(超车+极限期望)×时间折扣 − 内贴横向代价。

        候选带/接近方向自卫见 §四：本车道车（|x|<band_lo）与掠过中（<下沿）不作候选；
        d̂_min<窗下沿的"更贴"不奖励——窗中心是唯一目标（硬地板语义，设计稿 §〇.1）。
        带门/贴位目标吃 cand_x（成员位优先）；到站时间吃地面流积分（三件套①）。"""
        return self._score_car_reason(view)[0]

    def _score_car_reason(self, view: CarView) -> tuple[Scored | None, str | None]:
        """score_car 带拒绝原因形态（V0 门账本消费）：账本读拒绝面但不能有
        第二套门公式——本方法是唯一判定处，score_car 只是它的无原因透传。
        reason ∈ {"band", "approach"}；None=出了 Scored。"""
        x = self.cand_x(view)
        ax = abs(x)
        if not (self.ov.lane_band_lo <= ax < self.ov.lane_band_hi) \
                or view.rel_approach <= _EPS:
            return None, ("band" if not (self.ov.lane_band_lo <= ax
                                         < self.ov.lane_band_hi) else "approach")
        t_meet = self.car_t_meet_s(view.rel_approach, view.cy)
        life = math.exp(-t_meet / self.c.life_tau_s)
        goal = self.hug_goal(x)
        score = self._e_overtake * life \
            - self.c.shift_cost_per_lane * abs(goal)
        return Scored(group=None, score=score, t_miss_s=t_meet,
                      kind=KIND_CAND_OVERTAKE, car=view), None

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
        # 横向 veto 的到站时间与评分同口径（三件套①换基）：注入同一 callable，
        # 危险窗截断不吃第二份公式
        self.lateral = LateralSafety(cfg, self.cal,
                                     t_meet_fn=self.scorer.car_t_meet_s)
        # V0 逐候选门账本（本计划 §4）：_select 每拍重建，tick 入口清旧账；
        # trace 经 gate_ledger 属性消费，None=本拍未跑选择
        self._gate_ledger: dict | None = None
        # 计划层选择门（设计稿 v2；闸默认关=None=legacy _select 逐位不变）：
        # bonus＞极限超车＞coin 字典序 + CHANGE 改判慢拍+持续性门（plan.py）
        self._plan_sel = (PlanSelector(cfg, self.scorer, self.lateral)
                          if cfg.plan_layer.enabled else None)
        # 轨迹采样器（阶段二 §2.3；闸默认关=None=现役栈逐拍不变）：参数复用
        # 既有安全/运动面——横向可达域=v_lat_max、危险窗=lat_veto_horizon_s、
        # 接触界=lat_veto_gap_lane，与 LateralSafety 同源不另立真值。
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
        self._metric_by_id: dict = {}    # 逐车米制索引（fusion 消费面）
        # 走廊收益场参数面（corridor_reach 闸，2026-10-14 设计稿 §3.2-§3.4）：
        # enabled/control 任一开才构造（车道换算件在边界注入，本类内无车道
        # 单位）；shadow 落 trace，control 同拍消费 select/轨迹目标
        self._reach_params = self._build_reach_params() \
            if (cfg.corridor_reach.enabled or cfg.corridor_reach.control) \
            else None
        self.reset()

    def reset(self) -> None:
        """阶段切换/保守态出口/接线重入（重置面见 §二：全清，id 语义归上游）。"""
        self.watch.reset()
        if self._plan_sel is not None:
            self._plan_sel.reset()       # 计划承诺不跨阶段/不跨恢复
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
        self._blocked_s = 0.0                  # 承诺制：单次连续阻塞时长（恢复/新集清零）
        self._last_disp: float | None = None   # 超车逃逸判据的散布基线（select/switch 重置）
        self._hug_sigma: float | None = None   # 贴邻侧别（选中拍按自车位置锁定，第七轮）
        self._dt_last: float = 0.05            # 上一拍 dt（计划层慢拍计时基；reset 兜底值）
        self._fault = False
        self._last_report_ok = True
        self._reanchor_pending: float | None = None
        # 轨迹模式状态（阶段二）：初态速度来自执行回执差分 EMA（回执缺位 0）；
        # _traj_last_dt=上一拍轨迹目标（组转存续态保持，与 _current_goal 同语义）
        self._prev_exec: float | None = None
        self._vd = 0.0
        self._traj_prev = None                 # 上一拍中选轨迹（调试/续接面）
        self._traj_last_dt: float | None = None
        self._ro: float | None = None
        self._rwidth: float | None = None
        self._grid: DrivableGrid | None = None
        # hold 回中锚的栅格（与 _grid 分开存：_grid 受 grid_veto 闸，锚是
        # 独立消费者，不跟 veto 闸联动）
        self._grid_hold: DrivableGrid | None = None
        self._corridor: Corridor | None = None
        self._traj_diag: dict | None = None   # 逐拍采样诊断（trace 消费）
        # 走廊收益场逐拍态（corridor_reach 闸）：报告/目标跨阶段清——参数面
        # 不跨阶段（__init__ 构造，reset 不重建）
        self._reach_report: ReachReport | None = None
        self._reach_goal: float | None = None
        # 需求面承诺制（§8）：追逐意图随集生灭（纪律见 _Intent），_intent_hold
        # 是单拍冻结声明（缺席/gap 持位），update() 输出段读后即清
        self._intent: _Intent | None = None
        self._intent_hold = False

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
        ``corridor``（第 6 步）：同拍分带走廊，仅侧别空间检查消费
        （_side_has_space 按带来源规则）；None=无证据，对称默认不否决。"""
        self._dt_last = dt_s               # 计划层慢拍计时基（selector 累计）
        self.lateral.last_veto_cars = None # 每拍清账：快照只反映本拍判定
        self._views, self._events = traffic if traffic is not None else ((), ())
        # 车米制状态实例通道（C 线 2026-10-12，闸 metric_cars.enabled）：道评分
        # 与 ledger 米制列消费；行为路径不读它（V2 才换仪器）。闸关=空（回退路径）。
        self._metric_cars = obs.metric_cars if self.cfg.metric_cars.enabled else ()
        # V2 行为面（fusion 闸）：逐车米制索引供 dodge ttc 融合；LateralSafety
        # 危险窗截断吃同源通道（None=闸关，纯像素逐位不变）
        self._metric_by_id = {m.target_id: m for m in self._metric_cars}
        _metric_map = self._metric_by_id if self.cfg.metric_cars.fusion else None
        self.lateral.metric_map = _metric_map
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
        self._gate_ledger = None           # 每拍清账：本拍没跑 _select=无账，不读旧账
        # 走廊收益评定（corridor_reach 闸开才算；**先于状态分派**——_tick_cruise
        # 收益场选择同拍消费不隔拍，shadow 列同拍落）
        self._reach_report = (self._reach_evaluate(obs, executed_lane)
                              if self._reach_params is not None else None)
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
        # 需求面承诺制（§8.4.4/§8.4.2）：意图持位拍（缺席记忆/gap 等下一组）
        # 不采样轨迹（滚动目标是塌回点，采样即执行过期方向），x_target 冻结
        # executed——与 Track 1 暂停同一条输出通道（绕开平滑器防漂移）。
        intent_hold = self._intent_hold
        self._intent_hold = False           # 单拍声明，读后即清
        xt = None
        pause_hold = None
        if intent_hold and executed_lane is not None:
            pause_hold = executed_lane
        self._traj_diag = None
        if self.traj is not None and executed_lane is not None \
                and self._ro is not None \
                and not intent_hold \
                and self._state in (DecisionState.CRUISE, DecisionState.CHANGE):
            d_t = self._traj_d_target(obs, executed_lane)
            if d_t is not None:
                rep = self.traj.update(
                    d0=executed_lane, vd0=self._vd, ad0=0.0, d_target=d_t,
                    cars=self._views, width=self._rwidth, ro=self._ro,
                    hz=self.cfg.control.frame_rate_hz,
                    grid=self._grid, lane_w_m=self.cal.lane_w_m)
                # 逐拍采样诊断（S2-B 验收观测面）：否决分项与中选终点——
                # trace 消费，无此列则实机否决频次/ABORT 增量不可验
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
                if not rep.ok and self._state is DecisionState.CHANGE:
                    self._late_streak += 1
                    if self._late_streak >= _REEVAL_STREAK:
                        # 承诺制 Track 1（设计稿 v2 §3）：预算内暂停不弃追，
                        # 恢复优先（rep.ok 拍走上面分支自然续跑并清零重计）；
                        # 闸关/超预算=旧行为立即作废。
                        pause_hold = self._commitment_pause(dt_s, executed_lane)
                        if pause_hold is None:
                            self._enter_abort("traj_blocked")
                            reason = "cancel:traj_blocked"
                        else:
                            reason = "pause:blocked"
                elif rep.ok and self._state is DecisionState.CHANGE:
                    self._late_streak = 0
                    self._blocked_s = 0.0

        goal = self._current_goal(obs)
        alpha = 1.0 - math.exp(-dt_s / self.cfg.hysteresis.tau_filter_s)
        if pause_hold is not None:
            # 暂停拍冻结横位（设计稿 v2 §3.3 生效点）：x_target 直通 executed
            # 绕开平滑器——CHANGE 平滑器仍朝旧 goal 收敛，会顶着阻塞继续挪；
            # _x_smooth 同步到冻结位，恢复拍无跳变。
            self._x_smooth = pause_hold
        else:
            self._x_smooth += alpha * ((xt if xt is not None else goal)
                                       - self._x_smooth)
        x_out = pause_hold if pause_hold is not None \
            else (xt if xt is not None else self._x_smooth)
        move_allowed = self._state in (DecisionState.CRUISE, DecisionState.CHANGE)
        if not self.cfg.mode.allow_all_moves:
            move_allowed = False
        reanchor = self._reanchor_pending
        self._reanchor_pending = None      # 一次性：只在完成拍携带
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state, target_id=self._target_gid,
            x_target=x_out if self._state is not DecisionState.CONSERVE else None,
            move_allowed=move_allowed, reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks(),
            reanchor_lane=reanchor)

    @property
    def traj_diag(self) -> dict | None:
        """上一拍轨迹采样诊断（S2-B 验收观测面，trace 消费；None=非轨迹拍）：
        ok/why/n_eval/否决分项（n_grid 栅格/n_col 街车/n_reach 可达域/n_bound
        边界）/中选 [d1, T]。"""
        return self._traj_diag

    @property
    def gate_ledger(self) -> dict | None:
        """本拍逐候选门账本（V0 账本，V0 决策门账本 5071c02；
        trace 消费）：
        {"v": 2, "fid_hz": 采集帧率估计|None, "cars": [...], "coins": [...],
        "bonus": [...]}——
        cars 行 why ∈ {done_pass, band, approach, score, feas, veto, side,
        None=过全门}，带 x（单拍读数）/x_memb（车道成员位|None，三件套②）/
        cy/rel/age/w_m（TrafficObserver 有效性面产物，三件套③，过期转
        unknown 不落列）+w_m_age/w_px/conf+米制列（C 线 2026-10-12，闸
        metric_cars：z=米制距离 m、x_mtr=米制横位 m、vcls=接近速度 m/s、
        t_mtr=米制到站 s——None 且有 vcls=pacing 占用，None 且无 vcls=估计窗
        未开回退像素；far 轨不落行，其米制态只进道评分）；
        coins 行 why ∈ {conf, score, feas, veto, None}；bonus 行只落原料
        （x/cy/rel/w/conf/age，legacy 路不评 bonus）。
        None=本拍未跑选择（CONSERVE/ABORT/致命/冷却拍），不留旧账。"""
        return self._gate_ledger

    @property
    def traj_plan(self) -> "SampledTraj | None":
        """上一拍中选轨迹本体（调试图渲染面；coeffs 体积大，不进 trace）：
        d1/T/cost/coeffs，None=非轨迹拍或全灭。只读透传，消费方不得改。"""
        return self._traj_prev

    @property
    def target_kind(self) -> str | None:
        """当前目标类（trace 消费；计划层 V1 验收面：bonus/overtake/coin 的
        候选分布与切换频率从这列读）。"""
        return self._target_kind

    def _traj_d_target(self, obs: WorldObservation,
                       executed: float) -> float | None:
        """轨迹目标（路心系，道）：金币=ro+组心、超车=ro+锁定侧贴邻位、
        巡航=道收益持守道（V2 control 闸）或 0（守轴旧语义）。组转存续态
        （nan/缺位）保持上一拍目标——与 _current_goal 的"不外插 nan"同语义；
        无处可保持才 None（降级）。"""
        if self.cfg.corridor_reach.control:
            # 走廊收益场：CRUISE 持守与 CHANGE 移动都吃路径近带点（x_goal 每拍
            # 滚动重评，米制→道换算在边界）；报告缺席=诚实降级 None（轨迹链回
            # legacy 输出形成，不臆造路径）
            return (self._reach_goal / self.cal.lane_w_m
                    if self._reach_goal is not None else None)
        if self._state is DecisionState.CHANGE:
            if self._target_kind in (KIND_CAND_OVERTAKE, KIND_CAND_BONUS):
                v = self._car_target_of(obs, self._target_gid)
                if v is not None:
                    goal = (self._overtake_goal(self.scorer.cand_x(v))
                            if self._target_kind == KIND_CAND_OVERTAKE
                            else v.x_lane)   # bonus=撞上去：目标车体位
                    return self._ro + goal
                return self._traj_last_dt
            g = self._group_of(obs)
            if g is not None and not math.isnan(g.x_center):
                return self._ro + g.x_center
            return self._traj_last_dt
        return 0.0

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

    def _tick_cruise_reach(self, obs: WorldObservation,
                           executed: float | None) -> str:
        """CRUISE 走廊收益场选择（corridor_reach.control 闸，设计稿 §3.3）：
        胜出 x_goal 与 executed 差超死区 → CHANGE（select:reach_win）；否则
        持守（hold:reach_hold），轨迹目标吃路径近带点。报告缺席拍（无路心锚/
        走廊缺席/黑视）持守不翻面——不臆造可达性。需求面承诺制（§8.2）：选中
        拍锁定追逐意图（d0=本场选点，basis=带内币组，cs_center=重锚基线）。"""
        rep = self._reach_report
        if rep is None:
            self._reach_goal = None
            return "hold:reach_hold"
        self._reach_goal = rep.best_x_m
        goal_lane = rep.best_x_m / self.cal.lane_w_m
        if self._cool_t > 0:
            # 冷却中不发起新承诺（20261016 接线修复：_finish_change/_tick_abort
            # 落定时设的 t_cool_s 在 legacy 链有 hold:cooling 消费，reach 换血时
            # 漏接——veto/完成后 0.2s 即重选，邻车来不及演变，P2 实测 veto 18 次
            # 聚成 5 簇）。持守语义不变：轨迹目标仍吃场选点（traj 碰撞采样护航，
            # 车流挡路时自然回退路心等待），冷却只管「承诺」不管「缓移」。
            return "hold:cooling"
        if executed is None or \
                abs(goal_lane - executed) <= \
                self.cfg.corridor_reach.trigger_dead_zone_lane:
            return "hold:reach_hold"
        self._target_gid = None                 # 走廊路径位目标，无实体 gid
        self._target_kind = KIND_CAND_REACH
        self._cur_score = max(rep.candidates,
                              key=lambda c: c.score).score
        self._hug_sigma = None
        self._demand_lane = goal_lane
        self._late_streak = 0
        self._blocked_s = 0.0
        self._last_disp = None
        self._change_t = 0.0
        self._deadzone_ticks = 0
        self._intent = (_Intent(
            d0=goal_lane,
            basis_ids=self._band_coin_ids(obs, goal_lane),
            cs_center=None if self._corridor is None
            else self._corridor.center_lane)
            if self.cfg.commitment.demand_enabled else None)
        self._state = DecisionState.CHANGE
        return "select:reach_win"

    def _tick_change_reach(self, obs: WorldObservation, dt_s: float,
                           executed: float | None) -> str:
        """走廊收益场计划路（corridor_reach.control 闸，设计稿 §3.3）：目标=
        走廊路径位（非实体）。报告缺席：承诺制 §8.4.4 预算内记忆续跑（保留
        意图、x_target 冻结 executed），超预算/闸关/executed 缺席=既有取消。
        demand_enabled（§8）：完成/放弃判据吃受管意图而非滚动选点——逐拍
        优先级=重锚断裂 > 依据死亡 > 完成（主/次判据） > 安全 veto > gap
        持位 > 升级；x_goal 仍每拍滚动重评（运动与观测归场，承诺只接管
        「何谓到达」与「何时放弃」）。横向安全 veto 原样，意图不豁免。"""
        self._change_t += dt_s
        # 重锚先行于报告判定（20261016 A/B 教训：收益报告缺席 ≠ 坐标系停表
        # ——缺席期 center 变化无人跟踪，恢复拍跨基线一次性比对会误判
        # frame_break；信号源=corridor.center_lane，与收益报告无关）
        if self._intent is not None:
            blocked = self._intent_reanchor()
            if blocked is not None:
                return blocked
        rep = self._reach_report
        if rep is None:
            if (self.cfg.commitment.memory_enabled
                    and self._intent is not None and executed is not None):
                self._intent.absence_s += dt_s
                if self._intent.absence_s > \
                        self.cfg.commitment.memory_budget_s:
                    self._enter_abort("target_lost")
                    return "cancel:reach_lost"
                self._intent_hold = True   # 缺席拍冻结（Track 1 §3.3 同语义）
                return "hold:reach_absent"
            self._enter_abort("target_lost")
            return "cancel:reach_lost"
        goal_lane = rep.best_x_m / self.cal.lane_w_m
        self._reach_goal = rep.best_x_m
        if self._intent is not None:
            blocked = self._intent_basis(obs, rep, goal_lane, dt_s,
                                         executed)
            if blocked is not None:
                return blocked
            self._demand_lane = self._intent.d0   # 意图镜面（降级/trace 消费）
        else:
            self._demand_lane = goal_lane
        if executed is not None:
            if self._intent is not None:
                # 主判据（§8.3）：对齐 d0——d0 是有受管生命周期的意图状态，
                # 滚动选点不再参与完成判定（坍缩假到达的根治点）
                if abs(executed - self._intent.d0) <= \
                        self.cfg.hysteresis.dead_zone_lane:
                    return self._finish_change("converged:feedback",
                                               reanchor=self._intent.d0)
                # 次判据（场自己说「留在这」）：贴车 k_stay 序列 + 近旁币组
                # 实证（收益在场，不是缺席）；守门：带内依据仍活且场选点已
                # 离带 → 不是留驻是坍缩（fid2348 形态由此拦截）
                if (self._intent.stay_ticks >= self.cfg.commitment.k_stay
                        and self._near_car_coin(obs, executed) is not None
                        and not (self._intent.band_empty_s <= 0.0
                                 and abs(goal_lane - self._intent.d0)
                                 > LANE_HALF)):
                    return self._finish_change("converged:proxy",
                                               reanchor=self._intent.d0)
            elif self._demand_consumed(executed):
                return self._finish_change("converged:feedback",
                                           reanchor=goal_lane)
            veto = self.lateral.veto_reason(executed, goal_lane, self._views)
            if veto is not None:
                self._enter_abort(veto)
                return f"cancel:{veto}"
            # 超时出口（20261016 A/B 补既有缺口：t_change_max_s 的检查原本
            # 只在 executed 缺席的降级分支——旧判据集 2s 内必收敛用不着；
            # 意图生命周期把集拉长后（空窗持位 P1 实测 25.1s/30s），executed
            # 在场的集必须同享超时强制落位，否则持位无期）
            if self._change_t >= self.cfg.control.t_change_max_s:
                return self._finish_change(
                    "change_timeout",
                    reanchor=self._intent.d0 if self._intent is not None
                    else self._demand_lane)
            if self._intent is not None:
                # gap 持位（§8.4.2）：空窗期场选点塌回路心是反骑线机理——
                # 意图保留、车持位等下一组（冻结走 _intent_hold 输出通道）
                if self._intent.band_empty_s > 0.0:
                    self._intent_hold = True
                    return "hold:reach_gap"
                # 升级（§8.4.3）：带外新目的地连续 k_up 拍超在场带候选
                # tie_margin——换目标≠放弃，整体替换意图继续 CHANGE
                if self._intent.up_ticks >= self.cfg.commitment.k_up:
                    return self._intent_upgrade(rep, goal_lane, obs)
            return "moving:reach"
        # 无反馈降级：目标连读 2 拍进死区视为收敛，超时强制落位（金币分支同款）
        if abs(rep.best_x_m) / self.cal.lane_w_m \
                <= self.cfg.hysteresis.dead_zone_lane:
            self._deadzone_ticks += 1
            if self._deadzone_ticks >= 2:
                return self._finish_change("converged:proxy",
                                           reanchor=self._demand_lane)
        else:
            self._deadzone_ticks = 0
        if self._change_t >= self.cfg.control.t_change_max_s:
            return self._finish_change("change_timeout",
                                       reanchor=self._demand_lane)
        return "moving:reach"

    def _intent_reanchor(self) -> str | None:
        """坐标系重锚（§8.3，评审边界 A）：corridor.center_lane 相邻拍跳变超
        reanchor_eps 即换版，d0'=d0−Δc——executed 侧由回执链同帧平移，变换
        保持相对需求（=物理指向）不变；超 reanchor_break 判坐标系断裂 →
        有证据放弃（cancel:frame_break），不无条件追逐过期坐标。center 缺席
        拍重置基线：跨观测缺口不臆造跳变。返回非 None=本拍已被拦截收场。"""
        it = self._intent
        c_now = (self._corridor.center_lane
                 if self._corridor is not None else None)
        if c_now is None or it.cs_center is None:
            it.cs_center = c_now
            return None
        dc, it.cs_center = c_now - it.cs_center, c_now
        if abs(dc) <= self.cfg.commitment.reanchor_eps_lane:
            return None
        if abs(dc) > self.cfg.commitment.reanchor_break_lane:
            self._enter_abort("frame_break")
            return "cancel:frame_break"
        it.d0 -= dc
        it.reanchors += 1
        return None

    def _intent_basis(self, obs: WorldObservation, rep: ReachReport,
                      goal_lane: float, dt_s: float,
                      executed: float | None) -> str | None:
        """收益依据逐拍维护（§8.4.2，评审边界 B，全部机检可复算）：带内可见
        性计时 band_empty_s（>0=gap 态：空窗骑线语义，意图保留车持位）、场
        选点带外连续拍 pick_out_ticks；两者同时成立才判死（任一恢复计数归零
        即回 alive）——短暂看不到 ≠ 不存在，场偶发选偏 ≠ 弃追。stay/up 序列
        一并更新（次判据与升级门的连续性证据）。返回非 None=本拍已判死。"""
        it = self._intent
        c = self.cfg.commitment
        it.absence_s = 0.0            # 报告在场=无缺席（§8.4.4 恢复拍清零）
        if self._band_coin_ids(obs, it.d0):
            it.band_empty_s = 0.0
        else:
            it.band_empty_s += dt_s
        if abs(goal_lane - it.d0) <= LANE_HALF:
            it.pick_out_ticks = 0
        else:
            it.pick_out_ticks += 1
        if executed is not None:
            if abs(goal_lane - executed) <= \
                    self.cfg.hysteresis.dead_zone_lane:
                it.stay_ticks += 1
            else:
                it.stay_ticks = 0
            inband = [cd.score for cd in rep.candidates
                      if abs(cd.x_m / self.cal.lane_w_m - it.d0) <= LANE_HALF]
            if (inband
                    and max(cd.score for cd in rep.candidates)
                    >= max(inband) + self.cfg.corridor_reach.tie_margin
                    and abs(goal_lane - it.d0) > LANE_HALF):
                it.up_ticks += 1
            else:
                it.up_ticks = 0
        else:
            it.stay_ticks = 0     # 无回执拍不积累贴车证据（无观测无序列）
        if it.band_empty_s >= c.basis_grace_s \
                and it.pick_out_ticks >= c.k_out:
            self._enter_abort("basis_dead")
            return "cancel:basis_dead"
        return None

    def _intent_upgrade(self, rep: ReachReport, goal_lane: float,
                        obs: WorldObservation) -> str:
        """目标升级（§8.4.3）：d0/带/basis 整体替换、计数器随新承诺清零。
        协议四要素（同拍同场评分、连续 k_up、差 ≥ tie_margin、带变化）已在
        _intent_basis 的 up_ticks 序列里逐拍机检，此处只做替换与集簿记
        （switch 位同款纪律：计时/防抖/位移基线全部重起）。"""
        old = self._intent
        self._intent = _Intent(
            d0=goal_lane,
            basis_ids=self._band_coin_ids(obs, goal_lane),
            cs_center=old.cs_center,      # 坐标系连续：重锚基线与次数顺延
            reanchors=old.reanchors)
        self._cur_score = max(rep.candidates,
                              key=lambda c: c.score).score
        self._demand_lane = goal_lane
        self._change_t = 0.0
        self._deadzone_ticks = 0
        self._last_disp = None
        self._late_streak = 0
        self._blocked_s = 0.0
        return "switch:reach_upgrade"

    def _band_coin_ids(self, obs: WorldObservation,
                       d0: float) -> frozenset[int]:
        """目的地带（d0±LANE_HALF）内可见币组 id（可见性来源=obs.coin_groups
        位置，与场评分无关——依据在不在场看位置，值不值钱看场；nan 心存续组
        不入账）。"""
        return frozenset(
            g.group_id for g in obs.coin_groups
            if g.x_center == g.x_center and abs(g.x_center - d0) <= LANE_HALF)

    def _near_car_coin(self, obs: WorldObservation,
                       executed: float) -> int | None:
        """自车近旁（±LANE_HALF）可见币组 id（次判据的「收益在场」实证）。"""
        for g in obs.coin_groups:
            if g.x_center == g.x_center \
                    and abs(g.x_center - executed) <= LANE_HALF:
                return g.group_id
        return None

    @property
    def reach_report(self) -> "ReachReport | None":
        """上一拍走廊收益评定（corridor_reach 闸开才有值；trace cr_* 对照列
        的数据源，不进决策——行为切换在 control 闸）。None=闸关/弃权拍。"""
        return self._reach_report

    @property
    def veto_cars(self) -> list[dict] | None:
        """上一拍 veto 判定的参与车快照（观测面，trace veto_cars 列数据源；
        None=本拍无 veto 判定）。"""
        return self.lateral.last_veto_cars

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

    @property
    def intent_state(self) -> dict | None:
        """当前追逐意图观测面（§8.2；trace intent 列数据源；None=闸关/
        demand 关/集外无意图）。计数器与转移全部机检可复算（§8.4 证据列）。"""
        it = self._intent
        if it is None or not self.cfg.commitment.demand_enabled:
            return None
        return {"d0": round(it.d0, 3), "basis_ids": sorted(it.basis_ids),
                "absence_s": round(it.absence_s, 3),
                "band_empty_s": round(it.band_empty_s, 3),
                "pick_out": it.pick_out_ticks, "stay": it.stay_ticks,
                "up": it.up_ticks, "reanchors": it.reanchors,
                "basis": "gap" if it.band_empty_s > 0.0 else "alive"}

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
        if self.cfg.corridor_reach.control:
            # 走廊收益场选择（corridor_reach.control 闸）：换血 legacy band/feas
            # 选择链——收益场即选择器，金币/错身/碰撞以收益形式进入候选计分
            return self._tick_cruise_reach(obs, executed)
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
        self._blocked_s = 0.0
        self._last_disp = None
        self._hug_sigma = (_hug_sigma_of(executed if executed is not None else 0.0,
                                         cand.car.x_lane)
                           if cand.kind == KIND_CAND_OVERTAKE else None)
        # 有符号需求（planner §二：反向不得过门）；超车候选的需求=选中拍锁定侧的
        # 贴邻目标，bonus=目标车体位（撞上去，维护者裁定）；超车完成走 pass 事件
        # 不走 demand 门（§四换轴），demand 只服务回执/trace
        self._demand_lane = (cand.group.x_center if cand.kind == KIND_CAND_COIN
                             else cand.car.x_lane if cand.kind == KIND_CAND_BONUS
                             else self._overtake_goal(cand.car.x_lane))
        self._change_t = 0.0
        self._deadzone_ticks = 0
        self._state = DecisionState.CHANGE
        return "select:score_win"

    def _tick_change(self, obs: WorldObservation, dt_s: float,
                     executed: float | None) -> str:
        if self._target_kind == KIND_CAND_REACH:
            return self._tick_change_reach(obs, dt_s, executed)
        if self._target_kind in (KIND_CAND_OVERTAKE, KIND_CAND_BONUS):
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
                (better.kind, better.cand_id) != (self._target_kind,
                                                  self._target_gid):
            self._target_gid = better.cand_id
            self._target_kind = better.kind
            self._cur_score = better.score
            self._late_streak = 0
            self._blocked_s = 0.0
            self._last_disp = None   # 新目标新基线
            self._hug_sigma = (_hug_sigma_of(executed if executed is not None else 0.0,
                                             better.car.x_lane)
                               if better.kind == KIND_CAND_OVERTAKE else None)
            self._demand_lane = (better.group.x_center
                                 if better.kind == KIND_CAND_COIN
                                 else better.car.x_lane
                                 if better.kind == KIND_CAND_BONUS
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
        v = self._car_target_of(obs, self._target_gid)
        if v is None:
            ev = next((e for e in self._events if e.track_id == self._target_gid), None)
            if ev is not None and ev.outcome in (OUTCOME_PASS,
                                                 OUTCOME_PRESUMED_PASS):
                self._done_pass_ids.add(ev.track_id)
                # reanchor=None：超车完成拍没有"目标读数"可锚（车已出画）——
                # executed_lane 走路中心连续重锚（step 2.5），不吃事件锚
                # presumed_pass 与 pass 同路径结算；trace reason 分开记，账本不混
                reason = ("overtake_pass" if ev.outcome == OUTCOME_PASS
                          else "overtake_pass_presumed")
                return self._finish_change(
                    reason, cool_s=self.cfg.overtake.t_cool_pass_s)
            self._enter_abort("target_lost")
            return "cancel:overtake_lost"
        if self._target_kind == KIND_CAND_BONUS and v.cy >= self.cal.v_ego:
            # bonus 兑现=几何接触判定（车到自车行=接触窗，设计稿 §四.3）：
            # 撞上去（维护者裁定）——出画不作成功证明，到行即兑现
            self._done_pass_ids.add(v.id)
            return self._finish_change("bonus_contact",
                                       cool_s=self.cfg.overtake.t_cool_pass_s)
        better = self._select(obs, current=self._cur_score, executed=executed)
        if better is not None and \
                (better.kind, better.cand_id) != (self._target_kind,
                                                  self._target_gid):
            self._target_gid = better.cand_id
            self._target_kind = better.kind
            self._cur_score = better.score
            self._late_streak = 0
            self._blocked_s = 0.0
            self._last_disp = None   # 新目标新基线
            self._hug_sigma = (_hug_sigma_of(executed if executed is not None else 0.0,
                                             better.car.x_lane)
                               if better.kind == KIND_CAND_OVERTAKE else None)
            self._demand_lane = (better.group.x_center
                                 if better.kind == KIND_CAND_COIN
                                 else better.car.x_lane
                                 if better.kind == KIND_CAND_BONUS
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

        计划层闸开时整条分流 plan.PlanSelector（三类字典序+持续性门，设计稿
        v2）：current 语义从「分数基准」换为「(kind,id) 身份」——分数已退出
        选择链；CRUISE（current=None）即时选，CHANGE 改判走慢拍+K 连续门。

        阶段一（2026-10-02）：need 从**执行回执**起算（need=|目标−executed|，
        替换自车瞬移假设——执行滞后中位 0.45/纸 p90 0.9 车道的肇因）；回执缺位
        维持 0 起算旧口径（v1 兼容红线）。回执在场时另过横向安全 veto（假起点
        不判几何）。"""
        if self._plan_sel is not None:
            if not self.cfg.mode.allow_all_moves:
                return None               # V0 直行门与 legacy 同位（红线不旁路）
            return self._plan_sel.select(
                obs, self._views, executed,
                current=(None if current is None
                         else (self._target_kind, self._target_gid)),
                cooling=self._cool_t > 0,
                done_pass_ids=self._done_pass_ids,
                side_has_space=self._side_has_space,
                dt_s=self._dt_last)
        if self._cool_t > 0 or not self.cfg.mode.allow_all_moves:
            return None
        start = executed if executed is not None else 0.0
        t = self.cfg.timing
        min_score = self.cfg.hysteresis.min_score
        # V0 门账本（V0 决策门账本 5071c02）：与下方选择共用
        # 同一批判定，账本行是判定的读数面而非第二套门——公式单一真源=
        # 本函数+Scorer/LateralSafety。只记账不改判。
        tgt = {x.id: x for x in obs.targets}
        cars_rows: list[dict] = []
        coins_rows: list[dict] = []
        bonus_rows = [
            {"id": x.id, "x": None if x.x_lane is None else round(x.x_lane, 3),
             "cy": x.cy, "rel": round(x.rel_approach, 2), "w_px": x.w,
             "conf": round(x.conf, 2), "age": obs.frame_id - x.first_seen_fid}
            for x in obs.targets if x.kind == KIND_BONUS]
        best: Scored | None = None
        for g in obs.coin_groups:
            s = self.scorer.score(g, obs)
            row: dict = {"id": g.group_id, "x": round(g.x_center, 3),
                         "conf": round(g.conf_min, 3), "n": g.observed_count,
                         "score": None if s is None else round(s.score, 2),
                         "t_miss": None if s is None
                         or not math.isfinite(s.t_miss_s)
                         else round(s.t_miss_s, 2), "why": None}
            if s is None:
                row["why"] = "conf"
            else:
                ok = s.score >= min_score
                need = t.lane_change_duration_s(g.x_center - start)
                feas = not (need + t.tau_resp_s + t.margin_s >= s.t_miss_s)
                veto = None if executed is None else \
                    self.lateral.veto_reason(executed, g.x_center, self._views)
                row["feas"] = feas
                row["veto"] = veto
                if not ok:
                    row["why"] = "score"
                elif not feas:
                    row["why"] = "feas"
                elif veto is not None:
                    row["why"] = "veto"
            coins_rows.append(row)
            if s is None or s.score < min_score:
                continue
            # 时机：从执行位出发的变道耗时 + 响应延迟 + 余量 < 错过时间
            need = t.lane_change_duration_s(g.x_center - start)
            if need + t.tau_resp_s + t.margin_s >= s.t_miss_s:
                continue
            if executed is not None and \
                    self.lateral.veto_reason(executed, g.x_center, self._views):
                continue
            if best is None or s.score > best.score:
                best = s
        if self.cfg.mode.allow_overtake:
            metric_by_id = {m.target_id: m for m in self._metric_cars}
            for v in self._views:
                s, why = self.scorer._score_car_reason(v)
                memb = getattr(v, "x_memb", None)
                row = {"id": v.id, "x": round(v.x_lane, 3),
                       "x_memb": None if memb is None else round(memb, 3),
                       "cy": v.cy, "rel": round(v.rel_approach, 2),
                       "age": v.age_ticks,
                       "why": "done_pass" if v.id in self._done_pass_ids
                       else why}
                m = metric_by_id.get(v.id)
                if m is not None and m.z_m is not None:
                    # 米制列（C 线影子面）：z/vcls/t_mtr 齿对齐 MetricCarState；
                    # t_mtr=None 且 vcls≤0=pacing 占用语义，t_mtr=None 且无
                    # vcls=估计窗未开（消费端回退像素，ledger 顶层的像素 t_meet
                    # 列即是）。far 轨不在本循环（只遍历 _views），其米制态
                    # 只进道评分——ledger 缺它属已知盲区，验收面用道评分列看。
                    row["z"] = round(m.z_m, 1)
                    if m.x_m is not None:
                        row["x_mtr"] = round(m.x_m, 2)
                    if m.v_close_mps is not None:
                        row["vcls"] = round(m.v_close_mps, 2)
                    row["t_mtr"] = None if m.t_meet_s is None \
                        else round(m.t_meet_s, 2)
                w_m = getattr(v, "w_m", None)
                if w_m is not None:
                    # 宽度走 TrafficObserver 有效性面（年龄/置信门+TTL，三件套③）：
                    # 过期已显式转 None 不冒充有效；带龄标记供验收/下游消费
                    row["w_m"] = round(w_m, 3)
                    row["w_m_age"] = getattr(v, "w_m_age", None)
                tinfo = tgt.get(v.id)
                if tinfo is not None:
                    row["w_px"] = tinfo.w
                    row["conf"] = round(tinfo.conf, 2)
                if s is not None and row["why"] is None:
                    row["score"] = round(s.score, 2)
                    row["t_meet"] = round(s.t_miss_s, 2)
                    goal = self.scorer.hug_goal(self.scorer.cand_x(v), ref=start)
                    need = t.lane_change_duration_s(goal - start)
                    feas = not (need + t.tau_resp_s + t.margin_s >= s.t_miss_s)
                    veto = None if executed is None else \
                        self.lateral.veto_reason(executed, goal, self._views)
                    side = self._side_has_space(self.scorer.cand_x(v))
                    row["feas"] = feas
                    row["veto"] = veto
                    row["side"] = side
                    if s.score < min_score:
                        row["why"] = "score"
                    elif not feas:
                        row["why"] = "feas"
                    elif veto is not None:
                        row["why"] = "veto"
                    elif not side:
                        row["why"] = "side"
                cars_rows.append(row)
                if v.id in self._done_pass_ids:
                    continue
                if s is None or s.score < min_score:
                    continue
                # 可行性门（19:44 复盘补装，与金币路同形）：从执行位到内贴目标的
                # 耗时+响应+余量必须 < 相对接近时间——"来不及贴"的车不追
                #（远距离白超 d_min 中位 1.11 的主犯；阶段一补执行回执口径）
                goal = self.scorer.hug_goal(self.scorer.cand_x(v), ref=start)
                need = t.lane_change_duration_s(goal - start)
                if (need + t.tau_resp_s
                        + t.margin_s) >= s.t_miss_s:
                    continue
                # 横向安全 veto（阶段一）：机动走廊撞外推街车 → 拦下，不进评分
                if executed is not None and \
                        self.lateral.veto_reason(executed, goal, self._views):
                    continue
                # 空间闸门（维护者裁定 2026-09-22）：决策默认左右对称，禁用某方向
                # 必须有"那侧没空间"的显式证据（缘距读数）；证据不足=两侧都放行。
                if not self._side_has_space(self.scorer.cand_x(v)):
                    continue
                if best is None or s.score > best.score:
                    best = s
        self._gate_ledger = {"v": 2, "fid_hz": self.scorer.fid_hz,
                             "cars": cars_rows, "coins": coins_rows,
                             "bonus": bonus_rows}
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
        归 allow_overtake 闸管：V0/V1 兼容红线（关闸时行为逐拍不变）。
        ttc 米制融合（V2，闸 metric_cars.fusion）：像素逼近门放宽为「任一
        仪器报逼近」——地平线退化带 rel 读数失真恰是米制仪器的治愈对象；
        到站取两仪器较小者（先到者先防）。"""
        if not self.cfg.mode.allow_overtake or executed is None or not self._views:
            return None
        fusion = self.cfg.metric_cars.fusion
        cands: list[tuple[float, CarView, float]] = []
        for v in self._views:
            m = self._metric_by_id.get(v.id) if fusion else None
            m_t = (m.t_meet_s if m is not None and m.v_close_mps is not None
                   and m.v_close_mps > 0 else None)
            if v.rel_approach <= _EPS and m_t is None:
                continue                # 两仪器都说不逼近：正常机制管
            pixel = self.scorer.car_t_meet_s(v.rel_approach, v.cy)
            ttc = min(m_t, pixel) if m_t is not None else pixel
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
        """候选侧的路缘空间检查（第 6 步走廊带来源规则）。

        近带（bands[0]，z 3~6m）该侧边界证据按来源判：measured/occluded/
        inferred 有边界位置都按「边界距 < 需求 → 否决」（墙永远赢：物理墙
        与先验空气墙都不许挤，occluded 用最近已知位置+保守，inferred 用先验
        空气墙+保守）；unknown（无证据带）保持对称默认**不否决**——旧
        None 不否决的默认只对"无证据"保留，不对"有证据"（接口统一不得成为
        绕过旧否决的理由）。贴邻目标在候选的内侧（|hug|<|x|），闸门按候选
        侧缘距判。走廊缺席（corridor=None）=旧对称默认。"""
        c = self._corridor
        if c is None or not c.bands:
            return True
        b = c.bands[0].left if x_lane < 0 else c.bands[0].right
        if b.src == UNKNOWN or b.x_m is None:
            return True
        need_m = (self.cfg.overtake.d_hold_lane
                  + self.cfg.overtake.space_margin_lane) * self.cal.lane_w_m
        return abs(b.x_m) >= need_m

    def _degraded(self, obs: WorldObservation) -> bool:
        """恢复判定：与致命信号同一合取的"健康面"——alive+fresh+presence 或过渡。"""
        h = obs.health
        return not (h.perception_alive and h.frame_fresh)

    def _commitment_pause(self, dt_s: float,
                          executed: float | None) -> float | None:
        """承诺制 Track 1（设计稿 v2 §3）：traj 连续破判达标后预算内暂停——
        返回冻结横位（executed）；None=不暂停（闸关/executed 缺席/超预算），
        走既有作废。预算=单次连续阻塞时长（dt_s 累计；恢复拍在 traj ok 分支
        清零，新集在 select/switch 位清零），跨过预算的阻塞拍作废——恢复优先
        （恢复拍根本不进本函数）。"""
        if not self.cfg.commitment.pause_enabled or executed is None:
            return None
        self._blocked_s += dt_s
        if self._blocked_s >= self.cfg.commitment.pause_budget_s:
            return None
        return executed

    def _enter_abort(self, why: str) -> None:
        self._state = DecisionState.ABORT_CHANGE
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

    def _car_target_of(self, obs: WorldObservation, gid: int):
        """超车/bonus 目标的当拍视图：车流 views（CarView）优先；bonus 不进
        车流账本（TrafficObserver 只收 kind==car），走 obs.targets 按 kind 取，
        宽限过期=None（lost 语义）。TrackedTarget 鸭子 CarView（x_lane/cy/
        rel_approach/id 同名）。"""
        v = next((x for x in self._views if x.id == gid), None)
        if v is not None:
            return v
        t = next((x for x in obs.targets
                  if x.kind == KIND_BONUS and x.id == gid), None)
        if t is None or t.validity_until_fid <= obs.frame_id:
            return None
        return t

    def _current_goal(self, obs: WorldObservation) -> float:
        if self._state in (DecisionState.CRUISE, DecisionState.CHANGE):
            if self._target_kind in (KIND_CAND_OVERTAKE, KIND_CAND_BONUS):
                v = self._car_target_of(obs, self._target_gid)
                if v is not None:
                    # 时变轨迹（σ 锁定侧跟车，成员位优先抗噪）；bonus=撞上去
                    return (self._overtake_goal(self.scorer.cand_x(v))
                            if self._target_kind == KIND_CAND_OVERTAKE
                            else v.x_lane)
                if self._state is DecisionState.CHANGE:
                    return self._x_smooth                    # 落定拍前不外插假读数
                return self._hold_center_goal()
            g = self._group_of(obs)
            if g is not None and not math.isnan(g.x_center):
                return g.x_center
            # 组转存续态（本帧无成员观测，x_center=nan）：CHANGE 中保持最后 goal
            # （= 当前平滑值），不外插 nan——回放抓到 nan 污染 x_smooth 的缺陷。
            if self._state is DecisionState.CHANGE:
                return self._x_smooth
        return self._hold_center_goal() if self._state is DecisionState.CRUISE \
            else 0.0

    def _hold_center_goal(self) -> float:
        """hold 回中锚（2026-10-15 币渠道/贴墙诊断）：无候选时不守走廊中心——
        pair_slot 车锚走廊「骑黄线报正中」（域内 gate note 实录），守中心=守
        原地，贴墙不回、币线在旁边也永远不横穿。改用栅格绿区路心（
        grid_center_lane，非车锚：绿区范围中点的宽度加权中位，与供数主人
        region_reading 同源栅格不同算法——只作回中目标与互证，不混作供数；
        离线基线 f9a9a80 验证）做回中目标；返回值是走廊系 goal（=绿区路心
        ego 道 − 走廊中心 ego 道）。栅格弃权/走廊缺席回退 0（旧行为逐位）。"""
        if self._grid_hold is None or self._corridor is None \
                or self._corridor.center_lane is None:
            return 0.0
        g = grid_center_lane(self._grid_hold, self.cal.lane_w_m)
        if g is None:
            return 0.0
        return g - self._corridor.center_lane

    def _validity_ticks(self) -> int:
        return max(2, int(round(0.1 * self.cfg.control.frame_rate_hz)))
