# -*- coding: utf-8 -*-
"""道级收益评分（2026-10-12 设计裁决：决策单位=道，逐车仪器降级为输入）。

**它是什么**：每拍对每个可走道（4 道整道栅格，路心系，道0=路中心）算一个
收益分，输出逐道收益与相对序（argmax=建议道）。选道的**执行**交给既有
latsample 采样器（终点=道心）；本模块只回答「哪条道的当期收益最高」。

**它不是什么**：不做行为切换（`lane_scoring.enabled` 闸关时引擎不构造本类，
逐拍行为与现役栈逐位相同——plan_layer 同款兼容纪律）；不做逐车事件门
（band/score/feas 四门退役，车流信息以占用/错身价值形式进入道收益）。

**收益构成（事件语义——每个对象记一次，不按可见拍重复计）**：
- 币密度：组值 = coin_value_per_unit×枚数×conf（真源 scoring 段），组心映射
  路心系（xr = x + executed，与账本/反事实探针同一映射）。到站 ≤ 前瞻窗且
  道未被车先占 → 全值；更远 → 远权折减（方向信息仍在）。pacing 车（rel≤0）
  整窗占用——到站时刻对跟速车概念失效，占用表达才成立（V0 归因①'）。
- 邻道错身价值：错身车（t_meet ≤ 前瞻窗）的相邻道记 pass_value（30 保底；
  极限 120 走 p̂ 两层结构，影子期不上——计划稿 §5）。
- 占用/碰撞：本道有车在「我们到得了的时刻」（t_meet ≥ 变道需求）到达 →
  到站 < crash_t 判碰撞（负额大惩罚），否则该时段内本道币被遮挡不可收。

**时间口径**：车 t_meet **米制优先**（C 线 2026-10-12：锚点 z/v_close，锚新
估计窗未开或无锚回退像素）、像素回退走 `Scorer.car_t_meet_s`（地面流积分+
聚合式 fid 率，单一公式真源）；币到站走 `Scorer.score` 的 t_miss（与评分同一
仪器——coins 的全场流 k 仍是 rel×hz 旧时基（挂账），本模块继承同一偏差，不
另建第二份）。

**相对序**：输出按收益降序；道格锚=供数路心（executed 帧道0=路中心），
残余 0.3~0.6 道语义差（找边 vs 供数，见计划稿 §5）只影响绝对道号、不影响
相邻道的相对序——消费方按相对序读，不按绝对道号读。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from maaracing_master.plugins.speedrush.tracking import WorldObservation
from maaracing_master.plugins.speedrush.world_model import Calib

if TYPE_CHECKING:  # 循环导入只活在类型检查期：Scorer 留在 decision.py
    from maaracing_master.plugins.speedrush.config import DecisionConfig
    from maaracing_master.plugins.speedrush.decision import Scorer
    from maaracing_master.plugins.speedrush.traffic import CarView

# 4 道整道栅格（道，右正，原点=路中心）：ego 在道 0 的间隙（executed 帧
# 道中心=0），centers=±0.5/±1.5。游戏路宽 ~4.4 道实测；非整 4 道场景
# （弯道收窄）由出界门兜底，网格不随 W 收缩（收缩会把「相邻道」变成本道）。
LANES = (-1.5, -0.5, 0.5, 1.5)
LANE_HALF = 0.75                 # 道归属半宽（道）


@dataclass(frozen=True)
class LaneReport:
    """一拍道收益评定（trace/影子对照消费；benefits 与 LANES 对位）。"""

    benefits: tuple[float, ...]      # 逐道收益（分）
    pick: float                      # 建议道（argmax，平手取当前道）
    road_half: float                 # 出界门（道）——本次评定用的半宽


class LaneScorer:
    """道收益评定器（单线程 owner 使用；无逐拍状态，公式依赖经构造注入）。"""

    def __init__(self, cfg: "DecisionConfig", cal: Calib, scorer: "Scorer"):
        self._cfg = cfg
        self.c = cfg.lane_scoring
        self.cal = cal
        self.scorer = scorer

    def benefits(self, obs: WorldObservation, views: tuple["CarView", ...],
                 executed: float | None,
                 *, road_width: float | None,
                 metric_cars: tuple = ()) -> LaneReport | None:
        """一拍评定。executed 缺席=无路心锚（诚实弃权，None 不臆造道格）；
        road_width 缺席用 4.4 道先验半宽。

        ``views``：CarView 序列（鸭子同构可——cand_x/t_meet 走 scorer 通道）。
        ``metric_cars``：tracking.MetricCarState 序列（C 线 2026-10-12，闸
        `metric_cars.enabled`）——**米制优先、像素回退**：有锚车吃米制 z/x_m
        /t_meet（v_close≤0=掠过=占用语义；估计窗未开=回退像素），无锚车走
        像素仪器；只活在米制里的远轨（x_lane 无效被降级 far 的车，恰是地平线
        退化带住户）同样入账——那是本次搬家要治的盲区本体。
        空旷路=合法结果（道间等价，pick=当前道）。"""
        if executed is None:
            return None
        half = road_width / 2.0 + 0.2 if road_width else 2.2
        need_of = lambda L: self._need(L, executed)

        # ---- 车流：路心系入道 + 到站时刻（pacing=整窗占用） ----
        lane_car_t: dict[float, list[float | None]] = {L: [] for L in LANES}
        by_id = {v.id: v for v in views}
        metric_seen: set[int] = set()
        for m in metric_cars:
            v = by_id.get(m.target_id)
            if v is not None and v.cy >= self.cal.v_ego:
                metric_seen.add(m.target_id)
                continue                # 已过自车行：不占道（米制无 cy，随视图判）
            if m.z_m is not None:
                x = (m.x_m / self.cal.lane_w_m if m.x_m is not None
                     else (self.scorer.cand_x(v) if v is not None else None))
                tm = self._metric_t(m, v)
            elif v is not None:
                x, tm = self._pixel_instruments(v)      # 无锚：像素回退
            else:
                continue                # 无视图的远轨必有锚（far 轨本就 x 无效）；
                                        # 连锚都没有时它在 obs 里也没参赌资格
            metric_seen.add(m.target_id)
            self._enter_lane(x, tm, executed, half, lane_car_t)
        for vid, v in by_id.items():
            if vid in metric_seen or v.cy >= self.cal.v_ego:
                continue                # 米制路径已入账，不双计；已过自车行不占道
            x, tm = self._pixel_instruments(v)
            self._enter_lane(x, tm, executed, half, lane_car_t)

        # ---- 币：组值 + 到站（与评分同一 t_miss 仪器） ----
        coin_items = []                     # (lane, group, t_miss|None)
        for g in obs.coin_groups:
            if g.x_center != g.x_center:
                continue                    # 存续组 nan 心：不入账
            xr = g.x_center + executed
            if abs(xr) > half:
                continue
            L = self._lane_of(xr)
            if L is None:
                continue
            s = self.scorer.score(g, obs)
            if s is None:
                continue                    # 置信地板（质量门语义一致）
            coin_items.append((L, g, s.t_miss_s))

        # ---- 逐道收益（事件语义） ----
        out: list[float] = []
        for L in LANES:
            need = need_of(L)
            # 我们到得了该道之后才到达的车才构成威胁/遮挡（t_car < need 的
            # 车在我们赶到前已过——不撞也不挡）
            tms = [t for t in lane_car_t[L] if t is None or t >= need]
            if any(t is not None and t < self.c.crash_t_s for t in tms):
                out.append(-self.c.crash_penalty)
                continue
            benefit = 0.0
            # 币：可收 = 到站 ≤ 窗且道内无「先于币到达」的车；远币记远权
            for cl, g, t_miss in coin_items:
                if cl != L:
                    continue
                val = (self.scorer.c.coin_value_per_unit
                       * g.observed_count * g.conf_min)
                if t_miss is not None and math.isfinite(t_miss) \
                        and t_miss <= self.c.horizon_s:
                    if not any(tt is None or tt <= t_miss for tt in tms):
                        benefit += val
                else:
                    benefit += self.c.far_coin_weight * val
            # 邻道错身价值：错身车（t_meet ≤ 窗）的相邻道，变道来得及才计
            for cl, tms_c in lane_car_t.items():
                if abs(cl - L) != 1.0:
                    continue
                if any(tt is not None and tt <= self.c.horizon_s
                       and tt >= need for tt in tms_c):
                    benefit += self.c.pass_value
            out.append(benefit)

        # ---- pick：argmax，平手取当前道 ----
        best_i = 0
        for i, b in enumerate(out):
            if b > out[best_i] + 1e-9:
                best_i = i
        pick = LANES[best_i]
        cur = self._lane_of(executed)
        if cur is not None and out[LANES.index(cur)] >= out[best_i] - 1e-9:
            pick = cur                     # 平手不换道（防抖默认）
        return LaneReport(tuple(out), pick, half)

    # ---- 内部 ----

    def _metric_t(self, m, v) -> float | None:
        """米制到站时刻（分流语义见 MetricCarState docstring）：
        v_close>0 → z/v_close；v_close≤0 → None（掠过/远离=占用）；
        v_close None（锚新估计窗未开）→ 像素回退（有视图才退得动）。"""
        if m.v_close_mps is not None:
            return m.t_meet_s
        if v is None:
            return None
        return (self.scorer.car_t_meet_s(v.rel_approach, v.cy)
                if v.rel_approach > 0 else None)

    def _pixel_instruments(self, v) -> tuple[float, float | None]:
        """像素仪器（回退路径，与搬家前逐位同式）：cand_x（成员位优先）+
        地面流 t_meet（pacing=非正 rel → None=占用）。"""
        x = self.scorer.cand_x(v)
        tm = (self.scorer.car_t_meet_s(v.rel_approach, v.cy)
              if v.rel_approach > 0 else None)
        return x, tm

    def _enter_lane(self, x: float | None, tm: float | None,
                    executed: float, half: float,
                    lane_car_t: dict[float, list[float | None]]) -> None:
        """路心系横坐标入道格（病态行/出界不入账——与搬家前同一判据面；
        米制 x 不经过成员位投票，米制读数本身即整道归属）。已过自车行的
        排除在调用侧做（只有那里知道 cy）。"""
        if x is None or x != x:
            return                            # 缺读数/nan（病态行）不入账
        xr = x + executed
        if abs(xr) > half:
            return                            # 出界=膨胀读数，不入道格
        L = self._lane_of(xr)
        if L is None:
            return
        lane_car_t[L].append(tm)


    @staticmethod
    def _lane_of(xr: float) -> float | None:
        """路心系横坐标 → 道心（±0.75 内归属；出界由调用侧半宽门先挡）。"""
        best = min(LANES, key=lambda L: abs(xr - L))
        return best if abs(xr - best) <= LANE_HALF else None

    def _need(self, lane: float, executed: float) -> float:
        """到该道的变道需求（秒）：timing 线性模型，单一真源不复制。"""
        return self._cfg.timing.lane_change_duration_s(abs(lane - executed))
