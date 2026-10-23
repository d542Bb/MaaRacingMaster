# -*- coding: utf-8 -*-
"""路线承诺层（2026-10-23 V1）：跨帧唯一的横向意图持有者。

**它是什么**：跨帧持有「参考道 / 并线目标道 / 阶段 / 定案时刻」，每拍只判三件事
——能不能起意、要不要继续、何时算完成。接控制的车道意图从这里读，不再逐帧
重选。三条互斥定案路径：

1. **紧急逃生**：现任道上有**确认**占用/收窄证据（窗内被截住）→ 立即改道，
   可突破承诺期与收益门槛；没有"逃得进去"的替代道 → 参考道不动，只记
   `urgent_no_alt` 与证据距离（无完全可行替代时的恢复动作属后续独立步骤）。
2. **普通换道**：承诺期已过 **且** 不在取消冷却 **且** 收益超门槛 **且** 整段
   并线检查放行。到期只解锁重新评估，**不构成换道理由**。
3. **保持**：以上都不成立。窗口不足就**推迟**（下一拍重评），不翻面重选。

**它不是什么**：不算轨迹、不判精确碰撞、不重算中心线。占用与净空全部吃上游
粗层的**原始候选报告**——那是同一份净空真源，本层不建第二份，也不回读已经
接控制的道号（承诺值喂回候选评估会让迟滞语义在两层之间漂移）。

占用三态（互不等价，`无法确认` 永远不等于 `确认空闲`）：
- `确认空闲`：窗内无占用证据 → 整段检查直接给过（可用时间无限）；
- `被占`：确认有车（有时刻）或走廊收窄到走不下。车**远到够并完**的目标道仍然
  合格——替代道按时间判，不按"有没有车"一刀切；现任道上的确认占用是紧急证据；
- `无法确认`：有车但缺纵深/到站时间 → 不能作替代道（整段检查无从判可完成），
  也不单独构成紧急理由（紧急必须由现任道的正面危险证据触发）。

坐标与时间：道号沿**上游粗层帧**（相对场景 z=0 的分带中心线）持有——与两个
控制消费者今天读的原点逐位一致，本层不做第二次换算。时钟用 tick 累计秒
（`time.monotonic` 不进本模块：回放与实机同一确定性）。米→秒用粗层既有的
折算常数代理，不新造自车速度真源——本系统没有纵向速度观测，「净空米 →
可用秒」只能是代理口径，这点在 trace 里如实暴露。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.coarse import CoarseLane, CoarseReport

INF = math.inf

PHASE_KEEP = "keep"
PHASE_MERGING = "merging"

# 截断来源分类（沿用粗层 CoarseLane.blocker 的语义，不另立枚举真源）：
# None=确认空闲；"space"/"car"=确认被占；"unknown_car"=无法确认。
KNOWN_BLOCKERS = ("space", "car")


@dataclass(frozen=True)
class RouteParams:
    """路线层参数（真源 decision.json [route]；尺度/速度类复用既有真源）。"""

    commit_s: float = 1.5            # 承诺期：定案后多久才允许普通换道重评
    switch_gain_m: float = 4.0       # 净空收益门槛（与粗层换道迟滞同源一个数）
    t_meas_per_lane_s: float = 3.0   # 放行用保守完成时间：人工观测支持的**暂定
    # 初值**，未覆盖观测尾部。它的作用是提前排除安全窗口不足的起意，**不是**
    # 命令车辆花 3 秒并线（V1 不改执行层，车仍可能在 1 秒内横移完）
    v_lat_max_lane_s: float = 2.5    # 执行层横向速率上限（复用 [planner] 真源）
    proxy_speed_mps: float = 45.0    # 米→秒折算代理（复用 [coarse] 真源）
    t_urgent_s: float = 0.35         # 现任道可用时间低于此 = 紧急逃生
    cancel_unsafe_ticks: int = 5     # 取消判定的连续帧计数（防单帧野值弃窗）
    cancel_cooldown_s: float = 2.0   # 取消后的冷却（期内只许紧急逃生）。比承诺期
    # 长才有意义：两者相等时冷却总被承诺门先遮住，这条规则不可观测
    finish_tol_lane: float = 0.3     # 完成的位置容差（同 L4 中心带宽）
    finish_v_tol_lane_s: float = 0.4  # 完成的横速容差：带着横速"完成"正是过冲
    merge_max_s: float = 4.0         # 并线超时（人工 p90 量级）：到点按实测
    # 所在道落回 KEEP——卡在 MERGING 会让承诺期永不重启


@dataclass(frozen=True)
class RouteState:
    """一拍接一拍的路线承诺状态（frozen；推进返回新值，便于单测与回放）。"""

    lane_ref: float                  # 命令认为车应该在的道（相对粗层原点）
    lane_from: float                 # 本次并线的出发道（取消时回接）
    lane_target: float | None        # 并线目标道；None=无并线
    phase: str                       # keep / merging
    decided_t: float                 # 上次定案时刻（tick 累计秒）
    elapsed_s: float                 # 时钟本身（承诺期/冷却按时间走，不靠拍数）
    cooldown_left_s: float           # 取消冷却剩余
    unsafe_ticks: int                # 目标道危险证据的连续帧计数
    epoch: int | None                # 定案时的锚点纪元；与当拍不等 = 换纪作废
    v_ref: float                     # 上一拍实测横位（道，粗层帧）
    merge_started_t: float           # 本次并线起意时刻（超时判据用）
    v_est_lane_s: float              # 实测横位差分（道/s）：完成判据的速率面


@dataclass(frozen=True)
class RouteVerdict:
    """一拍定案结果（trace 观测面：离线要能复算为什么放行/为什么被拒）。"""

    phase: str
    lane_ref: float | None
    lane_target: float | None
    verdict: str                     # init/keep/merge_start/finish/cancel/
    # urgent_no_alt/epoch_reset/needs_init
    why: str                         # 保持或被拒的第一原因码
    commit_left_s: float
    gain_m: float | None
    t_need_s: float | None
    t_avail_s: float | None
    cooldown_left_s: float
    evidence_m: float | None         # 危险证据距离（米；紧急分支必带）
    epoch: int | None

    def as_trace(self) -> dict:
        def _r(x, nd):
            return None if x is None or not math.isfinite(x) else round(x, nd)
        return {"ph": self.phase, "ref": _r(self.lane_ref, 3),
                "tgt": _r(self.lane_target, 3), "vd": self.verdict,
                "why": self.why, "cl": _r(self.commit_left_s, 2),
                "gain": _r(self.gain_m, 2), "tn": _r(self.t_need_s, 3),
                "ta": _r(self.t_avail_s, 3), "cd": _r(self.cooldown_left_s, 2),
                "ev": _r(self.evidence_m, 2), "ep": self.epoch}


def init_state(epoch: int | None) -> RouteState:
    """冷启动参考道=路心道（0）。

    没有承诺时不动横向：0 道就是选择帧中心线本身，与粗层不接控制时的 L4
    回中逐位同形——V1 的行为差异只该来自"道号被承诺门控"，不该来自启动瞬间
    凭空挑一条最近的道压过去。作废/换纪的降级另走 `_rebuild`（那里可能有
    未完成的动作要保护，落回实测所在道）。"""
    return RouteState(0.0, 0.0, None, PHASE_KEEP, 0.0, 0.0, 0.0, 0, epoch,
                      0.0, 0.0, 0.0)


def _avail_s(c: CoarseLane | None, p: RouteParams) -> float | None:
    """该道最早占用时刻（秒，代理速度折算）。None=无法确认（有证据没时刻）；
    INF=窗内没有占用证据（确认空闲，按观察窗口径诚实，不冒充窗外已知）。"""
    if c is None or c.blocker == "unknown_car":
        return None
    if c.blocker is None:
        return INF
    return max(0.0, c.free_m) / p.proxy_speed_mps


def _known_blocked(c: CoarseLane | None) -> bool:
    return c is not None and c.blocker in KNOWN_BLOCKERS


def _qualified(c: CoarseLane | None) -> bool:
    """替代道资格：确认空闲、或有车但留给整段检查裁决。几何走不下（space）与
    无法确认（unknown_car）直接不合格——后者不是"更保守"，是"没有判据"。"""
    return c is not None and c.blocker not in ("space", "unknown_car")


def _clear(c: CoarseLane | None) -> bool:
    return c is not None and c.blocker is None


def _clamp_lane(ego_lane: float, by: dict) -> float:
    """实测横位落进候选道集合：取离它最近的候选道（候选是整数道，必然非空）。"""
    k = float(round(ego_lane))
    return min(by, key=lambda x: abs(x - k))


def _merge_times(d_lane: float,
                 p: RouteParams) -> tuple[float, float]:
    """(最快可能用时, 放行用保守用时)。下界只用于**否决**明显不可能的起意，
    放行要的是保守值——实机并线中位 0.47 s 正跑在下界上（横向速率上限的理论上
    界 0.4 s），用下界放行等于放过一切"来不及"的起意。"""
    t_lower = d_lane / p.v_lat_max_lane_s if p.v_lat_max_lane_s > 0 else INF
    return t_lower, max(t_lower, p.t_meas_per_lane_s * d_lane)


def tick_route(state: RouteState | None, report: CoarseReport | None,
               p: RouteParams, *, dt_s: float,
               epoch: int | None) -> tuple[RouteState | None, RouteVerdict]:
    """推进一拍。

    `report`：当拍**原始**粗层报告（逐道净空/占用三态），不是接控制的道号。
    `epoch`：供数门锚点纪元；None = 该拍没有可用路心锚点。
    返回 (新状态, 定案报告)。状态为 None 或报告为 None 时给 `needs_init`——
    **不回退旧坐标**（旧道号在新参考系里没有原意）。
    """
    if report is None or not report.lanes or report.reason == "no_road":
        # 没有车道级证据：时钟继续走（承诺期是时间量，不是"有证据的拍数"），
        # 但路线不推进、不判紧急——证据缺席不等于危险解除
        if state is None:
            return None, _needs_init(state, epoch)
        return replace(state, elapsed_s=state.elapsed_s + max(dt_s, 0.0)), \
            _needs_init(state, epoch)
    if state is None:
        s = init_state(epoch)
        return s, RouteVerdict(PHASE_KEEP, s.lane_ref, None, "init", "cold",
                               p.commit_s, None, None, None, 0.0, None, epoch)
    if epoch is None:
        # 缺锚点拍：参考道交回路心道（0=选择帧中心线，不动横向），不拿旧道号
        # 做判据——旧编号在这个参考系里没有原意；时钟继续走
        clock = state.elapsed_s + max(dt_s, 0.0)
        s = replace(state, lane_ref=0.0, lane_from=0.0, lane_target=None,
                    phase=PHASE_KEEP, decided_t=clock, unsafe_ticks=0,
                    epoch=None, merge_started_t=clock)
        return s, RouteVerdict(PHASE_KEEP, 0.0, None, "needs_init",
                               "no_anchor", p.commit_s, None, None, None,
                               s.cooldown_left_s, None, epoch)

    v_est = ((report.ego_lane - state.v_ref) / dt_s
             if dt_s > 0.0 else state.v_est_lane_s)
    clock = state.elapsed_s + max(dt_s, 0.0)
    s = replace(state, elapsed_s=clock, v_ref=report.ego_lane,
                v_est_lane_s=v_est,
                cooldown_left_s=max(0.0, state.cooldown_left_s - dt_s))
    by = {c.lane: c for c in report.lanes}
    if epoch != s.epoch:
        # 换纪 = 路心锚点重基：跨帧同一道号的几何含义失效。作废并降级到实测
        # 所在道（夹进候选域），不猜旧目标道还在不在。纪元只在**确认采信的
        # 超容差跳变**上递增（供数门负责计数），坏帧走上面的 needs_init
        k = _clamp_lane(report.ego_lane, by)
        st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                     phase=PHASE_KEEP, decided_t=clock, unsafe_ticks=0,
                     cooldown_left_s=0.0, epoch=epoch, merge_started_t=clock)
        return st, RouteVerdict(PHASE_KEEP, k, None, "epoch_reset",
                                "anchor_rebase", p.commit_s, None, None,
                                _avail_s(by[k], p), 0.0, None, epoch)
    cur = by.get(s.lane_ref)
    if cur is None:
        # 参考道落在候选集外（弯道把自车甩出 ±max_offset）：命令位已不属于
        # 当前道路几何，按实测所在道重建——路线钉在一个没读数的道上时，
        # 承诺期与紧急判据都失去输入
        k = _clamp_lane(report.ego_lane, by)
        st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                     phase=PHASE_KEEP, decided_t=clock, unsafe_ticks=0,
                     merge_started_t=clock)
        return st, RouteVerdict(PHASE_KEEP, k, None, "rebuild",
                                "lane_out_of_range", p.commit_s, None, None,
                                _avail_s(by[k], p), st.cooldown_left_s, None,
                                epoch)
    t_cur = _avail_s(cur, p)
    commit_left = max(0.0, p.commit_s - (clock - s.decided_t))

    if s.phase == PHASE_MERGING:
        return _tick_merging(s, by, commit_left, p, clock, epoch)
    return _tick_keep(s, by, cur, t_cur, commit_left, p, clock, epoch)


def _tick_merging(s: RouteState, by: dict, commit_left: float,
                  p: RouteParams, clock: float,
                  epoch: int | None) -> tuple[RouteState, RouteVerdict]:
    """并线中：只认两件事——到了没有（位置 + 速率）、该不该撤（目标道连续
    危险证据）。承诺期内其他候选一律不起意：中途改主意就是现在的逐拍翻面。"""
    tgt = by.get(s.lane_target)
    t_tgt = _avail_s(tgt, p)
    unsafe = s.unsafe_ticks + 1 if _known_blocked(tgt) else 0
    if unsafe >= p.cancel_unsafe_ticks:
        back = _clamp_lane(s.lane_from, by)
        st = replace(s, lane_ref=back, lane_target=None, lane_from=back,
                     phase=PHASE_KEEP, decided_t=clock, unsafe_ticks=0,
                     merge_started_t=clock, cooldown_left_s=p.cancel_cooldown_s)
        return st, RouteVerdict(PHASE_KEEP, back, None, "cancel",
                               "target_blocked", commit_left, None, None,
                               t_tgt, p.cancel_cooldown_s,
                               None if tgt is None else tgt.free_m, epoch)
    arrived = (abs(s.v_ref - s.lane_target) <= p.finish_tol_lane
               and abs(s.v_est_lane_s) <= p.finish_v_tol_lane_s)
    if arrived or clock - s.merge_started_t >= p.merge_max_s:
        # 超时落回**实测所在道**：车可能停在半道，硬判"完成"会把命令位钉在
        # 车根本没到的道上
        k = float(s.lane_target) if arrived else _clamp_lane(s.v_ref, by)
        st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                     phase=PHASE_KEEP, decided_t=clock, unsafe_ticks=unsafe,
                     merge_started_t=clock)
        return st, RouteVerdict(PHASE_KEEP, k, None, "finish",
                                "arrived" if arrived else "merge_timeout",
                                commit_left, None, None, t_tgt,
                                s.cooldown_left_s, None, epoch)
    return replace(s, unsafe_ticks=unsafe), RouteVerdict(
        PHASE_MERGING, s.lane_ref, s.lane_target, "keep", "merging",
        commit_left, None, None, t_tgt, s.cooldown_left_s, None, epoch)


def _tick_keep(s: RouteState, by: dict, cur, t_cur, commit_left: float,
               p: RouteParams, clock: float,
               epoch: int | None) -> tuple[RouteState, RouteVerdict]:
    alts = [c for c in by.values() if c.lane != s.lane_ref and _qualified(c)]
    # 左右读数完全对称是常态（同一套绿区/墙），同分必须有固定次序：统一取
    # **靠右（+）侧**，否则逐拍翻面会从这里再进来一次

    # ① 紧急逃生：现任道被**确认**占用/收窄且剩余可用时间已低于阈值——突破
    # 承诺期与收益门槛。阈值按观察窗口径给（0.5 s 量级的窗 + 代理速度：车挤进
    # 15~16 m 内才算迫在眼前）；再远一段距离的占用走下面的普通路径主动让，
    # 不是"见车就跑"，也不是"等撞上去才跑"。
    if _known_blocked(cur) and t_cur is not None and t_cur <= p.t_urgent_s:
        # 逃生方向先要"进得去"：按最快可能用时排除明显不可能的侧，再按
        # 完全空闲优先 → 离自车最近 → 时间多 → 道号次序，保证确定性
        feas = [(0 if _clear(c) else 1, abs(c.lane - s.v_ref),
                 -(_avail_s(c, p) or 0.0), -c.lane, c)
                for c in alts
                if (_avail_s(c, p) or 0.0) >
                _merge_times(abs(c.lane - s.lane_ref), p)[0]]
        if not feas:
            return s, RouteVerdict(PHASE_KEEP, s.lane_ref, s.lane_target,
                                   "urgent_no_alt", "no_alt", commit_left,
                                   None, None, t_cur, s.cooldown_left_s,
                                   cur.free_m, epoch)
        alt = min(feas)[4]
        return _start_merge(s, alt, clock), RouteVerdict(
            PHASE_MERGING, alt.lane, alt.lane, "merge_start", "urgent",
            commit_left, None, None, t_cur, s.cooldown_left_s, cur.free_m,
            epoch)

    # ② 普通换道：承诺期 → 冷却 → 收益 → 整段并线检查，任一不过即保持
    if commit_left > 0.0:
        return s, _hold(s, "commit_pending", commit_left, None, None, None,
                        s.cooldown_left_s, epoch)
    if s.cooldown_left_s > 0.0:
        return s, _hold(s, "cooldown", commit_left, None, None, None,
                        s.cooldown_left_s, epoch)
    if not alts:
        return s, _hold(s, "no_clear_alt", commit_left, None, None, None,
                        s.cooldown_left_s, epoch)
    best = max(alts, key=lambda c: (c.free_m, -abs(c.lane - s.lane_ref),
                                    c.lane))
    gain = best.free_m - (cur.free_m if cur is not None else best.free_m)
    if gain <= p.switch_gain_m:
        return s, _hold(s, "no_gain", commit_left, gain, None, t_cur,
                        s.cooldown_left_s, epoch)
    t_lower, t_grant = _merge_times(abs(best.lane - s.lane_ref), p)
    t_tgt = _avail_s(best, p)
    # 整段并线检查的**可用时间取目标道**的最早确认占用时刻：现任道上的占用是
    # 起意理由、不是否决项。若两边取同一个 min，任何够格的起意理由（净空差超
    # 门槛=现任道窗内确实有东西）都会同时把放行判死——观察窗只有 0.5 s 量级，
    # 保守完成时间却是 3 s/道，普通换道将结构上永不成立。那是判据打错层。
    # 目标道"无法确认"的已在资格判里出局（没有判据就不裁决），这里只剩
    # 确认空闲（INF）与有车（按米/代理速度）两种。
    if t_tgt < t_lower:
        return s, _hold(s, "no_time", commit_left, gain, t_lower, t_tgt,
                        s.cooldown_left_s, epoch)
    if t_tgt < t_grant:
        return s, _hold(s, "window_short", commit_left, gain, t_grant, t_tgt,
                        s.cooldown_left_s, epoch)
    return _start_merge(s, best, clock), RouteVerdict(
        PHASE_MERGING, best.lane, best.lane, "merge_start", "gain",
        commit_left, gain, t_grant, t_tgt, s.cooldown_left_s, None, epoch)


def _start_merge(s: RouteState, alt: CoarseLane,
                 clock: float) -> RouteState:
    return replace(s, lane_ref=alt.lane, lane_from=s.lane_ref,
                   lane_target=alt.lane, phase=PHASE_MERGING,
                   decided_t=clock, unsafe_ticks=0,
                   merge_started_t=clock, cooldown_left_s=s.cooldown_left_s)


def _hold(s: RouteState, why: str, commit_left: float, gain, t_need, t_avail,
          cooldown: float, epoch: int | None) -> RouteVerdict:
    return RouteVerdict(PHASE_KEEP, s.lane_ref, s.lane_target, "keep", why,
                        commit_left, gain, t_need, t_avail, cooldown, None,
                        epoch)


def _needs_init(state: RouteState | None,
                epoch: int | None) -> RouteVerdict:
    """当拍无车道级证据或无路心锚点：如实报待重新初始化，不臆造参考道。"""
    ref = None if state is None else state.lane_ref
    tgt = None if state is None else state.lane_target
    ph = PHASE_KEEP if state is None else state.phase
    return RouteVerdict(ph, ref, tgt, "needs_init", "no_evidence", 0.0,
                        None, None, None, 0.0, None, epoch)
