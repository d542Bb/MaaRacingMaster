# -*- coding: utf-8 -*-
"""计划层选择域（设计稿 docs/plan/speedrush-plan-layer-design.md v2 的实现位）：
三类字典序（bonus＞极限超车＞coin）+ 计划承诺 + 持续性反抖。

**选择无标定数值**（设计稿 P2）：类间靠字典序、类内靠到达时间 + 几何破平，
min_score/life_tau/switch_margin 退出选择链。本模块只在 `plan_layer.enabled`
时被调用（decision._select 头部分流）；闸关时 decision.py 旧路径逐位不变。

**候选资格**（设计稿 §四.2；全部复用既有量，不建第二套判据）：
- BONUS  obs.targets 按 kind==bonus 过滤（撞上去，维护者裁定 2026-10-05）：
  track 年龄≥min_obs、接近中、到达可行性（变道耗时+响应+余量 < 到站时间）。
  bonus 不在车流 views 里（TrafficObserver 只收 kind==car），横向 veto 与轨迹
  采样**天然看不见目标本体**——接触豁免由构造保证，不需要豁免集机制。
- OVERTAKE 现有候选带+接近方向（scorer.score_car 资格面）+到达可行性+veto+侧空间。
- COIN    聚合组置信地板（scorer.score 资格面，质量门保留）+时机可行性+veto。

类内排序=到达时间升序；破平：超车按所需横移小者优先、金币按枚数多者优先。
跨类「顺路扫币」破平（设计稿 §四.2）V1 简化不做：字典序下跨类只有严格胜出，
等价破平需要同类多候选各自带扫币账，等实机证据显示该场景真实出现再做。

**承诺与反抖**（P3）：CRUISE（无计划）即时选；CHANGE 改判走慢拍（_REEVAL_S）
+持续性门槛（新候选连续 K 次慢拍重评仍居其位才换）。旧 switch_margin 是
分数单位，选择器取消分数后不可再用（设计稿 §三.1）；t_cool 时间冷却沿用。

**contact 走廊分层（设计稿 §五）V1 简化**：只有 BONUS 计划声明接触，且豁免由
构造保证（见上）。街车 contact 层需要 LateralSafety 暴露「拦截车 id」才能
豁免（现只返回 reason 串），暂缓——等实机证据显示走廊被街车判死造成 ABORT
循环真实发生再做；现结构下街车接触本就不可能被计划选中执行（§五结构解的
强形态，比设计稿更保守）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from maaracing_master.plugins.speedrush.config import DecisionConfig
from maaracing_master.plugins.speedrush.tracking import WorldObservation
from maaracing_master.plugins.speedrush.world_model import KIND_BONUS, Calib

if TYPE_CHECKING:  # 循环导入只活在类型检查期：Scorer 留在 decision.py（执行态机侧）
    from maaracing_master.plugins.speedrush.coin_group import CoinGroup
    from maaracing_master.plugins.speedrush.decision import Scorer
    from maaracing_master.plugins.speedrush.traffic import CarView

KIND_CAND_COIN = "coin"
KIND_CAND_OVERTAKE = "overtake"
KIND_CAND_BONUS = "bonus"          # 计划层新增类（撞上去，维护者裁定 2026-10-05）

_REEVAL_S = 0.5                    # CHANGE 改判慢拍周期（设计稿 §二：0.5s 兜底）


@dataclass(frozen=True)
class Scored:
    """一次候选评定的完整依据（原 decision.py 住户，随选择域迁入本模块）。

    kind 判别三类：coin（group）/ overtake、bonus（car）。plan_layer 闸开时
    score 只是名义值（排序靠字典序+到达时间，分数不参与比较）；FSM 的
    _cur_score 记账与 legacy 改判路仍消费它。"""

    group: "CoinGroup | None"
    score: float
    t_miss_s: float
    kind: str = KIND_CAND_COIN
    car: "CarView | None" = None

    @property
    def cand_id(self) -> int:
        """跨类统一的候选标识（coin=组号 / car 类=track_id，两 id 空间由 kind 分隔）。"""
        if self.kind == KIND_CAND_COIN:
            return self.group.group_id
        return self.car.id


class PlanSelector:
    """三类字典序选择门（设计稿 §四.2）。纯编排：资格判据全部复用
    Scorer/LateralSafety/timing 既有实现，本类不复制任何几何/时机公式。"""

    def __init__(self, cfg: DecisionConfig, cal: Calib,
                 scorer: "Scorer", lateral) -> None:
        self._cfg = cfg
        self._cal = cal
        self._scorer = scorer
        self._lateral = lateral
        self._reeval_t = 0.0        # CHANGE 慢拍计时（累计 dt，到 _REEVAL_S 重评）
        self._streak_key: tuple[str, int] | None = None
        self._streak_n = 0

    def reset(self) -> None:
        """阶段切换/保守态出口与引擎 reset 同步清（计划承诺不跨阶段）。"""
        self._reeval_t = 0.0
        self._streak_key = None
        self._streak_n = 0

    # ---- 对外唯一入口（decision._select 分流调用）----

    def select(self, obs: WorldObservation, views, executed: float | None, *,
               current: tuple[str, int] | None, cooling: bool,
               done_pass_ids, side_has_space, dt_s: float) -> Scored | None:
        """一次选择询问。

        current=None（CRUISE 无计划）：即时选最优，不吃持续性门（没有要保护
        的承诺，抖不动任何东西）；非空（CHANGE 改判）：慢拍 + 持续性门槛。
        返回 Scored=选/换此候选；None=维持现计划或无候选。"""
        if cooling:
            self.reset()
            return None
        if current is not None:
            self._reeval_t += dt_s
            if self._reeval_t < _REEVAL_S:
                return None               # 慢拍未到：维持现计划（时间尺度分层）
            self._reeval_t = 0.0
        best = self._best(obs, views, executed, done_pass_ids, side_has_space)
        if best is None:
            self.reset()
            return None
        if current is None:
            return best
        key = (best.kind, best.cand_id)
        if key == current:
            self.reset()
            return None                   # 现计划仍是首选：维持（承诺不对称的收益侧）
        if key != self._streak_key:
            self._streak_key = key
            self._streak_n = 1
        else:
            self._streak_n += 1
        if self._streak_n >= self._cfg.plan_layer.switch_streak:
            self.reset()
            return best                   # 持续性达标：换计划（升级/同类换目标同门）
        return None                       # 换计划举证中：维持现计划

    # ---- 资格与排序（设计稿 §四.2 选择门）----

    def _best(self, obs: WorldObservation, views, executed: float | None,
              done_pass_ids, side_has_space) -> Scored | None:
        start = executed if executed is not None else 0.0
        t = self._cfg.timing
        hz = self._cfg.control.frame_rate_hz

        def _feasible(goal: float, t_miss: float) -> bool:
            need = t.lane_change_duration_s(goal - start)
            return need + t.tau_resp_s + t.margin_s < t_miss

        # BONUS：撞上去（目标本体不在 views——veto/采样天然不拦，见模块头注）
        bonus: list[Scored] = []
        for b in obs.targets:
            if b.kind != KIND_BONUS:
                continue                  # 观测契约=混合 targets，按 kind 过滤
            if obs.frame_id - b.first_seen_fid < self._cfg.traffic.min_obs_ticks:
                continue                  # track 年龄门槛：单帧噪声进不了选择门
            if b.rel_approach <= 0.0:
                continue                  # 不接近=永远撞不上
            rate = b.rel_approach * hz
            t_meet = max(0.0, self._cal.v_ego - b.cy) / rate
            if not _feasible(b.x_lane, t_meet):
                continue                  # 它到站前我们到不了它的道=撞不上
            bonus.append(Scored(group=None, score=self._cfg.hysteresis.min_score,
                                t_miss_s=t_meet, kind=KIND_CAND_BONUS, car=b))
        if bonus:
            bonus.sort(key=lambda s: s.t_miss_s)
            return bonus[0]               # 字典序最高类有候选即胜出

        overtake: list[Scored] = []
        for v in views:
            if v.id in done_pass_ids:
                continue
            s = self._scorer.score_car(v)
            if s is None:
                continue                  # 候选带/接近方向资格（分值不参与排序）
            goal = self._scorer.hug_goal(v.x_lane, ref=start)
            if not _feasible(goal, s.t_miss_s):
                continue
            if executed is not None and \
                    self._lateral.veto_reason(executed, goal, views) is not None:
                continue
            if not side_has_space(v.x_lane):
                continue
            overtake.append(Scored(group=None, score=s.score,
                                   t_miss_s=s.t_miss_s,
                                   kind=KIND_CAND_OVERTAKE, car=v))
        if overtake:
            overtake.sort(key=lambda s: (
                s.t_miss_s,
                abs(self._scorer.hug_goal(s.car.x_lane, ref=start) - start)))
            return overtake[0]

        coin: list[Scored] = []
        for g in obs.coin_groups:
            s = self._scorer.score(g, obs)
            if s is None:
                continue                  # 置信地板=质量门，保留（非分数门槛）
            if g.validity_until_fid <= obs.frame_id:
                continue
            if not _feasible(g.x_center, s.t_miss_s):
                continue
            if executed is not None and \
                    self._lateral.veto_reason(executed, g.x_center, views) is not None:
                continue
            coin.append(s)
        if coin:
            coin.sort(key=lambda s: (s.t_miss_s, -s.group.observed_count))
            return coin[0]
        return None
