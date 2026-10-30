# -*- coding: utf-8 -*-
"""行为决策层（2026-10-18 走廊重建：四层字典序）。

**它是什么**：每 tick 吃 `WorldObservation`（+ tick 时长 + 执行回执 + 车流 +
走廊），把目标换算进选择层坐标，交给 `selection.select` 做严格分层：

    奖励车（撞） > 普通车（擦边） > 金币（落线） > 走廊中心带（带内不修正；
    无机会时由粗层选连续净空最长的车道）

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

from maaracing_master.plugins.speedrush.coarse import (
    CoarseParams, CoarseReport, choose_lane)
from maaracing_master.plugins.speedrush.config import DecisionConfig
from maaracing_master.plugins.speedrush.corridor import Corridor
from maaracing_master.plugins.speedrush.depth_geo import CAM_Z_DATUM_M
from maaracing_master.plugins.speedrush.motion_plan import build_scene
from maaracing_master.plugins.speedrush.selection import (
    BonusSource, CarSource, CoinSource, LayeredSelector, Obj, RoadBand,
    RoadFrame, SelectionParams, SelectionReport)
from maaracing_master.plugins.speedrush.presence_occupancy import (
    OccObs, PresenceOccupancy)
from maaracing_master.plugins.speedrush.route import (
    ArrivalAttribution, PHASE_MERGING, RouteParams, RouteState, RouteVerdict,
    tick_route)
from maaracing_master.plugins.speedrush.traffic import (
    CarView, PassEvent, OUTCOME_PASS, OUTCOME_PRESUMED_PASS)
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionOutput, DecisionState, DistanceSource, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    IMAGE_H, KIND_BONUS, KIND_CAR, Calib, load_calib, t_meet_car_s, x_lane_of)

_SCHEMA = 5  # v5：删除 move_allowed（无消费者）；v4=maneuver 权限位
_EPS = 1e-9


def _lane_ttc_map(scene, c0: float, horizon_s: float) -> dict[int, float]:
    """整数道 → 最早到站时间（秒）：车道冲突的时间面（路线层消费）。

    场景对象 x 已是路心系米；c0 是同一帧 z=0 处路心。按整道（±0.5 道）
    归属，只收有到站时间且在视界内的车——到站时间已含相对速度，这是路线
    层唯一能区分「24m 内有个同速车」和「1s 后真撞上」的量。"""
    out: dict[int, float] = {}
    lw = scene.lane_w_m
    for o in scene.objs:
        if o.kind != "car" or not o.timed or o.t_arrive_s is None:
            continue
        if not (0.0 < o.t_arrive_s <= horizon_s):
            continue
        k = int(round((o.x_m - c0) / lw))
        t = out.get(k)
        if t is None or o.t_arrive_s < t:
            out[k] = o.t_arrive_s
    return out


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


def _iou2(a: tuple, b: tuple) -> float:
    if not a or not b:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0.0:
        return 0.0
    ua = ((a[2] - a[0]) * (a[3] - a[1])
          + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / ua if ua > 0 else 0.0


class ValidationWatch:
    """§四 致命信号器：只做信号与合取，不做状态转移。"""

    def __init__(self, cfg: DecisionConfig):
        self.v = cfg.validate
        self._empty_t = 0.0
        # id → (fid, cy, box)；真实观测。box 用于区分“快车逼近”与“身份互换”。
        self._last_cy: dict[int, tuple[int, int, tuple]] = {}

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
        # 只有“画面有车、跟踪却没输出”才算目标流枯竭；画面本来没车
        # （raw=False）是空旷路面，不是故障（2026-10-19 局 213750_p1 空街
        # 误停 5.4s）。币/奖励是机会面、天然闪断，不参与本判据（2026-10-20
        # 阶段 1：画面有币、跟踪空，旧判据冻结 1.8s 错过整组币）。
        if conj and not h.target_presence and h.raw_vehicle_presence:
            self._empty_t += dt_s
            if self._empty_t >= self.v.t_empty_s:
                return ValidationReport("target_empty")
        else:
            self._empty_t = 0.0
        # 信号·距离序倒挂：同 id 真实观测间 cy 跳变超物理上限（按失联 tick 数
        # 放大）。逼近的大车会在 1~2 拍内跨过 90px；只要 2D 框仍连续
        # （IoU≥0.3），就是同一台车在动，不是身份互换。
        for t in obs.targets:
            if t.last_seen_fid != obs.frame_id:
                continue  # 保持输出不比（外推值天然"跳"）
            prev = self._last_cy.get(t.id)
            if prev is not None:
                span = max(obs.frame_id - prev[0], 1)
                if abs(t.cy - prev[1]) > self.v.cy_jump_max_px * span \
                        and _iou2(getattr(t, "box", ()), prev[2]) < 0.3:
                    return ValidationReport("cy_jump")
            self._last_cy[t.id] = (obs.frame_id, t.cy,
                                   getattr(t, "box", ()))
        for g in obs.far_targets:
            if g.last_seen_fid == obs.frame_id:
                self._last_cy[g.id] = (obs.frame_id, g.cy, ())
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
        """车流到画面下缘秒数（地面流积分式，world_model.t_meet_car_s 单一
        公式真源；前置三件套①）。保险杠机位（2026-10-23）终点是画面下缘，
        不是追车机位的自车行——自车不在画面里。rel 单位 px/fid，换算秒吃
        self.fid_hz；供数缺位（None）退回旧行率式——单位/时间基准契约检查属
        回放验收（计划 §2①），换式不把旧单位错误带进新式。"""
        if self.fid_hz is None:
            rate = rel * self.hz
            return max(0.0, IMAGE_H - cy) / rate if rate > _EPS \
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
        """地面目标行 cy → 到画面下缘的恒速到站时间（流模型积分式）。

        线性式 (D−cy)/r(cy) 会低估不到站时间的一半量级——行速率随行号
        二次增长，越近越快，剩余行程不是匀速的；对 1/r 沿行积分才是真时间。
        保险杠机位（2026-10-23）终点是画面下缘，不是追车机位的自车行。"""
        a = 1.0 / (cy - self.cal.y_h)
        b = 1.0 / (IMAGE_H - self.cal.y_h)
        return max(0.0, (a - b) / k)


def _monotonic_coin_t(prev: tuple[float, int] | None, raw_t: float,
                      elapsed_s: float, tol_s: float) -> float:
    """同组金币到站时间只允许按时间递减；突增=换成了组里更远的一枚。"""
    if prev is None or not math.isfinite(prev[0]):
        return raw_t
    expected = prev[0] - elapsed_s
    if raw_t > expected + tol_s:
        return max(expected, 0.0)
    return raw_t


def _sel_report_key(rep: SelectionReport) -> tuple:
    """选择层报告比对键：只含接控制/接路线请求的语义字段。"""
    return (rep.layer, rep.why, round(rep.target_x_m, 6),
            rep.lane_request, bool(rep.maneuver),
            tuple((round(w.t, 6), round(w.x, 6), w.src)
                  for w in rep.waypoints))


def _sel_report_trace(rep: SelectionReport) -> dict:
    return {"l": rep.layer, "why": rep.why, "x": round(rep.target_x_m, 3),
            "req": rep.lane_request, "man": bool(rep.maneuver),
            "nwp": len(rep.waypoints)}


def _sel_in_trace(inp: tuple) -> dict:
    coarse, committed, commit_x = inp
    return {"coarse": None if coarse is None else round(coarse, 6),
            "commit": bool(committed),
            "cx": None if commit_x is None else round(commit_x, 6)}


def _route_trace(v: RouteVerdict | None) -> dict | None:
    if v is None:
        return None
    return {"ph": v.phase, "ref": v.lane_ref, "tgt": v.lane_target,
            "vd": v.verdict, "why": v.why, "ep": v.epoch,
            "blkfin": bool(v.blocked_finish)}


def _sel_objects_trace(rewards, cars, coins, ego_m: float,
                       lw: float) -> dict:
    """取证面：选择层当拍对象的完整输入（含金币排序用的枚数/置信度）。"""
    def rows(objs):
        return [[round(o.x / lw, 3),
                 None if o.t is None else round(o.t, 3),
                 round(float(o.count), 2), round(float(o.conf), 3)]
                for o in objs]
    return {"ego": round(ego_m / lw, 3), "rw": rows(rewards),
            "car": rows(cars), "coin": rows(coins)}


class SelectionShadowWatch:
    """选择层影子的比对与取证（只记录，不参与控制）。

    预分歧：两份路线给选择层的承诺输入相同，选择结果必须逐位一致；不一致
    记 `bug` 并保留首次不一致的两边报告、输入与路线状态（不归因于 C）。
    首次输入分歧：影子路线开始走不同的承诺态，此后结果不同是预期；记 `div`
    并保留首次分歧的完整现场。此后只在结果仍不同时记紧凑行。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.diverged = False
        self.first_div: dict | None = None
        self.first_mismatch: dict | None = None
        self.mismatch_ticks = 0

    def step(self, *, fid: int, prod: SelectionReport,
             shadow: SelectionReport, prod_in: tuple, shadow_in: tuple,
             prod_route: RouteVerdict | None,
             shadow_route: RouteVerdict | None, snapshot) -> dict | None:
        same_in = _sel_in_trace(prod_in) == _sel_in_trace(shadow_in)
        same_out = _sel_report_key(prod) == _sel_report_key(shadow)
        if same_in and same_out:
            return None
        if same_in:
            # 输入完全一致却选择不同：影子接线/初始化问题，或选择器非确定性。
            self.mismatch_ticks += 1
            rec: dict = {"bug": 1, "n": self.mismatch_ticks}
            if self.first_mismatch is None:
                self.first_mismatch = {
                    "fid": fid,
                    "prod": _sel_report_trace(prod),
                    "shadow": _sel_report_trace(shadow),
                    "in": _sel_in_trace(prod_in),
                    "route": {"prod": _route_trace(prod_route),
                              "shadow": _route_trace(shadow_route)},
                    "objects": snapshot(),
                }
                rec["first"] = self.first_mismatch
        else:
            if same_out and self.diverged:
                return None       # 已分叉但本拍选择回到一致：窗口闭合，安静
            if not self.diverged:
                self.diverged = True
                self.first_div = {
                    "fid": fid,
                    "prod": _sel_report_trace(prod),
                    "shadow": _sel_report_trace(shadow),
                    "in": {"prod": _sel_in_trace(prod_in),
                           "shadow": _sel_in_trace(shadow_in)},
                    "route": {"prod": _route_trace(prod_route),
                              "shadow": _route_trace(shadow_route)},
                    "objects": snapshot(),
                }
                rec = {"div": 1, "first": self.first_div}
            else:
                # 分歧窗口内每拍都带对象快照：机会层本会看到什么、按什么排序
                # （金币枚数/置信度）——窗口很短，取证成本可忽略。
                rec = {"div": 1, "objects": snapshot()}
        rec.update(_sel_report_trace(shadow))
        return rec


class DecisionEngine:
    """FSM + 迟滞（§二/§三/§五）。单线程 owner 使用（与 _log_grp 同一线程纪律）。"""

    def __init__(self, cfg: DecisionConfig, cal: Calib | None = None):
        self.cfg = cfg
        self.cal = cal if cal is not None else load_calib()
        # 存在性占用影子（2026-10-22，只读观测）：每拍推进，trace 读 occ_snap；
        # 不改变选择/规划输出。
        self.occ = PresenceOccupancy(self.cal.lane_w_m)
        self._occ_snap: dict | None = None
        self._sel_ego_m: float | None = None
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
            # 可达性复用执行层的横向运动学：一份数值真源，避免选择层
            # 再拍一个换道时间与底层打架。
            a_lat_lane_s2=cfg.planner.a_lat_gain,
            v_lat_max_lane_s=cfg.planner.v_lat_max,
            reaction_s=s.reaction_s, margin_s=s.margin_s,
            wall_margin_lane=s.wall_margin_lane,
            center_lookahead_m=s.center_lookahead_m,
            switch_dwell_s=s.switch_dwell_s,
            reward_tol_lane=s.reward_tol_lane,
            car_half_lane=s.car_half_lane,
            ego_half_lane=s.ego_half_lane,
            lane_clearance_lane=s.lane_clearance_lane))
        # 选择层影子（2026-10-28）：与生产同一拍同一批输入，承诺态跟影子路线
        # 走；只落 trace、不接控制。预分歧段两份输入相同，结果必须逐位一致
        # ——这是"影子接线可信"的运行时校验，也是 C 下游差异的可信度门。
        self._selector_shadow = LayeredSelector(self.selector.p)
        self._sel_watch = SelectionShadowWatch()
        self._sel_shadow: SelectionReport | None = None
        self._sel_shadow_trace: dict | None = None
        self._sel_report: SelectionReport | None = None
        self._sel_maneuver = False
        self._sel_objs: dict = {"reward": [], "car": [], "coin": []}
        self._sel_dbg: dict | None = None
        # 粗层选道（2026-10-19 影子期）：绿区净空 + 2D 占用，只落 trace。
        c = cfg.coarse
        self._coarse_params = CoarseParams(
            lookahead_m=c.lookahead_m,
            max_offset_lane=c.max_offset_lane,
            collision_margin_m=c.collision_margin_m,
            range_speed_mps=c.range_speed_mps,
            switch_margin_m=c.switch_margin_m,
            align_tol_lane=c.align_tol_lane,
            wall_margin_lane=s.wall_margin_lane,
            ego_half_lane=s.ego_half_lane,
            car_half_lane=s.car_half_lane,
            cam_z_datum_m=CAM_Z_DATUM_M,
            far_direction=c.far_direction)
        self._coarse: CoarseReport | None = None
        # 路线承诺层（2026-10-23 V1）：横向意图的跨帧持有者。每拍用**原始**粗层
        # 报告推进（闸关时也推进——那是影子对照面，量"承诺拦掉哪些翻面"）；
        # 只有 route.control 开时它的参考道才接控制。不回读 coarse_lane 属性：
        # 承诺值喂回候选评估会让迟滞语义在两层之间漂移。
        r = cfg.route
        self._route_params = RouteParams(
            # 门槛/速度类数值全部复用既有真源，不在 [route] 复制一份
            switch_gain_m=c.switch_margin_m,
            proxy_speed_mps=c.range_speed_mps,
            urgent_confirm_ticks=r.urgent_confirm_ticks,
            opportunity_confirm_ticks=r.opportunity_confirm_ticks,
            cancel_unsafe_ticks=r.cancel_unsafe_ticks,
            cancel_cooldown_s=r.cancel_cooldown_s,
            finish_tol_lane=s.center_band_lane,
            finish_v_tol_lane_s=r.finish_v_tol_lane_s,
            merge_max_s=r.merge_max_s,
            soft_car=r.soft_car,
            urgent_ttc_s=r.urgent_ttc_s,
            urgent_blind_m=r.urgent_blind_m,
            merge_safe_ttc_s=r.merge_safe_ttc_s,
            pass_hold=r.pass_hold,
            overtake=r.overtake,
            overtake_ttc_s=r.overtake_ttc_s)
        self._route: RouteState | None = None
        self._route_verdict: RouteVerdict | None = None
        # 到达门影子（2026-10-28）：同拍同输入再跑一份带门的路线，只落 trace，
        # 不接控制。生产路线用配置门（默认关），影子恒开；两份状态各自推进。
        self._route_shadow: RouteState | None = None
        self._shadow_verdict: RouteVerdict | None = None
        self._position: ArrivalAttribution | None = None
        # 上拍选择层越线请求（道，相对粗层原点）；逐拍刷新，喂 route 裁决
        self._route_req: float | None = None
        self._route_epoch = 0            # 参考系纪元（尺子跳变时 +1）
        self._ruler_prev: float | None = None   # 上一拍尺子（米，场景帧 z=0）
        self._ruler_exec_prev: float | None = None  # 上一拍 executed（道，右正）
        # 车米制状态实例通道（C 线；update 每拍刷新，reset 清——与 _views 同族）
        self._metric_cars: tuple = ()
        # 车位时间平滑（滚出道路帧，避免自车横移被当成目标横移）
        self._car_xf: dict[int, float] = {}
        self._car_zf: dict[int, float] = {}
        # 米制横位硬跳变保持：id → (last_accepted_x, consecutive_jump_ticks)
        self._car_x_hold: dict[int, tuple[float, int]] = {}
        # 金币组到站时间记忆（gid → (t, fid)）：同组只允许时间递减
        self._coin_t_mem: dict[int, tuple[float, int]] = {}
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
        self._coarse = None
        self._route = None
        self._route_verdict = None
        self._route_shadow = None
        self._shadow_verdict = None
        self._position = None
        self._route_req = None
        self._route_epoch = 0
        self._ruler_prev = None
        self._ruler_exec_prev = None
        self.occ.reset()
        self._occ_snap = None
        self._sel_ego_m = None
        self.selector.reset()
        self._selector_shadow.reset()
        self._sel_watch.reset()
        self._sel_shadow = None
        self._sel_shadow_trace = None
        self._car_xf.clear()
        self._car_zf.clear()
        self._car_x_hold.clear()
        self._coin_t_mem.clear()
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
               corridor: Corridor | None = None,
               position: ArrivalAttribution | None = None) -> DecisionOutput:
        """traffic=(在途车辆视图, 本拍落定事件)（traffic.TrafficObserver 直供）。

        缺省 None=无车流观测，行为与 coin-only v1 **逐拍相同**（V0/V1 兼容红线）。
        逐拍视图放实例通道（单线程 owner 纪律，与 _log_grp 同族）——helper 签名不扩散。

        ``road_offset``：保留在签名里兼容既有调用（本层不消费，路心由
        corridor 带给出）。``corridor``：同拍分带走廊，选择层唯一的空间证据；
        None=无路，持守当前横位。``position``：当拍到达位置归因（观测面 +
        影子到达门；生产默认门关，不影响控制）。"""
        self._position = position
        self._dt_last = dt_s
        self._fid = obs.frame_id
        self._fps = obs.fid_rate_hz
        # 差异行只对本拍有效：没有推进选择层（无路/非巡航）的拍不能回放上一拍。
        self._sel_shadow_trace = None
        self._views, self._events = traffic if traffic is not None else ((), ())
        # 车米制状态实例通道（C 线 2026-10-12，闸 metric_cars.enabled）：车源
        # 链消费（米制优先、像素回退）；闸关=空（回退路径）。
        self._metric_cars = obs.metric_cars if self.cfg.metric_cars.enabled else ()
        # 占用影子先于状态机推进：安全状态不随决策状态/距离有效性消失。
        # sel_ego_m 每拍清空，只有本拍 CRUISE 产出了当前选择才计算 veto。
        self._sel_ego_m = None
        self._update_occ(obs, executed_lane)
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
        # 并线承诺期不走 slew：目标是路线已定的道，限速只会让实际目标滞后
        # 承诺道（旧 trace 897 个并线拍里 155 拍目标差 >0.5 道就是这么来的）。
        route = self._route
        committed = (self.cfg.route.control and route is not None
                     and route.phase == PHASE_MERGING)
        slew = 0.0 if committed else \
            self.cfg.hysteresis.target_slew_lane_s * dt_s
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
        # 行为契约观测面（阶段 0）：只推导并记录，不改变控制。权限按相位
        # 给：非巡航/HOLD；并线或机动/COMMIT；其余/GENTLE。
        merging = route is not None and route.phase == PHASE_MERGING
        if self._state is not DecisionState.CRUISE:
            authority = "HOLD"
        elif merging or self._maneuver:
            authority = "COMMIT"
        else:
            authority = "GENTLE"
        intent_lane = None
        if route is not None:
            intent_lane = route.lane_target if merging else route.lane_ref
        deadline_s = None
        if merging:
            deadline_s = max(
                0.0, route.merge_started_t + self._route_params.merge_max_s
                - route.elapsed_s)
        return DecisionOutput(
            schema_version=_SCHEMA, state=self._state,
            x_target=x_out if self._state is not DecisionState.CONSERVE else None,
            reason=reason, emitted_fid=obs.frame_id,
            valid_until_fid=obs.frame_id + self._validity_ticks(),
            maneuver=self._maneuver, intent_lane=intent_lane,
            phase="merging" if merging else "keep", authority=authority,
            deadline_s=deadline_s,
            intent_epoch=0 if route is None else route.epoch)

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
            t_raw = s.t_miss_s
            if not math.isfinite(t_raw):
                continue
            # 同组时间单调：组里换了更远的一枚时 t 会突增，计划会在
            # 1.5s/3.0s 之间反复进出视界。沿用旧时间并按经过时间前移。
            mem = self._coin_t_mem.get(g.group_id)
            t_raw = _monotonic_coin_t(
                mem, t_raw, self._age_ms(mem[1]) / 1000.0 if mem else 0.0,
                self.cfg.motion.coin_time_jump_tol_s)
            self._coin_t_mem[g.group_id] = (t_raw, obs.frame_id)
            coins.append(CoinSource(
                x_m=(g.x_center + executed_lane) * lane_w,
                observed_count=g.observed_count, conf=g.conf_min,
                t_miss_s=t_raw,
                age_ms=self._age_ms(g.lead_fid or g.last_seen_fid)))
        # 记忆只保留最近 1.5s 出现过的组
        for gid in [k for k, (_, f) in self._coin_t_mem.items()
                    if self._age_ms(f) > 1500.0]:
            self._coin_t_mem.pop(gid, None)
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

        ax = self.cfg.traffic.metric_x_ema_alpha
        az = self.cfg.traffic.metric_z_ema_alpha
        near_cy = IMAGE_H - self.cfg.motion.near_cy_margin_px

        def _near(v, z: float | None) -> bool:
            """纵向近：有纵深看 z（米制锚或框高粗距离），无纵深才看行号
            （保险杠机位以画面下缘为界，自车行不是可观测几何）。"""
            if z is not None:
                return z <= self.cfg.motion.near_z_m
            return v is not None and v.cy >= near_cy

        def _half_w(v) -> float | None:
            w = getattr(v, "w_m", None) if v is not None else None
            return None if not w or w <= 0 else w / 2.0

        def enter(x: float | None, tm: float | None,
                  t_ov: float | None = None,
                  bridged: bool = False,
                  z_m: float | None = None,
                  age_ms: float = 0.0, src: str = "pixel",
                  tid: int | None = None,
                  near: bool = False,
                  half_w_m: float | None = None) -> None:
            if x is None or x != x:
                return
            # 平滑在道路帧（x+off）：自车横移由 off 抵消，目标自身的慢横移
            # 保留；米制 x/z 的逐拍抖动（±1~4m）在这里收敛到擦车带量级以下。
            xr = x + off
            if tid is not None:
                # 纵深大跳变 = 新锚点/真实通过：x 也必须从新基线重来。
                # 否则 x 的硬跳变门会把旧的坏横位锁死——一条远锚的 x=-18m
                # 在近锚 x=-2.7m 到达后仍被“hold”三拍，车被当成 15m 外
                # （2026-10-19 局 215826_p2 4065 拍）。
                pz = self._car_zf.get(tid)
                z_reset = (z_m is not None and z_m == z_m
                           and (pz is None or abs(z_m - pz) >
                                self.cfg.traffic.metric_z_jump_max_m))
                prev = self._car_xf.get(tid)
                if z_reset:
                    self._car_xf[tid] = xr
                    self._car_x_hold[tid] = (xr, 0)
                else:
                    xr = xr if prev is None else prev + ax * (xr - prev)
                    # 硬跳变门：真实横移单拍不可能 >1.5m（20Hz 下 30m/s 横速）；
                    # 先拒，连续 hold_ticks 拍仍在新位置才接受。
                    jmax = self.cfg.traffic.metric_x_jump_max_m
                    hold = self._car_x_hold.get(tid)
                    if hold is not None and jmax > 0.0 \
                            and abs(xr - hold[0]) > jmax:
                        n_jump = hold[1] + 1
                        if n_jump < self.cfg.traffic.metric_x_jump_hold_ticks:
                            xr = hold[0]
                            self._car_x_hold[tid] = (hold[0], n_jump)
                        else:
                            self._car_x_hold[tid] = (xr, 0)
                    else:
                        self._car_x_hold[tid] = (xr, 0)
                    self._car_xf[tid] = xr
                if z_m is not None and z_m == z_m:
                    # 大跳变=真实通过/换锚，直接重置；只有小抖动才走 EMA。
                    if z_reset:
                        self._car_zf[tid] = z_m
                    else:
                        z_m = pz + az * (z_m - pz)
                        self._car_zf[tid] = z_m
            out.append(CarSource(x_m=xr, t_meet_s=tm,
                                 t_overtake_s=t_ov, bridged=bridged,
                                 z_m=z_m, age_ms=age_ms, src=src,
                                 near=near, half_w_m=half_w_m))

        for m in self._metric_cars:
            v = by_id.get(m.target_id)
            bridged = getattr(m, "bridged", False)
            if m.source is DistanceSource.BOX_SIZE:
                # 无锚框高粗距离（2026-10-23）：只用于近/远与选道，横向仍走
                # 像素尺子、到站时间弃权（~40% 尾部误差不能进 TTC）；z_m 不喂
                # enter 的 z EMA/跳变门与路心采样（那是精确米制口径）。
                if v is None or v.x_lane is None:
                    continue                    # 无横向读数：诚实弃权（远轨无尺子）
                seen.add(m.target_id)
                enter(v.x_lane * lane_w, None, None, z_m=None, age_ms=0.0,
                      src="metric_coarse", tid=m.target_id,
                      near=_near(v, m.z_m), half_w_m=_half_w(v))
                continue
            elif m.z_m is not None:
                x = m.x_m
                if x is None and v is not None and v.x_lane is not None:
                    x = v.x_lane * lane_w       # 无同帧焦距：横向回退像素尺子
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
                  age_ms=(m.anchor_age_ms or 0.0), src="metric",
                  tid=m.target_id, near=_near(v, m.z_m),
                  half_w_m=_half_w(v))
        for vid, v in by_id.items():
            if vid in seen:
                continue                        # 米制路径已入账，不双计
            x, tm = self.scorer.pixel_instruments(v)
            seen.add(vid)
            # 新鲜度=距最后一次真实读数的帧数；旧构造没有 last_seen_fid 时
            # 才退回轨道生命期（age_ticks 不能当新鲜度，否则持续被检出的车
            # 会在 0.25s 后从场景蒸发）。
            if self._fps:
                fresh = (self._fid - v.last_seen_fid
                         if v.last_seen_fid else v.age_ticks)
                age_ms = max(0.0, fresh / self._fps * 1000.0)
            else:
                age_ms = 0.0
            enter(None if x is None else x * lane_w, tm,
                  age_ms=age_ms, tid=vid, near=_near(v, None),
                  half_w_m=_half_w(v))
        for k in [k for k in self._car_xf if k not in seen]:
            self._car_xf.pop(k, None)
            self._car_zf.pop(k, None)
            self._car_x_hold.pop(k, None)
        return out

    @property
    def bonus_src_count(self) -> int:
        """上一拍奖励车源数（近场+远场粗源之和；0=源为空——R5 漏斗观测面）。"""
        return self._bonus_src

    def _update_occ(self, obs: WorldObservation,
                    executed_lane: float | None) -> None:
        """把本拍目标/距离/掠过事件喂给占用影子（只读，不做决策）。"""
        dist = {d.target_id: d for d in obs.distances}

        def _m(d) -> float | None:
            # 锚龄 600ms 硬顶外的米制值不再作为“可信近距”证人；框/接地行
            # 仍可独立建占，所以过期距离只会少一条证人，不会清空占用。
            if d is None or d.metric_distance_m is None:
                return None
            if d.anchor_age_ms is not None and d.anchor_age_ms > 600.0:
                return None
            return d.metric_distance_m

        passed = {e.track_id for e in self._events
                  if e.outcome in (OUTCOME_PASS, OUTCOME_PRESUMED_PASS)}
        rows: list[OccObs] = []
        for t in obs.targets:
            if t.kind != KIND_CAR:
                continue
            rows.append(OccObs(
                t.id, t.h, t.cy, t.x_lane, _m(dist.get(t.id)),
                t.last_seen_fid == obs.frame_id))
        for t in obs.far_targets:
            if t.kind != KIND_CAR:
                continue
            rows.append(OccObs(
                t.id, t.h, t.cy, t.last_x_lane, _m(dist.get(t.id)),
                t.last_seen_fid == obs.frame_id))
        self._occ_snap = self.occ.update(
            obs.ts_ns / 1e9, rows, executed_lane, passed)

    def occ_snap(self) -> dict | None:
        """trace 观测面：占用快照 + 本拍选择是否落在被占车道（veto）。"""
        snap = self._occ_snap
        if not snap:
            return None
        out = dict(snap)
        rep = self._sel_report
        ego = self._sel_ego_m
        if rep is not None and ego is not None:
            out["sel_layer"] = rep.layer
            out["sel_why"] = rep.why
            out["sel_x"] = round(rep.target_x_m, 2)
            out["veto"] = bool(
                abs(rep.target_x_m - ego) < 0.5 * self.cal.lane_w_m)
        else:
            out["veto"] = False
        return out

    @property
    def metric_cars_snap(self) -> list[dict]:
        """上一拍车米制状态快照（观测面，trace mcars 列数据源；R3 归因面）：
        逐车横向位/纵向距/到站/接近速/锚龄——机会漏点（可达门 vs 碰撞判据）
        的离线复算输入。t_ov 沿 _reach_car_sources 同式（≤0/桥接段=仪器缺位
        弃权）。b/bwhy=速度像素桥标记与进入原因（闸 metric_bridge.enabled，
        默认关恒 False/None）。bz=锚间框高比桥接值（闸 box_bridge；
        trace_only 时只观测、z 仍是发布口径）。空表=metric_cars 关或无车。"""
        return [{"id": m.target_id, "src": m.source.value,
                 "x": None if m.x_m is None else round(m.x_m, 2),
                 "z": None if m.z_m is None else round(m.z_m, 2),
                 "t_ov": None if (m.t_meet_s is None or m.t_meet_s <= 0.0
                                  or getattr(m, "bridged", False))
                 else round(m.t_meet_s, 3),
                 "v": None if m.v_close_mps is None else round(m.v_close_mps, 2),
                 "age": None if m.anchor_age_ms is None
                 else round(m.anchor_age_ms, 1),
                 "bz": None if m.box_bridge_z_m is None
                 else round(m.box_bridge_z_m, 2),
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

        # 车道原点是供数门 master 绝对路心（墙/空气墙/绿区物理边界），不是
        # 分带不对称中点：带中点会被墙箱/单侧绿区拽偏（2026-10-20 实机 0.86
        # 道分歧），选择目标因此整体偏移。带边界仍按原样供宽度/包络。
        # A 系（带已加 executed 平移）里 master 位于 center+executed 道——
        # 两者互相抵消（executed≈−center）时原点≈0，目标道号与 planner 同帧。
        master = self._corridor.center_lane
        origin = (_at(0.0, lambda b, k: 0.5 * (b[2] + b[3]))
                  if master is None else (master + executed) * lane_w)
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
        # 中远场粗带换算进选择坐标（x + shift − origin，src 统一 inferred）：
        # 方向级消费（coarse.far_direction 开关）读不对称性，不进 bands。
        # 必须与 3m 带同一条平移（先加 executed、再减 origin）——只减 origin
        # 会让 executed 漏进远带中点，执行车道一跳就伪造出远场方向（p2
        # 2796→2799 实拍：远带与中心线未动，executed 翻 1.47 道，hint 从
        # −0.92 翻到 +0.55）。
        coarse_bands = tuple(
            RoadBand(bd.z0, bd.z1,
                     None if bd.left is None or bd.left.x_m is None
                     else bd.left.x_m + shift - origin,
                     None if bd.right is None or bd.right.x_m is None
                     else bd.right.x_m + shift - origin,
                     src="inferred")
            for bd in self._corridor.coarse_bands)
        left0 = _at(0.0, lambda b, k: b[2]) - origin
        right0 = _at(0.0, lambda b, k: b[3]) - origin
        width = (right0 - left0) if right0 > left0 else (
            self._corridor.width_lane * lane_w
            if self._corridor.width_lane else None)
        road = RoadFrame(bands, width, left0, right0, True, coarse_bands,
                         far_hint_lane=self._corridor.far_hint_lane)
        return road, shift - origin, origin

    def _tick_cruise_layered(self, obs: WorldObservation,
                             executed: float | None) -> str:
        """四层选择器接管 CRUISE：奖励车 > 擦边 > 金币 > 中心带（粗层选道）。"""
        if executed is None or self._corridor is None:
            self._reach_goal = None
            self._exec_branch = "hold"
            self._scene = None
            self._sel_report = None
            self._sel_maneuver = False
            self._sel_objs = {"reward": [], "car": [], "coin": []}
            self._sel_dbg = None
            self._coarse = None
            self._route_req = None
            # 无车道级证据拍：路线不推进，时钟继续走
            self._tick_route(executed=executed)
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
            self._coarse = None
            self._route_req = None
            # 无车道级证据拍：路线不推进，时钟继续走
            self._tick_route(executed=executed)
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
        # 选择帧 → 规划器路心帧：选择帧原点已是 master，frame_shift 只需
        # 补齐「master 在带平移系里的位置」（两者对齐时≈0）；不平移会让
        # frame_delta 常年超阈值、走廊按差额加宽到失效。
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
        # 粗层：绿区净空 + 2D 占用选方向。迟滞的现任道取上一拍粗层结果。
        # 自车横位与候选道必须同帧：候选道按每带中心线取相对量，自车也
        # 按 scene 的 z=0 中心线算——直接喂 executed（planner 路心系）会
        # 把「带中心线 vs 路心」的残差带进方向判定。
        c0 = self._scene.road.center_at(0.0)
        coarse_ego = (executed if c0 is None else
                      (self._scene.ego_x_m - c0) / self._scene.lane_w_m)
        self._coarse = choose_lane(
            self._scene.road, self._scene.objs, coarse_ego,
            self._scene.lane_w_m, self._coarse_params,
            current_lane=None if self._coarse is None else self._coarse.lane)
        # 路线承诺（2026-10-23 V1）：用**原始**粗层报告推进一拍，再让选择器读
        # 承诺后的参考道——顺序反了选择器就会吃到上一拍的承诺值。闸关时这一步
        # 照样跑（影子对照面：离线能量化"承诺拦掉了哪些翻面"），只是没人消费。
        self._tick_route(c0, executed, request=self._route_req)
        # 并线承诺期：路线已定目标道，选择层只能在这个道上做维护性修正。
        # 机会层（奖励车/擦边/金币）与预置偏移在此关闭——承诺期的目标被
        # 机会改道就是静默换道（契约 §4/§5）。
        committed = (self.cfg.route.control and self._route is not None
                     and self._route.phase == PHASE_MERGING)
        # 跨线唯一入口（2026-10-27）：闸开且保持态时把承诺道中心喂给选择层——
        # 出本道安全区间的机会目标回夹区间边界，越线意图改发路线请求；
        # 闸关逐位回旧行为（机会连续拽）。并线期目标本来就是承诺道，不喂。
        commit_x = None
        if self.cfg.route.control and self._route is not None and not committed:
            commit_x = self._route.lane_ref * self.cal.lane_w_m + c0
        # 粗层（2026-10-20 接控制）：只接管 L4 回中兜底——细层没有奖励车/
        # 擦边/金币目标时，去净空最长的车道，而不是贴回中心线。机会层不受
        # 影响；走廊陈旧（scene.stale）时退回纯回中，不用旧边界选道。
        coarse_lane = self.coarse_lane
        rep = self.selector.update(road, rewards, cars, coins, ego_m,
                                   self._dt_last,
                                   coarse_lane=coarse_lane,
                                   committed=committed, commit_x_m=commit_x)
        # 影子选择器：同一批对象/公路/自车输入，承诺态取影子路线。生产门关或
        # 两路未分叉时两份输入相同，结果必须逐位一致；不一致记 `bug` 取证。
        sh_committed = bool(self.cfg.route.control
                            and self._route_shadow is not None
                            and self._route_shadow.phase == PHASE_MERGING)
        sh_coarse = self._lane_for_route(
            self._route_shadow, self._shadow_verdict, self.cfg.route.control)
        sh_commit_x = None
        if self.cfg.route.control and self._route_shadow is not None \
                and not sh_committed:
            sh_commit_x = self._route_shadow.lane_ref * lw + c0
        rep_shadow = self._selector_shadow.update(
            road, rewards, cars, coins, ego_m, self._dt_last,
            coarse_lane=sh_coarse, committed=sh_committed,
            commit_x_m=sh_commit_x)
        self._sel_shadow = rep_shadow
        self._sel_shadow_trace = self._sel_watch.step(
            fid=obs.frame_id, prod=rep, shadow=rep_shadow,
            prod_in=(coarse_lane, committed, commit_x),
            shadow_in=(sh_coarse, sh_committed, sh_commit_x),
            prod_route=self._route_verdict,
            shadow_route=self._shadow_verdict,
            snapshot=lambda: _sel_objects_trace(
                rewards, cars, coins, ego_m, lw))
        self._route_req = rep.lane_request if self.cfg.route.control else None
        self._sel_report = rep
        self._sel_ego_m = ego_m
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
            # 越线请求（道，相对承诺道）：trace 观测面，离线复算
            # 「为什么并线/为什么没并」从这一列起
            "req": rep.lane_request,
            # 帧合成审计项：origin=走廊中心@0，shift=自车横位平移，
            # fshift=带中心帧→规划器路心帧的平移。车米制 x 与场景 x 的
            # 多米分歧只能靠这三项定位（2026-10-19 局 214717_p1）。
            "origin": round(origin, 3),
            "shift": round(executed * lw, 3),
            "fshift": round(frame_shift, 3),
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
    def coarse_report(self) -> CoarseReport | None:
        """上一拍粗层选道结果（trace 观测面；闸开时它是**原始候选评估**，
        不再等于接控制的那条道）。"""
        return self._coarse

    @property
    def route_verdict(self) -> RouteVerdict | None:
        """上一拍路线定案结果（trace route 列数据源；离线复算为什么放行/被拒）。"""
        return self._route_verdict

    @property
    def route_shadow_verdict(self) -> RouteVerdict | None:
        """上一拍到达门影子定案（trace route_shadow 列；不接控制）。"""
        return self._shadow_verdict

    @property
    def position_evidence(self) -> ArrivalAttribution | None:
        """上一拍到达位置归因（trace attribution 列；只读观测）。"""
        return self._position

    @property
    def selection_shadow_report(self) -> SelectionReport | None:
        """上一拍影子选择结果（只读观测；不接控制、不进路线请求）。"""
        return self._sel_shadow

    @property
    def selection_shadow_trace(self) -> dict | None:
        """上一拍选择影子差异行（None=与生产一致；bug=同输入不一致）。"""
        return self._sel_shadow_trace

    @property
    def selection_shadow_first_mismatch(self) -> dict | None:
        """本段首次"同输入不同结果"的现场（None=没发生过）。"""
        return self._sel_watch.first_mismatch

    RULER_TOL_LANE = 0.5    # 尺子单拍跳变超过半道 = 参考系重基

    def _ruler_epoch(self, c0: float | None,
                     executed: float | None = None) -> int:
        """参考系纪元。两个跳变源，任一崩过半道即 +1：

        ① 尺子 = **场景 z=0 分带中心线**——粗层道号相对它取，它单拍跳变超过
        半道即换义。**不跟绝对路心走**：20261024_p2 实测绝对路心在「记忆 ↔
        遮挡墙」之间每 ~0.85 s 摆 0.4~1.7 道（换源造成），而同一局的尺子
        p90 只动 0.09 道；按绝对路心换纪会把路线每秒作废一次（38/50 次并线
        被打断），承诺名存实亡。
        ② **执行层重锚**（executed 单拍跳变过半道）：深度路心翻面被 ⑥b 确认
        重基后，`executed` 一步换掉一道以上——道号语义跟着换，旧承诺指的道
        已不是车所在的道。20261027_154626_1 s1 13.3s：executed −1.38→+0.58
        后路线仍守 −1，空路上满舵左打 1.4 道。`executed` 经过 ⑥b 的跳变门/
        确认门，只在真重锚时跳（实测 5~9 次/局），不重现 0.85 s 摆动。"""
        lw = self.cal.lane_w_m
        if c0 is not None and lw > 0:
            tol = self.RULER_TOL_LANE * lw
            if self._ruler_prev is not None and abs(c0 - self._ruler_prev) > tol:
                self._route_epoch += 1
            self._ruler_prev = c0
        if executed is not None:
            if self._ruler_exec_prev is not None and \
                    abs(executed - self._ruler_exec_prev) > self.RULER_TOL_LANE:
                self._route_epoch += 1
            self._ruler_exec_prev = executed
        return self._route_epoch

    def _tick_route(self, c0: float | None = None,
                    executed: float | None = None,
                    request: float | None = None) -> None:
        """推进路线一拍。输入只有原始粗层报告、参考系纪元与上拍越线请求
        （边界 B：承诺值不喂回候选评估），输出经 `coarse_lane` 单一接缝接控制。

        同拍再推一份**到达门影子**（C）：同报告/纪元/请求，门恒开；影子状态
        独立推进，只落 trace，不接控制。生产路线的门读配置（默认关）。"""
        epoch = self._ruler_epoch(c0, executed)
        lane_ttc = (None if self._scene is None or c0 is None
                    else _lane_ttc_map(self._scene, c0,
                                       self.cfg.motion.horizon_s))
        # 上一拍选择层的超车动作：路线本拍不抢方向（路线的方向指令会关闭
        # 机会层，正在进行的擦边会被改成并线；一拍延迟可接受）
        passing = (self._sel_report is not None
                   and getattr(self._sel_report, "why", None) == "pass")
        self._route, self._route_verdict = tick_route(
            self._route, self._coarse, self._route_params,
            dt_s=self._dt_last, epoch=epoch,
            request=request, arrival=self._position,
            reject_shift_arrival=self.cfg.route.reject_shift_arrival,
            lane_ttc=lane_ttc, passing=passing)
        self._route_shadow, self._shadow_verdict = tick_route(
            self._route_shadow, self._coarse, self._route_params,
            dt_s=self._dt_last, epoch=epoch,
            request=request, arrival=self._position,
            reject_shift_arrival=True, lane_ttc=lane_ttc, passing=passing)

    def _lane_for_route(self, route: RouteState | None,
                        verdict: RouteVerdict | None,
                        committed: bool) -> float | None:
        """按给定路线状态取本拍接控制道（生产与影子共用同一套前置门）。"""
        c = self._coarse
        if not self.cfg.coarse.control or c is None or c.reason == "no_road":
            return None
        if self._scene is None or self._scene.stale:
            return None
        if not committed:
            return c.lane
        if route is None or verdict is None \
                or verdict.verdict == "needs_init":
            return None
        return route.lane_ref

    def _lane_for(self, committed: bool) -> float | None:
        """生产路线版 `_lane_for_route`（影子用同一函数换一套状态）。"""
        return self._lane_for_route(self._route, self._route_verdict, committed)

    @property
    def coarse_lane(self) -> float | None:
        """本拍接控制的横向意图道；None=纯中心带（闸关/无路/走廊陈旧/缺锚点）。

        路线闸开时给**承诺后的参考道**（选择器与轨迹层读同一个值），闸关时
        逐位回今天的粗层自选道。"""
        return self._lane_for(self.cfg.route.control)

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
