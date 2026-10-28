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
   计分到 `opportunity_confirm_ticks`**（断一拍扣一分、换侧重起计）才起意
   并线；拒了就维持 keep——目标早已被选择层贴回本道可活动带，永远不会停在两条
   道中间。计分门槛裁决的是「机会真的在那条道上」，不是「这一拍看见了东西」：
   实机一局原始请求 61 段里 40 段只有 1~2 拍（币组检测闪断）。
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

# 截断来源分类（沿用粗层 CoarseLane.blocker 的语义，不另立枚举真源）：
# None=确认空闲；"space"/"car"=确认被占；"unknown_car"=无法确认。
KNOWN_BLOCKERS = ("space", "car")
# 替代道资格：几何走不下（space）与无法确认（unknown_car）都不合格——后者
# 不是"更保守"，是"没有判据"。
UNQUALIFIED = ("space", "unknown_car")


@dataclass(frozen=True)
class RouteParams:
    """路线层参数（真源 decision.json [route]；尺度/速度类复用既有真源）。"""

    switch_gain_m: float = 4.0       # 逃生要求"多出一个换道门槛"的净空（与粗层
    # 换道迟滞同源一个数：差值不到一个门槛就不值得为它动方向）
    proxy_speed_mps: float = 45.0    # 米→秒折算代理（复用 [coarse] 真源，仅 trace）
    urgent_confirm_ticks: int = 3    # 现任道占用要连续这么多拍才起意（防闪检）
    opportunity_confirm_ticks: int = 6  # 机会换道请求的起意计分门槛（同侧连续
    # +1、断一拍 −1，泄漏式）：165841 局实测原始请求 61 段里 26 段只有 1 拍、
    # 14 段只有 2 拍（币组检测闪断）；不设这道门一局要并线 60 次（人工 7~9 次）。
    # 确认的是**证据持续性**，不是把方向盘锁住——计分期间目标始终在本道区间内。
    cancel_unsafe_ticks: int = 5     # 撤销判定的连续帧计数（防单帧野值弃窗）
    cancel_cooldown_s: float = 2.0   # 撤销后冷却：期内不回刚放弃的那条道
    finish_tol_lane: float = 0.3     # 完成的位置容差（同 L4 中心带宽）
    finish_v_tol_lane_s: float = 0.4  # 完成的横速容差：带着横速"完成"正是过冲
    merge_max_s: float = 4.0         # 并线超时（人工 p90 量级）：到点按实测
    # 所在道落回 keep——卡在并线态会让路线永不重启
    soft_car: bool = False           # 普通车（car）不构成撤销理由：车是时间成本，
    # 不是物理约束；目标道 space 仍按参考道失去几何重建。False=旧行为


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
    unsafe_ticks: int                # 目标道危险证据的连续帧计数
    blocked_ticks: int               # 现任道被截的连续帧计数（逃生确认）
    epoch: int                       # 定案时的参考系纪元；与当拍不等 = 换纪作废
    v_ref: float                     # 上一拍实测横位（道，粗层帧）
    merge_started_t: float           # 本次并线起意时刻（超时判据用）
    v_est_lane_s: float              # 实测横位差分（道/s）：完成判据的速率面
    req_lane: float | None = None    # 当拍机会换道请求目标道（选择层出界
    # 目标转来的请求；逐拍刷新、不跨帧持有——机会没了请求自然消失）
    req_credit: float = 0.0          # 同侧请求的累计分（起意门槛见 params）


@dataclass(frozen=True)
class RouteVerdict:
    """一拍定案结果（trace 观测面：离线要能复算为什么放行/为什么被拒）。"""

    phase: str
    lane_ref: float | None
    lane_target: float | None
    verdict: str                     # init/keep/merge_start/finish/timeout/
    # cancel/urgent_no_alt/epoch_reset/rebuild/needs_init
    why: str                         # 保持或被拒的第一原因码（keep 态口径见
    # `_request_reject_reason`：无请求 no_request/blocked_unconfirmed，未到门槛
    # scoring，被拒 current_unconfirmed/same_lane/not_adjacent/target_missing/
    # target_unqualified/cooldown_ban/path_blocked）
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

    def as_trace(self) -> dict:
        def _r(x, nd):
            return None if x is None or not math.isfinite(x) else round(x, nd)
        return {"ph": self.phase, "ref": _r(self.lane_ref, 3),
                "tgt": _r(self.lane_target, 3), "vd": self.verdict,
                "why": self.why, "age": _r(self.since_s, 2),
                "gain": _r(self.gain_m, 2), "tc": _r(self.t_cur_s, 3),
                "ev": _r(self.evidence_m, 2), "cd": _r(self.cooldown_left_s, 2),
                "ep": self.epoch, "blk": self.blocker,
                "crc": _r(self.credit, 1)}


def init_state(epoch: int) -> RouteState:
    """冷启动参考道=路心道（0）。

    没有承诺时不动横向：0 道就是选择帧中心线本身，与粗层不接控制时的 L4
    回中逐位同形——V1 的行为差异只该来自"道号被承诺门控"，不该来自启动瞬间
    凭空挑一条最近的道压过去。作废/换纪的降级另走 `_rebuild`（那里可能有
    未完成的动作要保护，落回实测可达道）。"""
    return RouteState(0.0, 0.0, None, PHASE_KEEP, 0.0, 0.0, 0.0, None, 0, 0,
                      epoch, 0.0, 0.0, 0.0)


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


def _path_clear(s: RouteState, alt: CoarseLane, by: dict) -> bool:
    """并入 alt 要横穿的中间道必须**确认空闲**。跨一条被占的道并线＝
    从占用车上碾过去：20261027_160949_1 s2 24.3s 现任道与右道都被 0 m 车
    占着，urgent 并到 −1，中间道上正贴着一辆 z 丢失的车——d_min 0.004。"""
    lo, hi = sorted((round(s.lane_ref), int(alt.lane)))
    return all(_clear(by.get(float(k))) for k in range(lo + 1, hi))


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
               request: float | None = None) -> tuple[RouteState | None, RouteVerdict]:
    """推进一拍。

    `report`：当拍**原始**粗层报告（逐道净空/占用三态），不是接控制的道号。
    `epoch`：参考系纪元（尺子跳变时递增）。返回 (新状态, 定案报告)；缺证据时
    给 `needs_init`，**不回退旧坐标**（旧道号在新参考系里没有原意）。
    `request`：当拍机会换道请求——**选择层出的是相对承诺道的偏移（±1）**，本层
    是相对→绝对的换算边界，状态里 `req_lane` 存**绝对**目标道（2026-10-27 契约
    冻结）。逐拍刷新，不持久。
    **请求计分的生命周期**（与换算同批冻结）：① 只有保持态累计与消费——并线中
    机会层不出目标，留着旧进度等于让上一道的判断跨状态生效；② 任何改 `lane_ref`
    的路径（`_start_merge`/`_rebuild`）随承诺道一起清空请求与计分，换算基准
    （消费拍的 `lane_ref`）因此始终等于生成拍的；③ 缺车道级证据的拍清零，不冻结
    也不漏分——连续性已断，断供前后两段凑不成一次证据。
    """
    if report is None or not report.lanes or report.reason == "no_road":
        # 没有车道级证据：时钟继续走，但路线不推进、不判逃生——证据缺席
        # 不等于危险解除，也不等于可以随便动
        if state is None:
            return None, _needs_init(state, epoch, 0.0)
        st = replace(state, elapsed_s=state.elapsed_s + max(dt_s, 0.0),
                     req_lane=None, req_credit=0.0)
        return st, _needs_init(st, epoch, 0.0)
    if state is None:
        s = init_state(epoch)
        return s, RouteVerdict(PHASE_KEEP, s.lane_ref, None, "init", "cold",
                               0.0, None, None, None, 0.0, epoch)

    v_est = ((report.ego_lane - state.v_ref) / dt_s
             if dt_s > 0.0 else state.v_est_lane_s)
    clock = state.elapsed_s + max(dt_s, 0.0)
    # 请求计分：同侧连续 +1、断拍 −1（泄漏到 0 才忘掉那条道），换侧重置起计。
    # 泄漏而不是清零：实机里真实币线的请求拍中位隔 6~7 拍才被重新看见一次
    # （检测占空比约一半），断拍即清零等于门槛永不达标；反之 1~2 拍的闪检
    # 攒不到分。门槛裁决「机会真的在那条道上」，不锁方向盘——计分期间目标
    # 始终在本道区间内。
    # 生命周期门（①）：并线中一律不累计——承诺期的机会请求没有来源（选择层
    # 关闭机会层），攒到的分属于上一道。只拒绝本拍不够：旧进度会在完成拍带着
    # 跨状态的分数提前起意（实测 6 拍门被穿到 4 拍），所以失效状态清零。
    if state.phase != PHASE_KEEP:
        req_lane, credit = None, 0.0
    elif request is None:
        credit = max(0.0, state.req_credit - 1.0)
        req_lane = state.req_lane if credit > 0 else None
    else:
        # 相对偏移 → 绝对目标道（契约见函数 docstring）。计分比较一律在绝对
        # 道号上做，否则承诺道非 0 时"相对 +1"会被当成"绝对 +1"= 当前道，
        # 机会换道结构上永不成立（实跑 20261027_183143 观测到的 0 触发）。
        req_abs = state.lane_ref + float(request)
        if req_abs == state.req_lane:
            req_lane, credit = req_abs, state.req_credit + 1.0
        else:
            # 新请求（含旧请求已清零后重新出现）：从 1 分起计——计分衡量的是
            # 「这条机会连续被看见了几拍」，首拍看见就该算一分。
            req_lane, credit = req_abs, 1.0
    s = replace(state, elapsed_s=clock, v_ref=report.ego_lane,
                v_est_lane_s=v_est, req_lane=req_lane, req_credit=credit,
                cooldown_left_s=max(0.0, state.cooldown_left_s - dt_s))
    by = {c.lane: c for c in report.lanes}
    if epoch != s.epoch:
        # 换纪 = 参考系重基：跨帧同一道号的几何含义失效。作废并降级到实测
        # 可达道，不猜旧目标道还在不在。重建不是决定，`decided_t` 原样保留
        return _rebuild(s, by, report.ego_lane, clock, epoch, p, "epoch_reset",
                        "anchor_rebase")
    cur = by.get(s.lane_ref)
    if cur is None or cur.blocker == "space":
        # 参考道失去几何：要么掉出候选集（弯道把车甩出 ±max_offset），要么
        # 该道被走廊判成走不下。钉在一条不存在的道上时目标会跟着它平移＝
        # 朝墙打舵，且逃生判据也失去输入——立刻重建到可行道
        return _rebuild(s, by, report.ego_lane, clock, epoch, p, "rebuild",
                        "lane_dropped" if cur is not None
                        else "lane_out_of_range",
                        blocker=None if cur is None else cur.blocker)
    # 现任道被截的连续拍数：逃生要"持续占用"，单帧闪检不起意（与撤销判据
    # 同一族的帧计数滞回；观察窗只有 0.5 s，帧计数是最便宜的去噪面）
    s = replace(s, blocked_ticks=(s.blocked_ticks + 1
                                  if _known_blocked(cur) else 0))
    if s.phase == PHASE_MERGING:
        return _tick_merging(s, by, p, clock, epoch)
    return _tick_keep(s, by, cur, _avail_s(cur, p), p, clock, epoch)


def _rebuild(s: RouteState, by: dict, ego_lane: float, clock: float,
             epoch: int, p: RouteParams, verdict: str,
             why: str, blocker: str | None = None) -> tuple[RouteState, RouteVerdict]:
    """降级为 KEEP(实测可达道)：并线作废、冷却清零、定案时钟不清。"""
    k = _drivable_lane(ego_lane, by)
    st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                 phase=PHASE_KEEP, unsafe_ticks=0, cooldown_left_s=0.0,
                 ban_lane=None, epoch=epoch, merge_started_t=clock,
                 req_lane=None, req_credit=0.0)
    return st, RouteVerdict(PHASE_KEEP, k, None, verdict, why,
                            max(0.0, clock - st.decided_t), None,
                            _avail_s(by[k], p), None, 0.0, epoch,
                            blocker=blocker)


def _tick_merging(s: RouteState, by: dict, p: RouteParams, clock: float,
                  epoch: int) -> tuple[RouteState, RouteVerdict]:
    """并线中：只认两件事——到了没有（位置 + 速率）、该不该撤（目标道连续
    确认被截）。并线期间不评估别的候选：中途改主意就是逐拍翻面。

    `soft_car` 开时「确认被截」不再认普通车：目标道有车仍继续并线，由计划层
    的 0.35 s 迎车门做物理否决；普通车擦撞只是时间成本（RULES §4.3）。"""
    tgt = by.get(s.lane_target)
    t_tgt = _avail_s(tgt, p)
    since = max(0.0, clock - s.decided_t)
    unsafe = s.unsafe_ticks + 1 if _cancel_blocked(tgt, p) else 0
    if unsafe >= p.cancel_unsafe_ticks:
        back = _drivable_lane(s.v_ref, by)
        st = replace(s, lane_ref=back, lane_target=None, phase=PHASE_KEEP,
                     decided_t=clock, unsafe_ticks=0, merge_started_t=clock,
                     cooldown_left_s=p.cancel_cooldown_s,
                     ban_lane=s.lane_target)
        return st, RouteVerdict(PHASE_KEEP, back, None, "cancel",
                                "target_blocked", since, None, t_tgt,
                                None if tgt is None else tgt.free_m,
                                p.cancel_cooldown_s, epoch,
                                blocker=None if tgt is None else tgt.blocker)
    arrived = (abs(s.v_ref - s.lane_target) <= p.finish_tol_lane
               and abs(s.v_est_lane_s) <= p.finish_v_tol_lane_s)
    timeout = clock - s.merge_started_t >= p.merge_max_s
    if arrived or timeout:
        # 超时落回**实测所在道**：车可能停在半道，硬判"完成"会把命令位钉在
        # 车根本没到的道上。超时是独立结局，不冒充 finish。
        k = float(s.lane_target) if arrived else _drivable_lane(s.v_ref, by)
        st = replace(s, lane_ref=k, lane_from=k, lane_target=None,
                     phase=PHASE_KEEP, decided_t=clock, unsafe_ticks=unsafe,
                     merge_started_t=clock)
        return st, RouteVerdict(
            PHASE_KEEP, k, None,
            "finish" if arrived else "timeout",
            "arrived" if arrived else "merge_timeout",
            since, None, t_tgt, None, s.cooldown_left_s, epoch,
            blocker=None if tgt is None else tgt.blocker)
    return replace(s, unsafe_ticks=unsafe), RouteVerdict(
        PHASE_MERGING, s.lane_ref, s.lane_target, "keep", "merging", since,
        None, t_tgt, None, s.cooldown_left_s, epoch,
        blocker=None if tgt is None else tgt.blocker)


def _tick_keep(s: RouteState, by: dict, cur: CoarseLane, t_cur: float | None,
               p: RouteParams, clock: float,
               epoch: int) -> tuple[RouteState, RouteVerdict]:
    """保持态：只有"现任道确认被截"才动，方向是"确实更宽"。

    不设时间窗门槛：窗长 24 m ≈0.53 s，横移一道最快 0.4 s，再加连续确认——
    "能不能在撞上之前到那儿"永远判否（离线重放 20261024_p2 会得出 40% 的帧
    "无路可逃"）。多出的净空本身就是多出的时间，按净空差裁决才判得动。
    """
    since = max(0.0, clock - s.decided_t)
    alts = [c for c in by.values() if c.lane != s.lane_ref and _qualified(c)]
    if s.cooldown_left_s > 0.0 and s.ban_lane is not None:
        alts = [c for c in alts if c.lane != s.ban_lane]

    if t_cur is not None and s.blocked_ticks >= p.urgent_confirm_ticks:
        feas = [c for c in alts
                if (c.blocker is None or c.free_m >= cur.free_m + p.switch_gain_m)
                and _path_clear(s, c, by)]
        if not feas:
            return s, RouteVerdict(PHASE_KEEP, s.lane_ref, s.lane_target,
                                   "urgent_no_alt", "no_alt", since, None,
                                   t_cur, cur.free_m, s.cooldown_left_s, epoch,
                                   blocker=cur.blocker, credit=s.req_credit)
        # 确认空闲优先 → 净空最大（可用时间最多）→ 离自车最近 → 道号（确定性；
        # 左右读数完全对称是常态，同分必须有个固定次序）
        alt = min(feas, key=lambda c: (0 if _clear(c) else 1, -c.free_m,
                                       abs(c.lane - s.v_ref), c.lane))
        return _start_merge(s, alt, clock), RouteVerdict(
            PHASE_MERGING, alt.lane, alt.lane, "merge_start", "urgent", since,
            alt.free_m - cur.free_m, t_cur, cur.free_m, s.cooldown_left_s,
            epoch, blocker=alt.blocker)

    if s.req_lane is not None:
        # 机会换道请求（选择层出界目标转来）：现任道确认空闲才让机会主导，
        # 现任被占/无法确认时不归请求管——逃生分支按自己的确认节奏裁决。
        if s.req_credit < p.opportunity_confirm_ticks:
            why = "scoring"          # 还在攒证据，不是被拒
        elif not _clear(cur):
            why = "current_unconfirmed"
        else:
            why = _request_reject_reason(s, s.req_lane, by)
            if why is None:
                alt = by[float(s.req_lane)]
                return _start_merge(s, alt, clock), RouteVerdict(
                    PHASE_MERGING, alt.lane, alt.lane, "merge_start",
                    "opportunity", since, None, _avail_s(alt, p), None,
                    s.cooldown_left_s, epoch, blocker=alt.blocker,
                    credit=s.req_credit)
    else:
        why = "no_request" if _clear(cur) else "blocked_unconfirmed"
    return s, RouteVerdict(PHASE_KEEP, s.lane_ref, s.lane_target, "keep", why,
                           since, None, t_cur, None, s.cooldown_left_s, epoch,
                           blocker=cur.blocker, credit=s.req_credit)


def _request_reject_reason(s: RouteState, tgt: float,
                           by: dict) -> str | None:
    """机会请求的拒绝原因码；None=可起意。只报**第一原因**，顺序即优先级。

    拒了就维持 keep——目标早已被选择层贴回本道可活动带，被拒/撤销的车**不会
    停在两条道中间**（请求逐拍刷新，机会还在下拍还会再来）。冷却期内刚
    放弃的道不回头；`space`/`unknown_car` 不是"更保守"，是"没有判据"。
    `path_blocked` 今天的生产路径恒真（请求只出相邻 ±1 道，中间道集合为空），
    留着是契约的守卫：请求口径一旦放宽，它就是唯一挡在占用车前面的门。"""
    alt = by.get(tgt)
    if tgt == s.lane_ref:
        return "same_lane"
    if alt is None:
        return "target_missing"      # 不能悄悄换成别的道当替补
    if abs(alt.lane - s.lane_ref) > 1.5:
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
                   decided_t=clock, unsafe_ticks=0,
                   merge_started_t=clock, cooldown_left_s=s.cooldown_left_s,
                   req_lane=None, req_credit=0.0)


def _needs_init(state: RouteState | None, epoch: int,
                since: float) -> RouteVerdict:
    """当拍无车道级证据：如实报待重新初始化，不臆造参考道。"""
    ref = None if state is None else state.lane_ref
    tgt = None if state is None else state.lane_target
    ph = PHASE_KEEP if state is None else state.phase
    cr = None if state is None else state.req_credit
    return RouteVerdict(ph, ref, tgt, "needs_init", "no_evidence", since,
                        None, None, None, 0.0, epoch, credit=cr)
