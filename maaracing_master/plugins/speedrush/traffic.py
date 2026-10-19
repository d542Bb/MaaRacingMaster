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
- **推定超越（presumed_pass）**：track 死亡时最后一次**真实**读数已贴身
  （cy ≥ presumed_cy_px）且在邻道内（|x_lane| ≤ presumed_lane_max）且仍在
  下滑（rel > 0，消失方向指向下缘）——判"正在被超越时消失"。动机：检测
  下界（cy≈575，自车遮挡+画角裁切）高于 636 判据线，真检永远到不了下带，
  51/51 pass 全靠遮挡外推跨线、慢速超越系统性记 lost（2026-10-08 取证，
  设计稿 §4A；判据预演 26 lost 晋升 16、远车零泄漏、pass 组 51/51 自洽）。
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

import math
from dataclasses import dataclass, field

from maaracing_master.plugins.speedrush.config import Traffic
from maaracing_master.plugins.speedrush.tracking import (
    WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_CAR, Calib, load_calib)

OUTCOME_PASS = "pass"
OUTCOME_PRESUMED_PASS = "presumed_pass"
OUTCOME_LOST = "lost"
OUTCOME_GHOST = "ghost"


@dataclass(frozen=True)
class CarView:
    """在途车辆实时视图（step 2 的候选生成吃这个，字段先给最小集）。

    ``x_memb``：车道成员资格位（整数道中心的 A1 读数，右正；生命期投票
    2026-10-11）。近场单拍读数噪声/膨胀 ±0.5 道量级撑不起接近段的带门——
    成员位是 track 生命期统计+车道先验（整道栅格）投票的产物，投票不成立
    （证据不足）为 None，消费方退回单拍 x_lane 不装懂。安全几何（横向
    veto、紧急回避的车体判距）一律仍吃 x_lane 原始物理位。
    ``w_m``/``w_m_age``：框宽地面尺换算的米宽估计与龄（tick，None=从未有
    效/已过期显式转 unknown）。只在真实读数拍按有效性门（年龄/置信/分母）
    重算，遮挡延续拍保持最后有效值并带龄；过期不冒充有效。"""

    id: int
    x_lane: float
    cy: int
    rel_approach: float      # px/fid，正=接近（Tracker EMA 透传；单位陷阱
                             # 见 tracking.TrackedTarget）
    d_min: float             # 自 track 出现以来的 min|x_lane|
    age_ticks: int
    v_lat: float = 0.0       # 横向速率（车道/tick，右正；帧间差分 EMA——阶段一
                             # CV 外推/横向安全 veto 的数据源，缺省 0=静止假设）
    x_memb: float | None = None
    w_m: float | None = None
    w_m_age: int | None = None
    # 最后一次真实读数帧号（0=旧构造未带）。决策层用它算"观测新鲜度"；
    # age_ticks 是轨道生命期（first_fid 起算），不是新鲜度——拿它当龄会
    # 让一辆持续被检出的车在 0.25s 后从场景里蒸发。
    last_seen_fid: int = 0


@dataclass(frozen=True)
class PassEvent:
    """track 出画落定事件（一次性；d_min 仅 pass/presumed_pass 有效）。"""

    track_id: int
    outcome: str             # OUTCOME_PASS / OUTCOME_PRESUMED_PASS / OUTCOME_LOST /
                             # OUTCOME_GHOST
    settled_fid: int         # 检出消失的拍号（≈ grace 过期帧，非真实越线帧——
                             #  离线配对 d_min/速度时按 settle − grace 回推）
    d_min: float | None
    age_ticks: int


@dataclass
class _Live:
    """内部活体账本（不出契约）：x_lane 读数在遮挡外推拍会重复，d_min 取 min 即可。

    车道成员资格（2026-10-11）：``votes``=整道票仓（道号→衰减质量，只在真实
    读数拍衰减+投票——遮挡期不衰减，宽限内成员位保持）；``memb``=当前成员
    位（票仓最大质量达下限才成立，None=证据不足）。账本随 track id 生灭，
    track 切换不继承错位（身份连续性边界）。
    宽度有效性（2026-10-11）：``w_m``=最后有效读数的米宽（只按有效性门在
    真实读数拍重算，遮挡延续拍不重算——外推 cy 投影正是 3.6→0.3m 崩坏的
    机制）；``w_m_fid``=该读数的拍号，TTL 由消费侧按龄判。"""

    first_fid: int
    n_obs: int
    last_cy: float
    last_cy_real: float      # 最后一次真实读数的 cy（遮挡延续拍不更新——
                             # presumed 判据吃真检位，外推值会漏进远车）
    rel: float               # rel_approach 直接透传（不二次平滑：EMA 在 Tracker 已做）
    d_min: float
    last_x: float | None = None   # 上次真实读数的 x_lane（横向速率差分基）
    v_lat: float = 0.0            # 横向速率 EMA（车道/tick；只在真实读数拍更新）
    votes: dict[int, float] = field(default_factory=dict)
    memb: int | None = None
    w_m: float | None = None
    w_m_fid: int | None = None


class TrafficObserver:
    """每 tick update(obs) 一次；与 Tracker 同一驱动节奏，单线程 owner 使用。

    ``conf_floor``：宽度有效性门的置信地板（真源=scoring.conf_floor，由
    装配侧传入——本层不建第二份真值；None=宽度面整体不出，CarView.w_m
    恒 None，诚实降级）。"""

    def __init__(self, p: Traffic, cal: Calib | None = None,
                 conf_floor: float | None = None):
        self.p = p
        self.cal = cal if cal is not None else load_calib()
        self._conf_floor = conf_floor
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
                            last_cy_real=t.cy,
                            rel=t.rel_approach, d_min=abs(t.x_lane))
                self._live[t.id] = rec
            rec.n_obs += 1
            rec.last_cy = t.cy
            rec.rel = t.rel_approach
            rec.d_min = min(rec.d_min, abs(t.x_lane))
            # 横向速率只在真实读数拍差分（遮挡延续拍 x 是陈旧重复值，投毒 EMA）
            if t.last_seen_fid == fid:
                rec.last_cy_real = t.cy
                if rec.last_x is not None:
                    inst = t.x_lane - rec.last_x
                    rec.v_lat += self.p.v_lat_ema_alpha * (inst - rec.v_lat)
                rec.last_x = t.x_lane
                self._vote_lane(rec, t)
                self._refresh_width(rec, t, fid)
            memb = rec.memb
            w_m, w_m_age = rec.w_m, None
            if w_m is not None:
                age = fid - rec.w_m_fid
                # TTL 过期显式转 unknown（不得冒充有效）；账本保留，重捕后
                # 新有效读数刷新即恢复
                if age <= self.p.width_ttl_ticks:
                    w_m_age = age
                else:
                    w_m = None
            views.append(CarView(id=t.id, x_lane=t.x_lane, cy=t.cy,
                                 rel_approach=t.rel_approach, d_min=rec.d_min,
                                 age_ticks=fid - rec.first_fid, v_lat=rec.v_lat,
                                 x_memb=memb, w_m=w_m, w_m_age=w_m_age,
                                 last_seen_fid=t.last_seen_fid))

        events: list[PassEvent] = []
        for tid in sorted(set(self._live) - seen):
            rec = self._live.pop(tid)
            outcome = self._classify(rec, fid)
            if outcome is not None:
                events.append(PassEvent(
                    track_id=tid, outcome=outcome, settled_fid=fid,
                    d_min=rec.d_min if outcome in (OUTCOME_PASS,
                                                   OUTCOME_PRESUMED_PASS) else None,
                    age_ticks=fid - rec.first_fid))

        self._last_fid = fid
        self._last_result = (tuple(views), tuple(events))
        return self._last_result

    def _vote_lane(self, rec: _Live, t) -> None:
        """车道成员资格投票（2026-10-11，只在真实读数拍）。

        单拍 x_lane 按车道先验（整道栅格：ego=0，邻道 ±1，A1 尺子单位=道宽）
        取整投票；票仓按衰减系数遗忘——真横移的连续新票在数拍内压过衰减的
        旧票（及时响应边界），偶发噪声票停留少数派（±0.5 道读数噪声不翻
        成员位）。读数 σ 超门不投票：远场/弯道失真读数不进票仓（近场保守，
        弯道退化的安全形态）。最大质量达下限成员位才成立，否则 None=消费
        方退回单拍读数（证据不足不装懂）。"""
        if t.x_sigma > self.p.lane_vote_sigma_max:
            return
        dec = self.p.lane_vote_decay
        lane = math.floor(t.x_lane + 0.5)
        votes = {l: w * dec for l, w in rec.votes.items()}
        votes[lane] = votes.get(lane, 0.0) + 1.0
        rec.votes = votes
        best_lane, best_w = max(votes.items(), key=lambda kv: kv[1])
        rec.memb = (best_lane if best_w >= self.p.lane_memb_min_weight
                    else None)

    def _refresh_width(self, rec: _Live, t, fid: int) -> None:
        """宽度有效读数刷新（2026-10-11，只在真实读数拍）。

        有效性门（计划 §2③/§4）：track 年龄 ≥ min_obs_ticks、conf ≥ 置信
        地板、归一分母在适用域——三条全过才重算 w_m（框宽×地面尺/分母，
        与账本 w_m 同式单一真源）。遮挡延续拍不重算：外推 cy 配陈旧框宽
        正是遮挡崩坏（3.6→0.3m）的机制。TTL 过期在 CarView 装配处显式转
        unknown。"""
        if self._conf_floor is None or t.conf < self._conf_floor:
            return
        if fid - rec.first_fid < self.p.min_obs_ticks:
            return
        denom = t.cy - self.cal.y_h
        if denom < self.cal.min_denom:
            return
        rec.w_m = t.w * self.cal.a_x * self.cal.lane_w_m / denom
        rec.w_m_fid = fid

    def _classify(self, rec: _Live, fid: int) -> str | None:
        """消失落定分类（设计稿 §三判据表的实现位）。"""
        if rec.n_obs < self.p.min_obs_ticks:
            return None                       # 闪现噪声：不记账
        bottom_exit = rec.last_cy >= self.cal.v_ego - self.p.exit_margin_px
        if not bottom_exit:
            if (rec.last_cy_real >= self.p.presumed_cy_px
                    and rec.last_x is not None
                    and abs(rec.last_x) <= self.p.presumed_lane_max
                    and rec.rel > 0):
                return OUTCOME_PRESUMED_PASS
            return OUTCOME_LOST               # 远端/侧向消失：没超成
        if ((fid - rec.first_fid) >= self.p.ghost_max_age_ticks
                and abs(rec.rel) < self.p.ghost_rel_eps):
            return OUTCOME_GHOST              # 收敛点假轨迹签名
        return OUTCOME_PASS
