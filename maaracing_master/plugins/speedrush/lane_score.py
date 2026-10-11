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

**时间口径**：车 t_meet 走 `Scorer.car_t_meet_s`（地面流积分+聚合式 fid 率，
单一公式真源）；币到站走 `Scorer.score` 的 t_miss（与评分同一仪器——coins
的全场流 k 仍是 rel×hz 旧时基（挂账），本模块继承同一偏差，不另建第二份）。

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
                 *, road_width: float | None) -> LaneReport | None:
        """一拍评定。executed 缺席=无路心锚（诚实弃权，None 不臆造道格）；
        road_width 缺席用 4.4 道先验半宽。

        ``views``：CarView 序列（鸭子同构可——cand_x/t_meet 走 scorer 通道）。
        空旷路=合法结果（道间等价，pick=当前道）。"""
        if executed is None:
            return None
        half = road_width / 2.0 + 0.2 if road_width else 2.2
        need_of = lambda L: self._need(L, executed)

        # ---- 车流：路心系入道 + 到站时刻（pacing=整窗占用） ----
        lane_car_t: dict[float, list[float | None]] = {L: [] for L in LANES}
        for v in views:
            x = self.scorer.cand_x(v)
            if x != x:
                continue                    # nan（病态行）不入账
            xr = x + executed
            if abs(xr) > half:
                continue                    # 出界=膨胀读数，不入道格
            L = self._lane_of(xr)
            if L is None or v.cy >= self.cal.v_ego:
                continue                    # 出格/已过自车行：不占道
            tm = (self.scorer.car_t_meet_s(v.rel_approach, v.cy)
                  if v.rel_approach > 0 else None)
            lane_car_t[L].append(tm)

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

    @staticmethod
    def _lane_of(xr: float) -> float | None:
        """路心系横坐标 → 道心（±0.75 内归属；出界由调用侧半宽门先挡）。"""
        best = min(LANES, key=lambda L: abs(xr - L))
        return best if abs(xr - best) <= LANE_HALF else None

    def _need(self, lane: float, executed: float) -> float:
        """到该道的变道需求（秒）：timing 线性模型，单一真源不复制。"""
        return self._cfg.timing.lane_change_duration_s(abs(lane - executed))
