# -*- coding: utf-8 -*-
"""走廊收益场选路径（2026-10-14 设计稿 §3.2/§3.3）——米制连续坐标的综合收益评估。

**它是什么**：每拍对走廊近场可行驶域按 0.5m 步进采样候选横向位置，逐候选做
**综合收益评估**（沿线可收的全部金币组整组值×时间语义 + 可错身车 pass_value
− 通往碰撞的大负罚 − 横向位移代价 + 远币远权），输出逐候选得分、胜出位置与
路径序列（近端=自车当前横向位，逐带插值到胜出位）。可达性即门槛：不可达的
收益不计入该候选——不再有「等进带 + 来得及检查」两道串行关卡，提前量天然
编码在 need(|Δx|)+tau+margin < t_miss 里。

**它不是什么**：不做行为切换（闸与 FSM 在 decision.py，本模块只回答「哪条
路径当期收益最高」）；不消费车道单位（车道→米制换算在**进决策边界一次**，
由调用方完成——timing 的变道耗时模型天然以道计，调用方以 ``need_of_dx_m``
闭包注入，本模块内无车道单位）；不执行轨迹（路径近带点喂 planner PD 链）。

**语义真源**：收益构成沿退役前 lane_score.benefits 摊开到连续坐标（事件语义——每个
对象记一次，不按可见拍重复计；lane_score 于 20261015 被本模块取代删除，历史
结论由 commit 指针回溯）：币整组值 = coin_value_per_unit × max(观测枚数,
组下限) × conf（组下限 7c28ce9 复用，残缺视图按整组计）；车 t_meet 的占用/
碰撞/错身三分与退役前 lane_score 同判；远币（到站 > 前瞻窗或仪器无读数）记远权，
方向信息仍在。金币「可收」三条件：横向落候选归属半宽内、可达（need+tau+
margin < t_miss）、无先于币到达的车遮挡——不可达**不计入也不降权**（时序窗
病理的反面：来不及的目标不追，不再被 feas 门否掉整个 CHANGE）。

**坐标基准**：米制 x_m（自车系，左负右正），走廊缘、目标、自车位置同一坐标
系（解耦 lane_w_m 单点标定锚对全链的放大）。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

# 道归属半宽（道，右正）：币/车横向归属判据的换算件（attribution_half_m =
# LANE_HALF × lane_w_m 在进决策边界算）。原 lane_score 住户，20261015 退役随迁。
LANE_HALF = 0.75


@dataclass(frozen=True)
class CoinSource:
    """一个金币组（边界处换算好的米制目标）。"""

    x_m: float                 # 组心（米，自车系，左负右正）
    observed_count: float      # 观测枚数（组下限计分按 max(枚数, 组下限)）
    conf: float                # 置信折扣（置信地板由调用方按 Scorer 语义过滤）
    t_miss_s: float | None     # 组到站时刻（Scorer.score 仪器）；None=仪器无读数


@dataclass(frozen=True)
class BonusSource:
    """一辆奖励车（R5 层 1，2026-10-17 设计稿 §9.1；plan.py bonus 候选同源
    几何，单一真源不复制）。撞上去触发退场 180×N / barrelRoll 120——收益
    入口不是障碍（不在车流 views 里，veto/采样天然不拦）。"""

    x_m: float                 # 车体位（米，自车系，左负右正）
    t_miss_s: float            # 到站时刻（Scorer.car_t_meet_s 仪器）
    conf: float                # 置信（审计面）
    tol_m: float | None = None  # 归属半容差（R5 §10 契约加法：远场粗源设宽值
                                # 「大可以模糊」，None=走 params.attribution_half_m
                                # 近场原值）


@dataclass(frozen=True)
class CarSource:
    """一辆车的米制到站仪器（占用/碰撞/错身三分共用）。

    t_overtake_s 是 R3 贴身猎车的**被超前车仪器**（2026-10-16 设计稿 §2.1）：
    米制锚齐备（z_m/v_close>0）的前车边界预计算 t=z/v_close——v_close>0=被
    接近（设计稿原文写 v_close<0 系符号笔误，L0 契约 MetricCarState 与实机
    trace 对账（符号一致率 70.8%、近距离车 87% 为正）均 v>0=接近，数值同为
    z/|v_close|）；估计窗未开/远离/已过（t≤0）=None=不追（仪器缺位弃权）。
    None 的车走旧错身项，行为逐位回今天。"""

    x_m: float                 # 车横位（米，自车系）
    t_meet_s: float | None     # 到站时刻；None=pacing/掠过（占用语义，不判碰撞）
    t_overtake_s: float | None = None   # 被超时刻（R3 前车仪器）；None=无米制锚
    # 速度像素桥（闸 metric_bridge.enabled，默认关=False=逐位回今天）：
    # True=本车 t_meet_s 是桥接值（不确定度走 bridge_weight/bridge_margin_add_s）
    bridged: bool = False


@dataclass(frozen=True)
class CorridorBandSpan:
    """一个走廊带的横向可行驶域（调用方从墙缘/绿区边界换算，米制）。

    left/right 为 None 表示该带该侧无证据（unknown）——近场可行驶域取**首个
    双侧有证据的带**；全无可行驶域=诚实弃权（None，不臆造路宽）。"""

    z0: float                  # 带起点（米，车头前；帧由调用方统一）
    z1: float
    left: float | None
    right: float | None


@dataclass(frozen=True)
class ReachParams:
    """收益场参数（数值真源 decision.json corridor_reach 节；车道换算件由
    调用方在边界注入，不进本表）。"""

    step_m: float = 0.5                  # 候选步进（米，与栅格格宽同量级）
    horizon_s: float = 2.0               # 收益前瞻（s）
    crash_t_s: float = 0.8               # 到站短于此判碰撞（s）
    crash_penalty: float = 100.0         # 碰撞候选负额（分；压过任何收益）
    pass_value: float = 30.0             # 错身保底（分）
    far_coin_weight: float = 0.3         # 前瞻窗外币的远权（方向信息仍在）
    tie_margin: float = 20.0             # 平手阈（分）：差 < 此值偏向离中心近者
    shift_cost_per_m: float = 0.0        # 横向位移代价（分/米，边界处由车道价换算）
    coin_value_per_unit: float = 30.0    # 单枚币值（真源 scoring 段）
    coin_group_min_count: float = 3.0    # 组下限（7c28ce9 复用：残缺视图按整组计）
    attribution_half_m: float = 1.7      # 币/车横向归属半宽（米，调用方由道半宽换算）
    pass_band_m: float = 3.38            # 错身外带宽（米，调用方由道宽换算）
    tau_s: float = 0.13                  # 响应时间常数（s，可达余量之一）
    margin_s: float = 0.15               # 安全裕度（s，可达余量之二）
    need_of_dx_m: Callable[[float], float] = None  # type: ignore[assignment]
    #   变道耗时模型 need(|Δx|)（秒）——timing 线性模型天然以道计，调用方在
    #   边界闭包换算（|Δx|/lane_w_m 后 lane_change_duration_s），本模块无车道单位
    # ---- R3 贴身猎车（2026-10-16 设计稿 §2.2；米制，全 None=闸关=逐位回今天）----
    hug_d_lo_m: float | None = None      # 贴身窗下沿（米）：d̂<此值=同车道贴尾不是错身，0 分
    hug_d_hi_m: float | None = None      # 上沿（米）：窗内=平台常数，窗外 logistic 连续衰减
    hug_p_center_m: float | None = None  # P_limit logistic 中心（米；真源 overtake.p_center_lane，
                                         #   调用方在边界 ×lane_w 换算，本表不复制数值）
    hug_p_scale_m: float | None = None   # logistic 尺度（米，同上）
    hug_value_limit: float = 0.0         # 极限动作分（RULES §4.11 镜像，真源 overtake.value_limit）
    # ---- R2 前瞻外方向权（2026-10-16 设计稿 §2.4；米制，hunt_horizon None=闸关）----
    # 奖励相对远车预选未来通过侧（非泛义"车流密集车道占位"——那是车道级密度
    # 吸引，另一机制，本表不做）；E 复用 R3 贴身同一条曲线（机制同源）：
    # d̂=0（正对远车）为 0、贴身窗最大、窗外衰减。作用域=近场 horizon 外、
    # hunt_horizon 内的米制锚前车（t_overtake 仅可测才计，估计缺位不奖）；
    # 近场 horizon 内是 R3 的地盘，不双计。
    far_car_weight: float = 0.0          # 远车方向权（标定对象随胜出聚合语义定）
    hunt_horizon_s: float | None = None  # 前瞻外作用域上界（s，建议 6s [需实测]）；
                                         #   None=R2 关
    far_aggregation: str = "max"         # 多车聚合三语义：max | sum | capped
                                         #   （capped 上限=value_limit+pass_value）
    # ---- R5 四层优先级（2026-10-17 设计稿 §9.1；bonus_value=0=闸关=逐位回今天）----
    bonus_value: float = 0.0             # 奖励车收益分（0=闸关；起值 120=一次极限
                                         #   动作分 [需实测]）
    # ---- R5 层 2 擦边倾向（2026-10-17 设计稿 §9.2；hug_visual=False=逐位回今天）----
    hug_visual: bool = False             # hug 分支扩到像素仪器车：t_overtake 缺位
                                         #   （米制锚未成形，覆盖仅 5%）的车回退
                                         #   t_meet_s（仪器链米制优先像素回退同源）
    # ---- 速度像素桥不确定度（2026-10-17 两局实测；None=闸关=逐位回今天）----
    # 桥接车 t_meet_s 的不确定度走消费面分权：threat 权重作用于碰撞/遮挡
    # 判据（时间放大 1/weight=威胁减半），裕度放宽作用于贴身可达门。
    bridge_weight: float | None = None   # 威胁降权（初值 0.5，实机标定候选）
    bridge_margin_add_s: float | None = None  # 桥接车可达门裕度放宽量（s）
                                         #   过同一可达门与 E 曲线
    # ---- R5 §10 奖励车归属容差（近远场同源；20261017 A/B 两轮教训定窄）----
    bonus_tol_m: float = 0.75            # 奖励车归属半容差（米）：宽了平价 120 摊成
                                         #   平台被平手阈拉回路心（fid1493 实测）；
                                         #   0.75 平台 2~3 候选，胜出位落源 ±0.5m 内


@dataclass(frozen=True)
class CandidateScore:
    """一个候选横向位的得分评定（trace cr_* 对照列消费）。"""

    x_m: float
    score: float
    crash: bool                 # 通往碰撞（大负罚，非 argmax 候选）
    coin_value: float           # 可收币累加（含远权折减）
    pass_value_sum: float       # 可错身车累加
    shift_cost: float           # 横向位移代价（正值，扣项）
    reachable_coins: int        # 计入整组值的币组数（审计面）
    far_coins: int              # 记远权的币组数（审计面）
    coin_t: tuple[float, ...] = ()   # 可收整组币的到站 t_miss（提前量仪器，
                                     # 20261015 control 期 instrumentation 补齐）
    hug_value: float = 0.0      # R3 贴身 E 项累加（审计面；互斥分支——出此值的车
                                # 不进旧错身项 pass_value_sum，同一辆车不双计）
    hug_cars: int = 0           # 计入贴身 E 项的前车数（审计面）
    far_value: float = 0.0      # R2 前瞻外方向权累加（审计面，w 折前；近场车不双计）
    bonus_value: float = 0.0    # R5 奖励车项累加（审计面；闸关恒 0）
    bonus_cars: int = 0         # 计入奖励车项的车数（审计面）


@dataclass(frozen=True)
class ReachReport:
    """一拍走廊收益评定（选路径的执行与防抖在 decision.py）。"""

    candidates: tuple[CandidateScore, ...]
    best_x_m: float             # 胜出横向位（argmax；平手选离中心近者）
    center_x_m: float           # 近场走廊中心（平手偏好的参照）
    path: tuple[tuple[float, float], ...]   # (z_m, x_m) 近→远：近端=executed，
    #   远端=best，逐带线性插值
    best_coin_t: tuple[float, ...] = ()     # 胜出候选的可收币 t_miss（提前量仪器，
    #   20261015 control 期 instrumentation 补齐）


def _coin_group_value(coin: CoinSource, p: ReachParams) -> float:
    """整组值 = 单枚 × max(观测枚数, 组下限) × conf（组下限 7c28ce9 复用）。"""
    n = max(coin.observed_count, p.coin_group_min_count)
    return p.coin_value_per_unit * n * coin.conf


def _hug_p(d_m: float, p: ReachParams) -> float:
    """P_limit logistic（米制连续坐标，与道单位形状逐点等价——线性缩放）。
    参数真源 overtake 节（p_center/p_scale），调用方边界换算进表；本函数只管
    曲线形状不认数值。贴身窗平台值 = pass_value + limit × P(平台中心)（真源
    推导非手选）：lane_w=3.38 时 P(0.625 道)≈0.852 → 平台 ≈132。"""
    return 1.0 / (1.0 + math.exp((d_m - p.hug_p_center_m) / p.hug_p_scale_m))


def _hug_e(d_m: float, p: ReachParams) -> float:
    """贴身 E 曲线（R3/R2 机制同源的单一真源，2026-10-16 设计稿 §2.2/§2.4）：
    d̂ < 下沿 = 0（硬地板，同车道贴尾/正对远车不是通过侧）；窗内 = 平台常数
    （P 在窗中心取值）；窗外 = pass + limit × P(d̂) logistic 连续衰减。
    R3 直接消费此值；R2 以 w_far_car 折权后消费同一形状。"""
    if d_m < p.hug_d_lo_m:
        return 0.0
    if d_m <= p.hug_d_hi_m:
        return p.pass_value + p.hug_value_limit * _hug_p(
            (p.hug_d_lo_m + p.hug_d_hi_m) / 2.0, p)
    return p.pass_value + p.hug_value_limit * _hug_p(d_m, p)


def evaluate(bands: Sequence[CorridorBandSpan],
             coins: Sequence[CoinSource],
             cars: Sequence[CarSource],
             executed_x: float,
             params: ReachParams,
             bonuses: Sequence[BonusSource] = ()) -> ReachReport | None:
    """一拍走廊收益评定。无近场可行驶域（全带缺证据）=诚实弃权（None）；
    空旷路=合法结果（候选间只有位移代价差，胜出=离自车最近的可行位）。"""
    span = next(((b.left, b.right) for b in bands
                 if b.left is not None and b.right is not None), None)
    if span is None or not (params.step_m > 0):
        return None
    left, right = span
    if not right > left:
        return None
    center = (left + right) / 2.0

    # ---- 候选集：近场 span 内 0.5m 步进（格心采样，span 8m → ~16 候选） ----
    n = max(1, int(math.ceil((right - left) / params.step_m - 1e-9)))
    step = (right - left) / n
    xs = [left + (i + 0.5) * step for i in range(n)]

    def need_of(x: float) -> float:
        return params.need_of_dx_m(abs(x - executed_x))

    # R3/R2 总开关（参数缺=None=逐位回今天；分开关语义在 config）
    hug_on = params.hug_d_lo_m is not None
    far_on = (params.hunt_horizon_s is not None and params.far_car_weight > 0
              and hug_on)   # R2 复用贴身窗沿曲线，无 R3 参数时无 E 可言

    # ---- 车三分预备（事件语义：每车记一次） ----
    # 本道（归属半宽内）的车：碰撞/遮挡判据面；先于币到达的车把币挡住。
    # 威胁读数：桥接车（metric_bridge 闸开才可能有）t_meet_s 不确定度走
    # 降权——时间放大 1/weight（weight=0.5 → 威胁减半，实机标定候选）；
    # 闸关/非桥接=原值逐位回今天。
    def threat_t(c) -> float | None:
        t = c.t_meet_s
        if t is not None and c.bridged and params.bridge_weight:
            return t / params.bridge_weight
        return t

    def car_in_path(x: float) -> list[float | None]:
        return [threat_t(c) for c in cars
                if abs(c.x_m - x) <= params.attribution_half_m]

    out: list[CandidateScore] = []
    for x in xs:
        need = need_of(x)
        # 碰撞：到得了之后才到、到站短于 crash_t 的车（与退役前 lane_score
        # 同判；pacing 车 t=None 是占用不是碰撞）
        tms = car_in_path(x)
        crash = any(t is not None and need <= t < params.crash_t_s for t in tms)
        if crash:
            out.append(CandidateScore(x, -params.crash_penalty, True,
                                      0.0, 0.0,
                                      params.shift_cost_per_m * abs(x - executed_x),
                                      0, 0))
            continue
        coin_value = 0.0
        n_reach = n_far = 0
        coin_ts: list[float] = []           # 可收整组币的到站（提前量仪器）
        for coin in coins:
            if abs(coin.x_m - x) > params.attribution_half_m:
                continue                    # 横向不落归属半宽：不是这条路径的币
            val = _coin_group_value(coin, params)
            if coin.t_miss_s is not None and math.isfinite(coin.t_miss_s) \
                    and coin.t_miss_s <= params.horizon_s:
                # 可达性即门槛：耗时+τ+margin < t_miss 才计入（提前量天然编码；
                # crash/pass 用纯耗时 need——与退役前 lane_score 同判）
                if not need + params.tau_s + params.margin_s < coin.t_miss_s:
                    continue                # 来不及：不追（不计入也不降权）
                # 遮挡：先于币到达的车把币挡住（我们赶到前已过的车 t<need
                # 不挡、pacing 车 t=None 整窗占用——与退役前 lane_score 同判；
                # 桥接车威胁降权同 crash 判据）
                if any(abs(c.x_m - coin.x_m) <= params.attribution_half_m
                       and (c.t_meet_s is None
                            or need <= threat_t(c) <= coin.t_miss_s)
                       for c in cars):
                    continue
                coin_value += val
                n_reach += 1
                coin_ts.append(coin.t_miss_s)
            else:
                coin_value += params.far_coin_weight * val   # 远币：方向信息仍在
                n_far += 1
        # 奖励车项（R5 层 1，设计稿 §9.1）：与币同门同式——横向归属半容差
        # （每源 tol_m，默认 params.bonus_tol_m）、
        # 可达门 need+τ+margin < t_miss ≤ 前瞻窗、先到的车遮挡；闸关
        # （bonus_value=0 或 bonuses 空）恒 0=逐位回今天。撞上去是收益入口
        # 不是障碍（奖励车不在车流 views 里，碰撞判据面天然不含它）。
        # **窄容差平价**（20261017 实机 A/B 两轮教训）：平价 120 在宽容差内
        # 摊成平价平台，平手阈「差<20 选离中心近者」把胜出位拉回路心（宽
        # 容差 3.4 实测 11 候选平台、cr_best=0.17）；就近衰减又把峰值降到
        # 108，90 分金币候选进平手集同样翻面。窄容差（0.75m）平台只剩
        # 2~3 候选：胜出位落在源 ±0.5m 内，且 120−90=30 > 20 峰值真胜出。
        bonus_sum = 0.0
        n_bonus = 0
        if params.bonus_value > 0.0:
            for b in bonuses:
                tol = b.tol_m if b.tol_m is not None else params.bonus_tol_m
                if abs(b.x_m - x) >= tol:
                    continue                    # 横向不落归属半容差：不是这条路径的奖励车
                if not (need + params.tau_s + params.margin_s < b.t_miss_s
                        <= params.horizon_s):
                    continue                    # 来不及/窗外：不追（不计入也不降权）
                if any(abs(c.x_m - b.x_m) <= tol
                       and (c.t_meet_s is None
                            or need <= threat_t(c) <= b.t_miss_s)
                       for c in cars):
                    continue                    # 遮挡：先到的车挡住（赶到前已过的车不挡）
                bonus_sum += params.bonus_value
                n_bonus += 1
        # 错身/贴身/前瞻外：R3 贴身猎车按仪器**互斥分支**（2026-10-16 设计稿
        # §2.2，同一辆车不双计）——米制锚前车（t_overtake 非 None）走贴身 E 项，
        # 其余车（像素仪器）走旧错身项逐位不变。占用/遮挡/碰撞判据面不动
        # （car_in_path 照旧含贴身窗前车：窗在归属半宽内，crash 判据照旧护
        # 第三方接近车）。R2 前瞻外方向权（§2.4）作用于近场 horizon 外、
        # hunt_horizon 内的米制锚前车——近场是 R3 的地盘，按 t_overtake 区间
        # 互斥不双计。
        pass_sum = 0.0
        hug_sum = 0.0
        n_hug = 0
        far_es: list[float] = []
        for c in cars:
            d = abs(c.x_m - x)
            if far_on and c.t_overtake_s is not None \
                    and params.horizon_s < c.t_overtake_s <= params.hunt_horizon_s:
                # R2：远车预选未来通过侧——E 复用贴身曲线（机制同源），
                # 正对远车（d̂=0，硬地板下）为 0、贴身窗最大
                far_es.append(_hug_e(d, params))
            if hug_on:
                # R5 层 2（设计稿 §9.2）：闸 hug_visual 开时，t_overtake 缺位
                # （米制锚未成形）的车回退 t_meet_s（像素仪器）过同一可达门与
                # E 曲线——擦边倾向不再依赖米制锚覆盖；闸关=逐位回今天
                t_pass = c.t_overtake_s
                if t_pass is None and params.hug_visual:
                    t_pass = c.t_meet_s
                if t_pass is not None:
                    if t_pass > params.horizon_s:
                        continue                    # 窗外：无价值（旧错身同判）
                    # 可达性唯一门（与币门同式同真源）：过近/来不及即自然失败，
                    # 不再另设 minActionTime 门。桥接车 t_meet_s 不确定度走
                    # 裕度放宽（margin_add_s，闸开才可能非 None）
                    margin = params.margin_s
                    if c.bridged and params.bridge_margin_add_s is not None:
                        margin += params.bridge_margin_add_s
                    if not (need + params.tau_s + margin <= t_pass):
                        continue
                    e = _hug_e(d, params)
                    if e > 0.0:
                        hug_sum += e
                        n_hug += 1
                    continue                        # d̂<下沿：硬地板 0（不进旧错身项）
            if not (params.attribution_half_m < d
                    <= params.attribution_half_m + params.pass_band_m):
                continue
            if c.t_meet_s is not None \
                    and need <= c.t_meet_s <= params.horizon_s:
                pass_sum += params.pass_value
        # R2 多车聚合（三语义，影子对比后定档）：max / sum / sum 封顶
        # （上限=value_limit+pass_value——单次全价值动作的规模，防多车求和
        # 吹爆候选分）
        far_value = 0.0
        if far_es:
            if params.far_aggregation == "max":
                far_value = max(far_es)
            elif params.far_aggregation == "sum":
                far_value = sum(far_es)
            else:
                far_value = min(sum(far_es),
                                params.hug_value_limit + params.pass_value)
            far_value *= params.far_car_weight
        out.append(CandidateScore(
            x, coin_value + pass_sum + hug_sum + far_value + bonus_sum
            - params.shift_cost_per_m * abs(x - executed_x),
            False, coin_value, pass_sum,
            params.shift_cost_per_m * abs(x - executed_x), n_reach, n_far,
            tuple(coin_ts), hug_sum, n_hug, far_value, bonus_sum, n_bonus))

    # ---- 胜出：argmax；平手（差 < tie_margin）选离走廊中心近者（六点 1） ----
    best = max(out, key=lambda c: (c.score, -abs(c.x_m - center)))
    tied = [c for c in out if c.score >= best.score - params.tie_margin]
    best = min(tied, key=lambda c: abs(c.x_m - center))

    path = _path_preview(bands, executed_x, best.x_m)
    return ReachReport(tuple(out), best.x_m, center, path, best.coin_t)


def _path_preview(bands: Sequence[CorridorBandSpan], x_from: float, x_to: float,
                  ) -> tuple[tuple[float, float], ...]:
    """路径序列（设计稿 §3.3）：近端=自车当前 x，逐带线性插值到胜出位，
    远端=胜出位。走廊带不可读（弯道帧）时调用方以朝向记忆带喂入（§3.3，
    本函数对帧来源无感知）。"""
    if not bands:
        return ((0.0, x_from), (0.0, x_to))
    z_near = bands[0].z0
    z_far = bands[-1].z1
    span = z_far - z_near
    pts: list[tuple[float, float]] = [(z_near, x_from)]
    for b in bands:
        w = 0.0 if span <= 0 else (b.z1 - z_near) / span
        pts.append((b.z1, x_from + (x_to - x_from) * min(1.0, max(0.0, w))))
    return tuple(pts)
