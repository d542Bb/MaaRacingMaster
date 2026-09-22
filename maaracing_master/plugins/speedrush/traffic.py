# -*- coding: utf-8 -*-
"""车流观测层（阶段 C 设计稿 §十 step 1）：车辆轨迹 → pass 事件 + 在途车辆视图。

**分层归属**（红线 1）：只消费 ``tracking.WorldObservation`` 里 kind==car 的轨迹，
**不新建第二套跟踪**——跨帧关联、遮挡外推、grace 过期全部依赖 Tracker 已发的 id
（生态两问②：派生不平行，禁止第二份真相）。消费者是评分层（设计稿 §二，step 2 接线）；
planner 不读本层。

**pass 判据**（设计稿 §三；起值 [需实测 C5] 全部落 decision.json traffic 段，
本文件不写字面量）：
- **超车完成**：track 于自车行下带消失（last cy ≥ v_ego − exit_margin_px）。
  游戏语义：街车从屏幕下沿出画 = 我们越过了它。
- **没超成（lost）**：其余位置消失——**保守不得分**（错判的 d_min 会污染 §二 的
  期望收益与 C4 标定面；漏判不损失任何东西，密度排序自然会再选中别的目标车）。
- **鬼影（ghost）**：长寿命、相对速率近零、最后却"从下面消失"（雨测 191 帧收敛点
  假轨迹案，README §7.5）→ 单独 outcome，不当 pass 也不进 lost 账。
- 观测次数不足 min_obs_ticks 的闪现（检测噪声）：**不发事件**（宁缺毋滥）。

**d_min 口径**：track 全寿命 min|x_lane|（A1 归一最近横向间距）——与 RULES §4.2
"最近归一间距 0.65/0.81"两档同一测量口径（trick 实验方法），pass 事件自带 d_min
即 C4（P_limit 形状）的标定数据源，**不需要额外采数**。
"""

from __future__ import annotations

from dataclasses import dataclass

from maaracing_master.plugins.speedrush.config import Traffic
from maaracing_master.plugins.speedrush.tracking import (
    TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_CAR, Calib, load_calib)

OUTCOME_PASS = "pass"
OUTCOME_LOST = "lost"
OUTCOME_GHOST = "ghost"


@dataclass(frozen=True)
class CarView:
    """在途车辆实时视图（step 2 的候选生成吃这个，字段先给最小集）。"""

    id: int
    x_lane: float
    cy: int
    rel_approach: float      # px/tick，正=接近（Tracker EMA 透传）
    d_min: float             # 自 track 出现以来的 min|x_lane|
    age_ticks: int


@dataclass(frozen=True)
class PassEvent:
    """track 出画落定事件（一次性；d_min 仅 pass 有效）。"""

    track_id: int
    outcome: str             # OUTCOME_PASS / OUTCOME_LOST / OUTCOME_GHOST
    settled_fid: int         # 检出消失的拍号（≈ grace 过期帧，非真实越线帧——
                             #  离线配对 d_min/速度时按 settle − grace 回推）
    d_min: float | None
    age_ticks: int


@dataclass
class _Live:
    """内部活体账本（不出契约）：x_lane 读数在遮挡外推拍会重复，d_min 取 min 即可。"""

    first_fid: int
    n_obs: int
    last_cy: float
    rel: float               # rel_approach 直接透传（不二次平滑：EMA 在 Tracker 已做）
    d_min: float


class TrafficObserver:
    """每 tick update(obs) 一次；与 Tracker 同一驱动节奏，单线程 owner 使用。"""

    def __init__(self, p: Traffic, cal: Calib | None = None):
        self.p = p
        self.cal = cal if cal is not None else load_calib()
        self.reset()

    def reset(self) -> None:
        self._live: dict[int, _Live] = {}
        self._last_fid: int | None = None
        self._last_result: tuple[tuple[CarView, ...], tuple[PassEvent, ...]] | None = None

    def update(self, obs: WorldObservation
               ) -> tuple[tuple[CarView, ...], tuple[PassEvent, ...]]:
        """→ (在途车辆视图, 本拍落定事件)。同帧重发幂等（WGC 缓存纪律与 Tracker 一致）；
        frame_id 倒退 fail-loud（真乱序输入，静默重放会把账本搅糊）。"""
        fid = obs.frame_id
        if self._last_fid is not None and fid < self._last_fid:
            raise ValueError(f"frame_id 倒退：{fid} < {self._last_fid}")
        if self._last_fid == fid and self._last_result is not None:
            return self._last_result

        seen: set[int] = set()
        views: list[CarView] = []
        for t in obs.targets:
            if t.kind != KIND_CAR:
                continue
            seen.add(t.id)
            rec = self._live.get(t.id)
            if rec is None:
                rec = _Live(first_fid=fid, n_obs=0, last_cy=t.cy,
                            rel=t.rel_approach, d_min=abs(t.x_lane))
                self._live[t.id] = rec
            rec.n_obs += 1
            rec.last_cy = t.cy
            rec.rel = t.rel_approach
            rec.d_min = min(rec.d_min, abs(t.x_lane))
            views.append(CarView(id=t.id, x_lane=t.x_lane, cy=t.cy,
                                 rel_approach=t.rel_approach, d_min=rec.d_min,
                                 age_ticks=fid - rec.first_fid))

        events: list[PassEvent] = []
        for tid in sorted(set(self._live) - seen):
            rec = self._live.pop(tid)
            outcome = self._classify(rec, fid)
            if outcome is not None:
                events.append(PassEvent(
                    track_id=tid, outcome=outcome, settled_fid=fid,
                    d_min=rec.d_min if outcome == OUTCOME_PASS else None,
                    age_ticks=fid - rec.first_fid))

        self._last_fid = fid
        self._last_result = (tuple(views), tuple(events))
        return self._last_result

    def _classify(self, rec: _Live, fid: int) -> str | None:
        """消失落定分类（设计稿 §三判据表的实现位）。"""
        if rec.n_obs < self.p.min_obs_ticks:
            return None                       # 闪现噪声：不记账
        bottom_exit = rec.last_cy >= self.cal.v_ego - self.p.exit_margin_px
        if not bottom_exit:
            return OUTCOME_LOST               # 远端/侧向消失：没超成
        if ((fid - rec.first_fid) >= self.p.ghost_max_age_ticks
                and abs(rec.rel) < self.p.ghost_rel_eps):
            return OUTCOME_GHOST              # 收敛点假轨迹签名
        return OUTCOME_PASS
