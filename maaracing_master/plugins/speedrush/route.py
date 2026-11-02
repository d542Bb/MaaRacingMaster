# -*- coding: utf-8 -*-
"""路线承诺层（2026-10-23 V1）：横向意图的跨帧持有者。

**它是什么**：跨帧持有「参考道 / 并线目标道 / 阶段 / 定案时刻」，每拍只判三件事
——该不该动、动了之后何时算完成、什么时候该撤。接控制的车道意图从这里读，
不再逐帧重选。

**定案规则**（一条主动路径 + 撤销）：

1. **逃生换道**：现任道被**确认**占用/收窄（连续确认，单帧闪检不起意）→
   换到**确实更宽**的一条：完全空闲的道直接合格；有车的道要多出一个换道门槛
   （`switch_gain_m`）的净空。没有合格去处就原地不动（`no_alt`，证据留 trace）。
2. **机会换道（请求制，2026-10-27）**：选择层出本道区间的目标不再自己拽车
   跨线，改发 `request`（目标道）。本层是**穿越车道线的唯一入口**：现任道
   确认空闲、请求=相邻一道、目标道合格且横穿路径空闲，**并且同侧请求连续
   计分到 `opportunity_confirm_s`**（按实测 `dt` 累计、断拍同速扣减、换侧重起计）才起意
   并线；拒了就维持 keep——目标早已被选择层贴回本道可活动带，永远不会停在两条
   道中间。计分门槛裁决的是「机会真的在那条道上」，不是「这一拍看见了东西」：
   实机一局原始请求 61 段里 40 段只有 1~2 拍（币组检测闪断）。
   `route.request_hold` 开时请求变成状态（6c）：无读数在 `request_gap_s` 内
   冻结证据、不得加分；起意只认当拍有效来源（`request_live=True`），
   证据够了但来源不可达报 `no_reach`。
3. **并线中只认两件事**：到了没有（位置进目标道中心带 **且** 横速落稳）、
   该不该撤（目标道连续确认被截）。并线期间不评估别的候选——中途改主意就是
   逐拍翻面。
4. **撤销后冷却**：刚放弃的那条道在冷却期内不再作为去处（A↔B 来回摆的根）。

**为什么不再拆"普通换道"这一路**（20261024 两局实测定案）：粗层观察窗只有
24 m（按 `range_speed_mps` 折算约 0.53 s），而"净空差超门槛"的前提是现任道
窗内确实有东西——于是任何够格的主动换道同时也是逃生换道，两路在这套窗径下
永远重叠。拆成两套门槛只会造出"承诺期内不能动"的空转：实机一局 327 拍卡在
承诺期，其中没有一次真正的决定（38/50 次并线还被换纪打断）。宽松窗径（远带
/前瞻）进来之后，主动选道按同一套"更宽就动"的规则自然复活。

**它不是什么**：不算轨迹、不判精确碰撞、不重算中心线。占用与净空全部吃上游
粗层的**原始候选报告**——那是同一份净空真源，本层不建第二份，也不回读已经
接控制的道号（承诺值喂回候选评估会让迟滞语义在两层之间漂移）。

占用三态（互不等价，`无法确认` 永远不等于 `确认空闲`）：
- `确认空闲`：窗内无占用证据 → 换过去直接合格；
- `被占`：确认有车，或走廊收窄到走不下（`space`）；现任道上是起意理由，
  替代道上按净空差裁决；
- `无法确认`：有车但缺纵深/到站时间 → 不能作替代道（没有判据就不裁决），
  也不单独构成逃生理由（逃生要现任道的正面证据）。

坐标与时间：道号沿**上游粗层帧**（相对场景 z=0 的分带中心线）持有——与两个
控制消费者今天读的原点逐位一致，本层不做第二次换算。时钟用 tick 累计秒
（`time.monotonic` 不进本模块：回放与实机同一确定性）。米→秒用粗层既有的
折算常数代理，不新造自车速度真源——本系统没有纵向速度观测。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.coarse import CoarseLane, CoarseReport

INF = math.inf

PHASE_KEEP = "keep"
PHASE_MERGING = "merging"

# 请求道的合法跨度（道）：选择层只出相邻 ±2，被拒边界 2.5——重锚换基准后
# 目标超出这个跨度也发不出请求，保留证据没有意义（与 `_request_reject_reason`
# 同一把尺）。
REQ_SPAN_LANE = 2.5


def _reached(value: float, limit: float) -> bool:
    """按秒累计与门槛的比较：吸收浮点累加误差（0.05×6 不逐位等于 0.3）。"""
    return value + 1e-9 >= limit


# 截断来源分类（沿用粗层 CoarseLane.blocker 的语义，不另立枚举真源）：
# None=确认空闲；"space"/"car"=确认被占；"unknown_car"=无法确认。
KNOWN_BLOCKERS = ("space", "car")
# 替代道资格：几何走不下（space）与无法确认（unknown_car）都不合格——后者
# 不是"更保守"，是"没有判据"。
UNQUALIFIED = ("space", "unknown_car")

# 到达位置归因（2026-10-28 C 门契约）：只描述「完成位置来自哪一份读数」，
# 不评价读数本身是否可用。换档标签属于相邻两次新鲜读数之间的变化，挂在
# **较新那份读数**上；保持值继承它重复的那份读数的标签，下一份新鲜读数到达
# 即清掉旧标签。
ATTR_NORMAL = "NORMAL"          # 新鲜读数 / hop=0|2 读数的保持值
ATTR_SHIFT_HELD = "SHIFT_HELD"  # hop=1 的新鲜读数，或它的保持值
ATTR_OTHER = "OTHER"            # 已知不属于上两类（保持值已经变了）
ATTR_UNKNOWN = "UNKNOWN"        # 证据不足（首帧/缺读数/hop 缺失）


@dataclass(frozen=True)
class ArrivalAttribution:
    """一拍的位置归因（观测面；只有 SHIFT_HELD 会被到达门拦）。"""

    kind: str
    reason: str | None = None
    last_fresh_ro: float | None = None
    last_fresh_hop: int | None = None
    last_fresh_fid: int | None = None
    d_ro: float | None = None

    def as_trace(self) -> dict:
        return {"k": self.kind, "why": self.reason,
                "fid": self.last_fresh_fid, "hop": self.last_fresh_hop,
                "d": None if self.d_ro is None else round(self.d_ro, 4)}


class PositionLineage:
    """路心读数序列上的归因器；状态只有「上一份新鲜读数」。

    实现口径（与 `C_GATE_DESIGN.md` §8 一致）：`fresh` 由调用方给（新鲜几何
    证据且非保持），`hop` 是该新鲜读数**到达时**的换档标签；保持拍传
    `fresh=False`，`hop` 忽略。容差只用于判断「保持值是否等于上一份新鲜
    读数」；超出即 OTHER，不做近似匹配。
    """

    def __init__(self, tol: float = 1e-3):
        self.tol = tol
        self.last: tuple[float, int | None, int | None] | None = None

    def step(self, *, fresh: bool | None, hop: int | None,
             ro: float | None, fid: int | None) -> ArrivalAttribution:
        if fresh is None:
            return ArrivalAttribution(ATTR_UNKNOWN, "freshness_unknown",
                                      *(self._last_fields()))
        if ro is None:
            return ArrivalAttribution(ATTR_UNKNOWN, "no_reading",
                                      *(self._last_fields()))
        if fresh:
            prev = self.last
            if prev is None:
                kind, why = ATTR_UNKNOWN, "first_fresh"
                d = None
            elif hop is None:
                kind, why = ATTR_UNKNOWN, "no_hop"
                d = ro - prev[0]
            elif hop == 1:
                kind, why = ATTR_SHIFT_HELD, None
                d = ro - prev[0]
            elif hop in (0, 2):
                kind, why = ATTR_NORMAL, None
                d = ro - prev[0]
            else:
                kind, why = ATTR_UNKNOWN, "bad_hop"
                d = ro - prev[0]
            self.last = (float(ro), hop, fid)
            return ArrivalAttribution(kind, why, float(ro), hop, fid, d)
        # 保持拍：位置项只有等于上一份新鲜读数时才归因给它
        prev = self.last
        if prev is None:
            return ArrivalAttribution(ATTR_UNKNOWN, "no_fresh")
        lro, lhop, lfid = prev
        d = float(ro) - lro
        if abs(d) > self.tol:
            return ArrivalAttribution(ATTR_OTHER, "held_changed",
                                      lro, lhop, lfid, d)
        if lhop == 1:
            return ArrivalAttribution(ATTR_SHIFT_HELD, None,
                                      lro, lhop, lfid, d)
        if lhop in (0, 2):
            return ArrivalAttribution(ATTR_NORMAL, None,
                                      lro, lhop, lfid, d)
        return ArrivalAttribution(ATTR_UNKNOWN, "last_hop_missing",
                                  lro, lhop, lfid, d)

    def _last_fields(self):
        if self.last is None:
            return (None, None, None, None)
        return (self.last[0], self.last[1], self.last[2], None)



@dataclass(frozen=True)
class RouteParams:
    """路线层参数（真源 decision.json [route]；尺度/速度类复用既有真源）。"""

    switch_gain_m: float = 4.0       # 逃生要求"多出一个换道门槛"的净空（与粗层
    # 换道迟滞同源一个数：差值不到一个门槛就不值得为它动方向）
    proxy_speed_mps: float = 45.0    # 米→秒折算代理（复用 [coarse] 真源，仅 trace）
    urgent_confirm_s: float = 0.15   # 现任道占用要连续这么久才起意（防闪检）
    opportunity_confirm_s: float = 0.25  # 机会换道请求的起意计分门槛（同侧按
    # 实测 dt 累计、断拍同速扣减，泄漏式）。165841 局原始请求 61 段里 26 段
    # 只有 1 拍、14 段只有 2 拍（币组检测闪断）；不设这道门一局要并线 60 次
    # （人工 7~9 次）。0.25 s=5 拍仍远高于 1~2 拍闪检，而 2026-11-01 实车两场
    # 的请求意图证据顶部就在 0.25/0.29 s（同侧有效拍 5 个左右）——0.30 s 的
    # 6 拍把它们整段挡在门外。按秒而非拍数：实际节拍 17.7~17.85 Hz 时同样的
    # 6 拍会变成 0.34 s，窗口随负载漂移。
    cancel_unsafe_s: float = 0.25    # 撤销判定的连续时长（防单帧野值弃窗）
    # 6c 请求证据/动作两位：证据按时长累计、丢检冻结，起意只认当拍可达来源。
    request_hold: bool = False
    request_gap_s: float = 0.2       # 无读数冻结上限（秒）
    request_intent_age_s: float = 1.0  # 同一意图的最长寿命（秒）
    cancel_cooldown_s: float = 2.0   # 撤销后冷却：期内不回刚放弃的那条道
    finish_tol_lane: float = 0.3     # 完成的位置容差（同 L4 中心带宽）
    finish_v_tol_lane_s: float = 0.4  # 完成的横速容差：带着横速"完成"正是过冲
    merge_max_s: float = 4.0         # 并线超时（人工 p90 量级）：到点按实测
    # 所在道落回 keep——卡在并线态会让路线永不重启
    soft_car: bool = False           # 普通车（car）不构成撤销理由：车是时间成本，
    # 不是物理约束；目标道 space 仍按参考道失去几何重建。False=旧行为
    # —— 紧急逃生的时间面（2026-10-30）——
    # 粗层净空是「24 m 窗内有无车」，不是「多久会撞上」。按代理速度折算，
    # 窗内任何车都 ≤0.53s＝永远紧急，路线于是见车就并（90s 局并线 21 次，
    # 人工同局 26 次但每次都在真冲突上）。这两个参数用**实测到站时间**
    # （相对速度口径）替代代理时间：车真的要到线上了才允许动方向。
    urgent_ttc_s: float = 1.0        # 当前道（承诺道与实际所在道）最近到站时间
    # ≤ 此值才起紧急逃生；None=该道无车/无时间证据
    urgent_blind_m: float = 6.0      # 无到站时间（不接近或无法测速）但粗层净空
    # ≤ 此值的近距离盲区仍可逃生
    merge_safe_ttc_s: float = 0.8    # 逃生目标道上有车时，到站时间必须 ≥ 此值
    # （够并过去）；更近的车不选它当逃生方向
    pass_hold: bool = True           # 超车动作进行中（选择层正在出擦边目标）
    # 不抢方向：临近车会让紧急逃生在 1s 内起意，把刚要成的擦边超车改成并线
    # （2026-10-30 实测：31 段≥5 拍的擦边里有 6 段被 3 拍内的并线打断）
    overtake: bool = True            # 提前超车并线：车 1~2.5s 到站、邻道确认
    # 空闲、横穿路径空闲 → 现在就并过去。等 1s 紧急门才动，等于把超车拖成
    # 临门闪避（真机常见：跟到 0.9s 才起并线）
    overtake_ttc_s: float = 2.5      # 提前超车的到站时间上界（秒）；下界是
    # urgent_ttc_s（更近的归紧急逃生）
    # 承诺道跟随实测道（6g 归因）：粗层把车所在道短暂判成 space 时路线会重建
    # 到邻道，之后没有机制把承诺道挪回来——车在 −1 道、承诺道写 0，选择层就把
    # 邻道币判成本道目标，机会请求永远发不出来。实测道可行且偏离承诺道
    # ≥anchor_follow_lane 持续 anchor_follow_s，就静默重锚（不是并线、不进
    # 冷却）；并线中不做（车在两道之间是正常过渡）。默认关：关时逐位回旧行为。
    anchor_follow: bool = False
    anchor_follow_s: float = 0.3     # 偏离持续时长（秒）
    anchor_follow_lane: float = 0.6  # 偏离阈值（道）：超过半道且留 0.1 道防抖


@dataclass(frozen=True)
class RouteState:
    """一拍接一拍的路线承诺状态（frozen；推进返回新值，便于单测与回放）。"""

    lane_ref: float                  # 命令认为车应该在的道（相对粗层原点）
    lane_from: float                 # 本次并线的出发道（撤销时回接）
    lane_target: float | None        # 并线目标道；None=无并线
    phase: str                       # keep / merging
    decided_t: float                 # 上次定案时刻（tick 累计秒）
    elapsed_s: float                 # 时钟本身（冷却按时间走，不靠拍数）
    cooldown_left_s: float           # 撤销冷却剩余
    ban_lane: float | None           # 冷却期内禁入的道（刚放弃的那条）
    unsafe_s: float                  # 目标道危险证据的连续时长（秒）
    blocked_s: float                 # 现任道被截的连续时长（秒，逃生确认）
    epoch: int                       # 定案时的参考系纪元；与当拍不等 = 换纪作废
    v_ref: float                     # 上一拍实测横位（道，粗层帧）
    merge_started_t: float           # 本次并线起意时刻（超时判据用）
    v_est_lane_s: float              # 实测横位差分（道/s）：完成判据的速率面
    req_lane: float | None = None    # 当拍机会换道请求目标道（选择层出界
    # 目标转来的请求；逐拍刷新、不跨帧持有——机会没了请求自然消失）
    req_credit: float = 0.0          # 同侧请求的累计秒数（门槛见 params）
    req_oid: int | None = None       # 触发身份（诊断/连续性，不参与计分）
    req_side: int = 0                # 请求方向（+1/−1）
    req_age_s: float = 0.0           # 同一意图的年龄（秒）
    req_pause_s: float = 0.0         # 无读数累计（秒）
    req_state: str = "none"          # active/paused/none（6c）
    drift_s: float = 0.0             # 承诺道与实测道偏离的连续时长（秒）


@dataclass(frozen=True)
class RouteVerdict:
    """一拍定案结果（trace 观测面：离线要能复算为什么放行/为什么被拒）。"""

    phase: str
    lane_ref: float | None
    lane_target: float | None
    verdict: str                     # init/keep/merge_start/finish/timeout/
    # cancel/urgent_no_alt/epoch_reset/rebuild/needs_init/anchor_follow
    why: str                         # 保持或被拒的第一原因码（keep 态口径见
    # `_request_reject_reason`：无请求 no_request/blocked_unconfirmed/not_urgent，
    # 未到门槛 paused/scoring，被拒 current_unconfirmed/same_lane/not_adjacent/
    # target_missing/target_unqualified/cooldown_ban/path_blocked；证据够但
    # 当拍无有效来源报 no_reach（6c）；请求目标已是车所在道报 already_there
    since_s: float                   # 距上次定案的时间（"多久动一次"的观测面）
    gain_m: float | None             # 当前道与被选替代道的净空差（米）
    t_cur_s: float | None            # 现任道最早占用时刻（代理速度，秒）
    evidence_m: float | None         # 危险证据距离（米；逃生分支必带）
    cooldown_left_s: float
    epoch: int
    # 目标/参考道在结束拍的粗层 blocker：car/space/unknown_car/None。
    # 阶段 0 观测面：区分“车因撤销”与“空间作废”，不参与控制。
    blocker: str | None = None
    credit: float | None = None      # 机会请求的当前计分（保道态观测面：
    # 「为什么还没并线」= 计分未到门槛）
    blocked_finish: bool = False     # 到达门（C）当拍是否拦下了一个本会成立的
    # finish；只作观测/影子，门关时恒 False，不参与其他分支
    req_state: str = "none"          # 6c 请求三态
    req_oid: int | None = None
    req_age_s: float = 0.0
    req_pause_s: float = 0.0
    req_hold: bool = False           # 6c 开关状态（trace 验收：确认本局开了）
    anchor_follow: bool = False      # 6h 开关状态（trace 验收：确认本局开了）

    def as_trace(self) -> dict:
        def _r(x, nd):
            return None if x is None or not math.isfinite(x) else round(x, nd)
        return {"ph": self.phase, "ref": _r(self.lane_ref, 3),
                "tgt": _r(self.lane_target, 3), "vd": self.verdict,
                "why": self.why, "age": _r(self.since_s, 2),
                "gain": _r(self.gain_m, 2), "tc": _r(self.t_cur_s, 3),
                "ev": _r(self.evidence_m, 2), "cd": _r(self.cooldown_left_s, 2),
                "ep": self.epoch, "blk": self.blocker,
                "crc": _r(self.credit, 2), "blkfin": self.blocked_finish,
                # 6c 请求状态观测面
                "reqs": self.req_state, "roid": self.req_oid,
                "rage": _r(self.req_age_s, 2),
                "rpa": _r(self.req_pause_s, 2), "rh": self.req_hold,
                "af": self.anchor_follow}


def init_state(epoch: int) -> RouteState:
    """冷启动参考道=路心道（0）。

    没有承诺时不动横向：0 道就是选择帧中心线本身，与粗层不接控制时的 L4
    回中逐位同形——V1 的行为差异只该来自"道号被承诺门控"，不该来自启动瞬间
    凭空挑一条最近的道压过去。作废/换纪的降级另走 `_rebuild`（那里可能有
    未完成的动作要保护，落回实测可达道）。"""
    return RouteState(0.0, 0.0, None, PHASE_KEEP, 0.0, 0.0, 0.0, None, 0, 0,
                      epoch, 0.0, 0.0, 0.0)


def _clear_req(s: RouteState) -> RouteState:
    return replace(s, req_lane=None, req_credit=0.0, req_oid=None,
                   req_side=0, req_age_s=0.0, req_pause_s=0.0,
                   req_state="none")


def _carry_req_across_reanchor(s: RouteState) -> RouteState:
    """重锚换基准（anchor_follow）：请求证据换基准保留，不清零。

    anchor_follow 把参考道挪到车所在道，不改变道的几何语义——攒到的证据
    属于「机会真的在那条道上」这个判断，跟着绝对道号走，不跟着参考道走
    （台架坐实：重锚清掉的多是攒满在即的证据，换基准后同一目标仍落在
    ±2 请求跨度内，保留就能接着攒）。目标道超出新参考系的请求跨度
    （绝对差 >2.5 道，±2 也够不着）才清。证据位保留、动作位降级：req_state
    置 paused，起意仍须下一拍有活请求——保留的是证据，不是当拍可达性。"""
    if s.req_lane is None or s.req_credit <= 0.0:
        return _clear_req(s)
    off = s.req_lane - s.lane_ref
    if abs(off) > REQ_SPAN_LANE:
        return _clear_req(s)
    return replace(s, req_side=1 if off > 0 else -1,
                   req_state="paused", req_pause_s=0.0)


def _advance_request(s: RouteState, request: float | None, live: bool,
                     oid: int | None, p: RouteParams,
                     step: float) -> RouteState:
    """推进请求证据一拍（6c）。

    `live=True`：本拍触发源有新鲜读数且可达（选择层已按时间锥筛过）。
    关 `request_hold` 时逐位复现旧的泄漏计分；打开后，无读数拍在
    `request_gap_s` 内冻结、不累计；证据/动作都在这里分叉：冻结只能保住
    已攒的证据，起意门另认 `req_state == "active"`。
    """
    if s.phase != PHASE_KEEP:
        return _clear_req(s)
    if not p.request_hold:
        # 旧行为：同侧 +dt、断拍 −dt、换侧重置；不做暂停/意图龄
        if request is None:
            credit = max(0.0, s.req_credit - step)
            keep = credit > 0.0
            return replace(
                s, req_lane=s.req_lane if keep else None, req_credit=credit,
                req_oid=s.req_oid if keep else None,
                req_side=s.req_side if keep else 0,
                req_age_s=s.req_age_s + step if keep else 0.0,
                req_pause_s=0.0,
                req_state="leak" if keep else "none")
        req_abs = s.lane_ref + float(request)
        side = 1 if request > 0 else -1
        same = req_abs == s.req_lane and side == s.req_side
        return replace(
            s, req_lane=req_abs,
            req_credit=s.req_credit + step if same else step,
            req_oid=s.req_oid if same and s.req_oid is not None else oid,
            req_side=side,
            req_age_s=s.req_age_s + step if same else step,
            req_pause_s=0.0, req_state="active")
    # request_hold：丢检冻结、不可达清零、意图超龄清零
    if request is None:
        if s.req_lane is None or s.req_credit <= 0.0:
            return _clear_req(s)
        pause = s.req_pause_s + step
        age = s.req_age_s + step
        if pause > p.request_gap_s + 1e-9 \
                or age > p.request_intent_age_s + 1e-9:
            return _clear_req(s)
        return replace(s,
                       req_credit=min(s.req_credit,
                                      p.opportunity_confirm_s),
                       req_pause_s=pause, req_age_s=age,
                       req_state="paused")
    req_abs = s.lane_ref + float(request)
    side = 1 if request > 0 else -1
    same = req_abs == s.req_lane and side == s.req_side
    if not same:                       # 换道/换向：新的意图
        pause = 0.0 if live else step
        if not live and pause > p.request_gap_s + 1e-9:
            return _clear_req(s)
        return replace(s, req_lane=req_abs,
                       req_credit=step if live else 0.0,
                       req_oid=oid, req_side=side,
                       req_age_s=step, req_pause_s=pause,
                       req_state="active" if live else "paused")
    if live:
        credit, pause, state = s.req_credit + step, 0.0, "active"
    else:
        pause = s.req_pause_s + step
        if pause > p.request_gap_s + 1e-9:
            return _clear_req(s)
        credit = min(s.req_credit, p.opportunity_confirm_s)
        state = "paused"
    age = s.req_age_s + step
    if age > p.request_intent_age_s + 1e-9:
        return _clear_req(s)
    return replace(s, req_lane=req_abs, req_credit=credit,
                   req_oid=oid if oid is not None else s.req_oid,
                   req_side=side, req_age_s=age, req_pause_s=pause,
                   req_state=state)


def _avail_s(c: CoarseLane | None, p: RouteParams) -> float | None:
    """该道最早占用时刻（秒，代理速度折算；trace/观测面）。None=无法确认
    （有证据没时刻）；INF=窗内没有占用证据（确认空闲——按观察窗口径诚实）。"""
    if c is None or c.blocker == "unknown_car":
        return None
    if c.blocker is None:
        return INF
    return max(0.0, c.free_m) / p.proxy_speed_mps


def _known_blocked(c: CoarseLane | None) -> bool:
    return c is not None and c.blocker in KNOWN_BLOCKERS


def _cancel_blocked(c: CoarseLane | None, p: RouteParams) -> bool:
    """并线撤销的占用面。soft_car 时普通车（car）不算：车是时间成本，可由计划层
    按 0.35 s 迎车门当场否决，不需要路线整段撤销。`space` 不进这里——目标道走
    不下时参考道失去几何，已在 `tick_route` 的 rebuild 分支处理。"""
    if p.soft_car:
        return False
    return c is not None and c.blocker in KNOWN_BLOCKERS


def _qualified(c: CoarseLane | None) -> bool:
    return c is not None and c.blocker not in UNQUALIFIED


def _clear(c: CoarseLane | None) -> bool:
    return c is not None and c.blocker is None


def _already_in(ego_lane: float, lane: float) -> bool:
    """车实测已在这条道（0.3 道以内）：再为它起一次并线是空转。"""
    return abs(ego_lane - lane) <= 0.3


def _has_band(c: CoarseLane | None) -> bool:
    """这条道有没有可用的绿区带（宽度余量）。

    `space` 有两种：真车道被走廊判成暂时走不下（有带宽数字，重新出现后会
    恢复）；以及外侧 3 m 肩道（无有效带，`width_margin_m=None`）。后者不能
    当承诺道跟随——它的中心在墙外，目标会朝外打（185205 局 1068 拍）。"""
    return c is not None and (c.blocker != "space"
                              or c.width_margin_m is not None)


def _drifted_free(s: RouteState, by: dict, p: RouteParams) -> bool:
    """车实际所在道与承诺道不同、且偏离达到重锚阈值时，先重锚再起逃生。

    重建到邻道后原道马上又可行时，承诺道会粘在邻道（173954：keep 态 47.8%
    的拍错位 >0.5 道）。这种拍不按陈旧承诺道的占用起逃生/超车——等
    anchor_follow 把参考道挪到车所在道。机会请求不在这里拦：它的目标相对
    承诺道，证据攒满就按它起意（见 `_tick_keep`）。偏离阈值与重锚一致
    （`anchor_follow_lane`）：0.5~0.6 道的边界带里重锚不会触发，若这里也
    拦，请求计分涨到门槛也永远起不了意。"""
    k = float(round(s.v_ref))
    return (k != s.lane_ref
            and abs(s.v_ref - s.lane_ref) >= p.anchor_follow_lane
            and _clear(by.get(k)))


def _path_clear(s: RouteState, alt: CoarseLane, by: dict) -> bool:
    """并入 alt 要横穿的中间道必须**确认空闲**。跨一条被占的道并线＝
    从占用车上碾过去：20261027_160949_1 s2 24.3s 现任道与右道都被 0 m 车
    占着，urgent 并到 −1，中间道上正贴着一辆 z 丢失的车——d_min 0.004。

    起点用**实测横位**不是承诺道：车被钉在 0.67 道（属于 +1 道）而承诺道
    写 0 时，按承诺道算出的穿越集合会少掉真正跨过的那一条——20261030_122416
    seq 1422 正是从 0.67 道并向 −1，按 ref=0 算中间道为空，实际碾过 0 道上
    7.4m 车的车道。"""
    lo, hi = sorted((int(round(s.v_ref)), int(alt.lane)))
    return all(_clear(by.get(float(k))) for k in range(lo + 1, hi))


def _lane_ttc_at(lane_ttc: dict[int, float] | None,
                 lane: float) -> float | None:
    if lane_ttc is None:
        return None
    return lane_ttc.get(int(round(lane)))


def _cur_ttc(s: RouteState, lane_ttc: dict[int, float] | None) -> float | None:
    """当前道最早到站时间：承诺道与实测所在道取更近者（车横跨两道时，
    只看承诺道会漏掉正压在身上的那条）。"""
    if lane_ttc is None:
        return None
    ttcs = [t for t in (_lane_ttc_at(lane_ttc, s.lane_ref),
                        _lane_ttc_at(lane_ttc, s.v_ref)) if t is not None]
    return min(ttcs) if ttcs else None


def _urgent_time_ok(s: RouteState, cur: CoarseLane, p: RouteParams,
                    lane_ttc: dict[int, float] | None) -> bool:
    """紧急逃生的时间门。

    ``lane_ttc``=整数道→最早到站时间（秒）。无时间证据的无车/不接近道保持
    旧行为（由净空盲区兜底）；整张表缺席（离线/单测未供）也保持旧行为，
    生产装配必供。"""
    if lane_ttc is None:
        return True
    t = _cur_ttc(s, lane_ttc)
    if t is not None:
        return t <= p.urgent_ttc_s
    return cur.free_m <= p.urgent_blind_m


def _target_time_ok(alt: CoarseLane, p: RouteParams,
                    lane_ttc: dict[int, float] | None) -> bool:
    """逃生目标道的时间门：那里有车但要 ≥ merge_safe_ttc_s 才够并过去。"""
    if lane_ttc is None:
        return True
    t = _lane_ttc_at(lane_ttc, alt.lane)
    return t is None or t >= p.merge_safe_ttc_s


def _drivable_lane(ego_lane: float, by: dict) -> float:
    """重建落点：离实测横位最近的**可行驶**候选道。

    `space` = 该道已被走廊判为走不下，不能当落点——参考道钉在一条不存在的
    道上时，控制目标会跟着它平移，等于朝墙打舵。可行道全无时退最宽的一条。"""
    drivable = [c for c in by.values() if c.blocker != "space"]
    if not drivable:
        return max(by.values(), key=lambda c: (c.free_m, c.lane)).lane
    k = float(round(ego_lane))
    return min(drivable, key=lambda c: (abs(c.lane - k), -c.lane)).lane


def tick_route(state: RouteState | None, report: CoarseReport | None,
               p: RouteParams, *, dt_s: float,
               epoch: int,
               request: float | None = None,
               request_live: bool = True,
               request_action: bool | None = None,
               request_oid: int | None = None,
               arrival: ArrivalAttribution | None = None,
               reject_shift_arrival: bool = False,
               lane_ttc: dict[int, float] | None = None,
               passing: bool = False,
               ) -> tuple[RouteState | None, RouteVerdict]:
    """推进一拍。

    `report`：当拍**原始**粗层报告（逐道净空/占用三态），不是接控制的道号。
    `epoch`：参考系纪元（尺子跳变时递增）。返回 (新状态, 定案报告)；缺证据时
    给 `needs_init`，**不回退旧坐标**（旧道号在新参考系里没有原意）。
    `request`：当拍机会换道请求——**选择层出的是相对承诺道的偏移（±2）**，本层
    是相对→绝对的换算边界，状态里 `req_lane` 存**绝对**目标道（2026-10-27 契约
    冻结）。逐拍刷新，不持久。
    `request_live`：触发源本拍是否有新鲜读数（6c）。False 时只冻结证据、不
    加分、不允许起意；`request_oid` 只作诊断与同道换对象连续性。
    `request_action`：动作位（第 7 期得分源平滑）——触发源本拍**原始** slack
    是否可达。False 时证据仍可累计，但不得起意（`no_reach`，等原始可达拍）；
    None=未平滑（第 6 期，两位同源，不设额外门）。
    `request_hold`（默认关）：开时无读数在 `request_gap_s` 内冻结，超过或意图
    年龄超 `request_intent_age_s` 清零；关时逐位复现旧的泄漏计分。
    **请求计分的生命周期**（与换算同批冻结）：① 只有保持态累计与消费——并线中
    机会层不出目标，留着旧进度等于让上一道的判断跨状态生效；② 任何改 `lane_ref`
    的路径（`_start_merge`/`_rebuild`）随承诺道一起清空请求与计分，换算基准
    （消费拍的 `lane_ref`）因此始终等于生成拍的；③ 缺车道级证据的拍清零，不冻结
    也不漏分——连续性已断，断供前后两段凑不成一次证据。
    `arrival` / `reject_shift_arrival`：到达位置门（C，2026-10-28）。门开且
    归因为 `SHIFT_HELD` 时，本会成立的 finish 不成立；撤销优先、超时语义不变。
    生产默认门关（`reject_shift_arrival=False`）；影子/未来开关才传 True。
    `lane_ttc`：整数道→最早到站时间（秒，相对速度口径）。紧急逃生与目标道
    选择的时间面（2026-10-30）；None=离线/单测未供，逐位保持旧行为。
    `passing`：选择层本拍（上一拍）正在出擦边目标。`pass_hold` 开时路线不抢
    方向，把这 1s 的逃生窗口留给超车动作完成。
    """
    if report is None or not report.lanes or report.reason == "no_road":
        # 没有车道级证据：时钟继续走，但路线不推进、不判逃生——证据缺席
        # 不等于危险解除，也不等于可以随便动
        if state is None:
            return None, replace(_needs_init(state, epoch, 0.0),
                                 anchor_follow=p.anchor_follow)
        st = _clear_req(replace(state,
                                elapsed_s=state.elapsed_s + max(dt_s, 0.0)))
        return st, replace(_needs_init(st, epoch, 0.0),
                           anchor_follow=p.anchor_follow)
    if state is None:
        s = init_state(epoch)
        return s, RouteVerdict(PHASE_KEEP, s.lane_ref, None, "init", "cold",
                               0.0, None, None, None, 0.0, epoch,
                               anchor_follow=p.anchor_follow)

    v_est = ((report.ego_lane - state.v_ref) / dt_s
             if dt_s > 0.0 else state.v_est_lane_s)
    step = max(dt_s, 0.0)
    clock = state.elapsed_s + step
    # 请求计分：同侧连续按秒累计、断拍同速扣减（泄漏到 0 才忘掉那条道），
    # 换侧重置起计。泄漏而不是清零：实机里真实币线的请求拍中位隔 6~7 拍才被
    # 重新看见一次（检测占空比约一半），断拍即清零等于门槛永不达标；反之
    # 1~2 拍的闪检攒不到分。门槛裁决「机会真的在那条道上」，不锁方向盘——
    # 计分期间目标始终在本道区间内。
    # 生命周期门（①）：并线中一律不累计——承诺期的机会请求没有来源（选择层
    # 关闭机会层），攒到的分属于上一道。只拒绝本拍不够：旧进度会在完成拍带着
    # 跨状态的分数提前起意（实测 6 拍门被穿到 4 拍），所以失效状态清零。
    req_s = _advance_request(state, request, request_live, request_oid, p,
                             step)
    s = replace(req_s, elapsed_s=clock, v_ref=report.ego_lane,
                v_est_lane_s=v_est,
                cooldown_left_s=max(0.0, state.cooldown_left_s - dt_s))
    by = {c.lane: c for c in report.lanes}
    if epoch != s.epoch:
        # 换纪 = 参考系重基：跨帧同一道号的几何含义失效。作废并降级到实测
        # 可达道，不猜旧目标道还在不在。重建不是决定，`decided_t` 原样保留
        st, v = _rebuild(s, by, report.ego_lane, clock, epoch, p,
                         "epoch_reset", "anchor_rebase")
        return st, replace(v, anchor_follow=p.anchor_follow)
    cur = by.get(s.lane_ref)
    # 车实测就在这条道上：单帧 space 不能据此重建（走廊带漏检会让车所在的
    # 道短暂“走不下”，路线被反复推开再拉回，173954 局 15 次 rebuild 里大半
    # 是这种）。车本身占着这条道，几何至少存在；宽度/占用交给逃生与计划层。
    keep_space = (p.anchor_follow and _has_band(cur)
                  and abs(report.ego_lane - s.lane_ref) <= 0.5)
    if cur is None or (cur.blocker == "space" and not keep_space):
        # 参考道失去几何：要么掉出候选集（弯道把车甩出 ±max_offset），要么
        # 该道被走廊判成走不下。钉在一条不存在的道上时目标会跟着它平移＝
        # 朝墙打舵，且逃生判据也失去输入——立刻重建到可行道
        st, v = _rebuild(s, by, report.ego_lane, clock, epoch, p, "rebuild",
                         "lane_dropped" if cur is not None
                         else "lane_out_of_range",
                         blocker=None if cur is None else cur.blocker)
        return st, replace(v, anchor_follow=p.anchor_follow)
    # 承诺道跟随实测道（默认关）：实测道与承诺道不同、偏离超过
    # anchor_follow_lane 且持续 anchor_follow_s，就静默重锚。重建到邻道后
    # 这条道可能马上又可行，旧实现没有回程，承诺道会一直粘在错的道上
    # （173954：keep 态 47.8% 的拍错位 >0.5 道）——选择层随之把邻道机会
    # 判成本道目标。落点取车实际所在道（存在即可，不要求走廊确认宽度）：
    # 车已经开在那里，space 只会让跟随被反复清零。
    if p.anchor_follow and s.phase == PHASE_KEEP:
        k = float(round(report.ego_lane))
        if not _has_band(by.get(k)):
            # 肩道（无有效带）不能当承诺道：跟过去目标就朝墙外
            k = _drivable_lane(report.ego_lane, by)
        drift = abs(report.ego_lane - s.lane_ref)
        follow = k != s.lane_ref and drift >= p.anchor_follow_lane
        drift_s = s.drift_s + step if follow else 0.0
        if follow and _reached(drift_s, p.anchor_follow_s):
            st = _carry_req_across_reanchor(
                replace(s, lane_ref=k, lane_from=k,
                        lane_target=None, drift_s=0.0))
            return st, RouteVerdict(
                PHASE_KEEP, k, None, "anchor_follow", "lane_drift",
                max(0.0, clock - st.decided_t), None, _avail_s(by[k], p),
                None, s.cooldown_left_s, epoch,
                blocker=None if by.get(k) is None else by[k].blocker,
                anchor_follow=True)
        s = replace(s, drift_s=drift_s)
    # 现任道被截的连续时长：逃生要"持续占用"，单帧闪检不起意（与撤销判据
    # 同一族的时域滞回；观察窗只有 0.5 s，按实测 dt 累计）
    s = replace(s, blocked_s=(s.blocked_s + step
                              if _known_blocked(cur) else 0.0))
    if s.phase == PHASE_MERGING:
        st, v = _tick_merging(s, by, p, clock, epoch, arrival,
                              reject_shift_arrival, step)
    else:
        st, v = _tick_keep(s, by, cur, _avail_s(cur, p), p, clock, epoch,
                           lane_ttc, passing, request_action)
    return st, replace(v, req_state=st.req_state, req_oid=st.req_oid,
                       req_age_s=st.req_age_s, req_pause_s=st.req_pause_s,
                       req_hold=p.request_hold,
                       anchor_follow=p.anchor_follow)


def _rebuild(s: RouteState, by: dict, ego_lane: float, clock: float,
             epoch: int, p: RouteParams, verdict: str,
             why: str, blocker: str | None = None) -> tuple[RouteState, RouteVerdict]:
    """降级为 KEEP(实测可达道)：并线作废、冷却清零、定案时钟不清。"""
    k = _drivable_lane(ego_lane, by)
    st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                 phase=PHASE_KEEP, unsafe_s=0.0, cooldown_left_s=0.0,
                 ban_lane=None, epoch=epoch, merge_started_t=clock,
                 req_lane=None, req_credit=0.0, req_oid=None, req_side=0,
                 req_age_s=0.0, req_pause_s=0.0, req_state="none")
    return st, RouteVerdict(PHASE_KEEP, k, None, verdict, why,
                            max(0.0, clock - st.decided_t), None,
                            _avail_s(by[k], p), None, 0.0, epoch,
                            blocker=blocker)


def _tick_merging(s: RouteState, by: dict, p: RouteParams, clock: float,
                  epoch: int,
                  arrival: ArrivalAttribution | None = None,
                  reject_shift_arrival: bool = False,
                  dt_s: float = 0.0,
                  ) -> tuple[RouteState, RouteVerdict]:
    """并线中：只认两件事——到了没有（位置 + 速率）、该不该撤（目标道连续
    确认被截）。并线期间不评估别的候选：中途改主意就是逐拍翻面。

    `soft_car` 开时「确认被截」不再认普通车：目标道有车仍继续并线，由计划层
    的 0.35 s 迎车门做物理否决；普通车擦撞只是时间成本（RULES §4.3）。
    到达门（C）只否决 `finish`：归因为 SHIFT_HELD 时本会成立的完成不成立，
    继续并线；撤销分支在它之前、超时分支照常。"""
    tgt = by.get(s.lane_target)
    t_tgt = _avail_s(tgt, p)
    since = max(0.0, clock - s.decided_t)
    unsafe = s.unsafe_s + dt_s if _cancel_blocked(tgt, p) else 0.0
    if _reached(unsafe, p.cancel_unsafe_s):
        back = _drivable_lane(s.v_ref, by)
        st = replace(s, lane_ref=back, lane_target=None, phase=PHASE_KEEP,
                     decided_t=clock, unsafe_s=0.0, merge_started_t=clock,
                     cooldown_left_s=p.cancel_cooldown_s,
                     ban_lane=s.lane_target)
        return st, RouteVerdict(PHASE_KEEP, back, None, "cancel",
                                "target_blocked", since, None, t_tgt,
                                None if tgt is None else tgt.free_m,
                                p.cancel_cooldown_s, epoch,
                                blocker=None if tgt is None else tgt.blocker)
    blocked = bool(reject_shift_arrival and arrival is not None
                   and arrival.kind == ATTR_SHIFT_HELD)
    arrived = (not blocked
               and abs(s.v_ref - s.lane_target) <= p.finish_tol_lane
               and abs(s.v_est_lane_s) <= p.finish_v_tol_lane_s)
    timeout = clock - s.merge_started_t >= p.merge_max_s
    if arrived or timeout:
        # 超时落回**实测所在道**：车可能停在半道，硬判"完成"会把命令位钉在
        # 车根本没到的道上。超时是独立结局，不冒充 finish。
        k = float(s.lane_target) if arrived else _drivable_lane(s.v_ref, by)
        st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                     phase=PHASE_KEEP, decided_t=clock, unsafe_s=unsafe,
                     merge_started_t=clock)
        return st, RouteVerdict(
            PHASE_KEEP, k, None,
            "finish" if arrived else "timeout",
            "arrived" if arrived else "merge_timeout",
            since, None, t_tgt, None, s.cooldown_left_s, epoch,
            blocker=None if tgt is None else tgt.blocker,
            blocked_finish=blocked)
    return replace(s, unsafe_s=unsafe), RouteVerdict(
        PHASE_MERGING, s.lane_ref, s.lane_target, "keep", "merging", since,
        None, t_tgt, None, s.cooldown_left_s, epoch,
        blocker=None if tgt is None else tgt.blocker,
        blocked_finish=blocked)


def _tick_keep(s: RouteState, by: dict, cur: CoarseLane, t_cur: float | None,
               p: RouteParams, clock: float,
               epoch: int,
               lane_ttc: dict[int, float] | None = None,
               passing: bool = False,
               request_action: bool | None = None,
               ) -> tuple[RouteState, RouteVerdict]:
    """保持态：只有"现任道确认被截"才动，方向是"确实更宽"。

    不设时间窗门槛：窗长 24 m ≈0.53 s，横移一道最快 0.4 s，再加连续确认——
    "能不能在撞上之前到那儿"永远判否（离线重放 20261024_p2 会得出 40% 的帧
    "无路可逃"）。多出的净空本身就是多出的时间，按净空差裁决才判得动。

    `anchor_follow` 开时，车实际所在道确认空闲、与承诺道偏离达到重锚阈值：
    跳过逃生与超车起意，等 `tick_route` 重锚——否则会按陈旧承诺道的占用起
    一次空转并线。机会请求照常按承诺道计分并起意（`_start_merge` 会改承诺道）。
    """
    since = max(0.0, clock - s.decided_t)
    alts = [c for c in by.values() if c.lane != s.lane_ref and _qualified(c)]
    if s.cooldown_left_s > 0.0 and s.ban_lane is not None:
        alts = [c for c in alts if c.lane != s.ban_lane]

    not_urgent = False
    pass_hold = bool(p.pass_hold and passing)
    drifted_free = (p.anchor_follow and s.phase == PHASE_KEEP
                    and _drifted_free(s, by, p))
    if not drifted_free and t_cur is not None \
            and _reached(s.blocked_s, p.urgent_confirm_s):
        if pass_hold:
            not_urgent = True        # 超车动作进行中：方向留给擦边目标
        elif not _urgent_time_ok(s, cur, p, lane_ttc):
            not_urgent = True        # 确认被截，但车还不到线上：留在本道过
        else:
            feas = [c for c in alts
                    if (c.blocker is None
                        or c.free_m >= cur.free_m + p.switch_gain_m)
                    and _path_clear(s, c, by)
                    and _target_time_ok(c, p, lane_ttc)]
            if p.anchor_follow:
                # 车已经在的目标道不是逃生方向（并在那儿是空转）
                feas = [c for c in feas if not _already_in(s.v_ref, c.lane)]
            if not feas:
                return s, RouteVerdict(PHASE_KEEP, s.lane_ref, s.lane_target,
                                       "urgent_no_alt", "no_alt", since, None,
                                       t_cur, cur.free_m, s.cooldown_left_s,
                                       epoch, blocker=cur.blocker,
                                       credit=s.req_credit)
            # 确认空闲优先 → 净空最大（可用时间最多）→ 离自车最近 → 道号（确定性；
            # 左右读数完全对称是常态，同分必须有个固定次序）
            alt = min(feas, key=lambda c: (0 if _clear(c) else 1, -c.free_m,
                                           abs(c.lane - s.v_ref), c.lane))
            return _start_merge(s, alt, clock), RouteVerdict(
                PHASE_MERGING, alt.lane, alt.lane, "merge_start", "urgent",
                since, alt.free_m - cur.free_m, t_cur, cur.free_m,
                s.cooldown_left_s, epoch, blocker=alt.blocker)

    # 提前超车（2026-10-30）：本道被截、车 1~2.5s 才到站，邻道**确认空闲**
    # 且横穿路径空闲 → 现在就并过去。等紧急门（≤1s）才动等于把超车拖成
    # 临门闪避（真机常见：跟到 0.9s 才起并线）。只认完全空闲的邻道：有车的
    # 道仍归紧急逃生（那边的净空差门槛更严，避免为超车并进另一辆车后面）。
    if (not drifted_free and p.overtake and not pass_hold and t_cur is not None
            and _reached(s.blocked_s, p.urgent_confirm_s)):
        cur_ttc = _cur_ttc(s, lane_ttc)
        if cur_ttc is not None \
                and p.urgent_ttc_s < cur_ttc <= p.overtake_ttc_s:
            feas = [c for c in alts if _clear(c) and _path_clear(s, c, by)]
            if p.anchor_follow:
                feas = [c for c in feas if not _already_in(s.v_ref, c.lane)]
            if feas:
                alt = min(feas, key=lambda c: (abs(c.lane - s.v_ref),
                                               -c.free_m, c.lane))
                return _start_merge(s, alt, clock), RouteVerdict(
                    PHASE_MERGING, alt.lane, alt.lane, "merge_start",
                    "overtake", since, alt.free_m - cur.free_m, t_cur,
                    cur.free_m, s.cooldown_left_s, epoch,
                    blocker=alt.blocker)

    if s.req_lane is not None:
        # 机会换道请求（选择层出界目标转来）：现任道确认空闲才让机会主导，
        # 现任被占/无法确认时不归请求管——逃生分支按自己的确认节奏裁决。
        # 承诺道陈旧但车所在道空闲时不在这里拦：请求目标相对的是承诺道，
        # 证据攒满就按它起意（_start_merge 会把承诺道改到目标道）；拦下来
        # 只会与 0.3 s 重锚互相抢，实测两段 0.30/0.32 s 的证据被静默清掉。
        if not _reached(s.req_credit, p.opportunity_confirm_s):
            # 6c：无读数冻结时计分不再增长，原因如实报 paused
            why = ("paused" if p.request_hold and s.req_state == "paused"
                   else "scoring")   # 还在攒证据，不是被拒
        elif p.request_hold and s.req_state != "active":
            # 证据够了但没有当拍有效来源：不允许起意（等下一拍可达）
            why = "no_reach"
        elif request_action is False:
            # 第 7 期动作位：证据够了但本拍原始 slack 不可达——平滑不能把
            # 不可达拍变成起意拍，等原始可达拍（离线 stuck7=0 复算）
            why = "no_reach"
        elif not _clear(cur):
            why = ("pass_hold" if pass_hold
                   else "not_urgent" if not_urgent else "current_unconfirmed")
        else:
            why = _request_reject_reason(s, s.req_lane, by)
            if why is None:
                alt = by[float(s.req_lane)]
                if p.anchor_follow and _already_in(s.v_ref, alt.lane):
                    # 请求目标是车已经在的道：承诺道陈旧，等重锚，不起并线
                    why = "already_there"
                else:
                    return _start_merge(s, alt, clock), RouteVerdict(
                        PHASE_MERGING, alt.lane, alt.lane, "merge_start",
                        "opportunity", since, None, _avail_s(alt, p), None,
                        s.cooldown_left_s, epoch, blocker=alt.blocker,
                        credit=s.req_credit)
    else:
        why = ("lane_drift" if drifted_free
               else "no_request" if _clear(cur)
               else "pass_hold" if pass_hold
               else "not_urgent" if not_urgent else "blocked_unconfirmed")
    return s, RouteVerdict(PHASE_KEEP, s.lane_ref, s.lane_target, "keep", why,
                           since, None, t_cur, None, s.cooldown_left_s, epoch,
                           blocker=cur.blocker, credit=s.req_credit)


def _request_reject_reason(s: RouteState, tgt: float,
                           by: dict) -> str | None:
    """机会请求的拒绝原因码；None=可起意。只报**第一原因**，顺序即优先级。

    拒了就维持 keep——目标早已被选择层贴回本道可活动带，被拒/撤销的车**不会
    停在两条道中间**（请求逐拍刷新，机会还在下拍还会再来）。冷却期内刚
    放弃的道不回头；`space`/`unknown_car` 不是"更保守"，是"没有判据"。
    请求口径放宽到相邻 ±2 道（2026-11-02）：判据是光锥（能不能去），
    横穿的中间道由 `_path_clear` 把关——它是唯一挡在占用车前面的门。"""
    alt = by.get(tgt)
    if tgt == s.lane_ref:
        return "same_lane"
    if alt is None:
        return "target_missing"      # 不能悄悄换成别的道当替补
    if abs(alt.lane - s.lane_ref) > REQ_SPAN_LANE:
        return "not_adjacent"
    if not _qualified(alt):
        return "target_unqualified"
    if s.cooldown_left_s > 0.0 and s.ban_lane is not None \
            and alt.lane == s.ban_lane:
        return "cooldown_ban"
    if not _path_clear(s, alt, by):
        return "path_blocked"
    return None


def _start_merge(s: RouteState, alt: CoarseLane,
                 clock: float) -> RouteState:
    return replace(s, lane_ref=alt.lane, lane_from=s.lane_ref,
                   lane_target=alt.lane, phase=PHASE_MERGING,
                   decided_t=clock, unsafe_s=0.0,
                   merge_started_t=clock, cooldown_left_s=s.cooldown_left_s,
                   req_lane=None, req_credit=0.0, req_oid=None, req_side=0,
                   req_age_s=0.0, req_pause_s=0.0, req_state="none")


def _needs_init(state: RouteState | None, epoch: int,
                since: float) -> RouteVerdict:
    """当拍无车道级证据：如实报待重新初始化，不臆造参考道。"""
    ref = None if state is None else state.lane_ref
    tgt = None if state is None else state.lane_target
    ph = PHASE_KEEP if state is None else state.phase
    cr = None if state is None else state.req_credit
    return RouteVerdict(ph, ref, tgt, "needs_init", "no_evidence", since,
                        None, None, None, 0.0, epoch, credit=cr)
