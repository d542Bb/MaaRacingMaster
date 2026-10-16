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


def evaluate(bands: Sequence[CorridorBandSpan],
             coins: Sequence[CoinSource],
             cars: Sequence[CarSource],
             executed_x: float,
             params: ReachParams) -> ReachReport | None:
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

    # R3 贴身猎车总开关（参数全缺=None=逐位回今天；分开关语义在 config）
    hug_on = params.hug_d_lo_m is not None

    # ---- 车三分预备（事件语义：每车记一次） ----
    # 本道（归属半宽内）的车：碰撞/遮挡判据面；先于币到达的车把币挡住
    def car_in_path(x: float) -> list[float | None]:
        return [c.t_meet_s for c in cars
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
                # 不挡、pacing 车 t=None 整窗占用——与退役前 lane_score 同判）
                if any(abs(c.x_m - coin.x_m) <= params.attribution_half_m
                       and (c.t_meet_s is None
                            or need <= c.t_meet_s <= coin.t_miss_s)
                       for c in cars):
                    continue
                coin_value += val
                n_reach += 1
                coin_ts.append(coin.t_miss_s)
            else:
                coin_value += params.far_coin_weight * val   # 远币：方向信息仍在
                n_far += 1
        # 错身/贴身：R3 贴身猎车按仪器**互斥分支**（2026-10-16 设计稿 §2.2，
        # 同一辆车不双计）——米制锚前车（t_overtake 非 None）走贴身 E 项，
        # 其余车（像素仪器）走旧错身项逐位不变。占用/遮挡/碰撞判据面不动
        # （car_in_path 照旧含贴身窗前车：窗在归属半宽内，crash 判据照旧护
        # 第三方接近车）。
        pass_sum = 0.0
        hug_sum = 0.0
        n_hug = 0
        for c in cars:
            d = abs(c.x_m - x)
            if hug_on and c.t_overtake_s is not None:
                if c.t_overtake_s > params.horizon_s:
                    continue                    # 窗外：无价值（旧错身同判）
                # 可达性唯一门（与币门同式同真源）：过近/来不及即自然失败，
                # 不再另设 minActionTime 门
                if not (need + params.tau_s + params.margin_s
                        <= c.t_overtake_s):
                    continue
                if d < params.hug_d_lo_m:
                    continue                    # 硬地板：同车道贴尾不是错身
                if d <= params.hug_d_hi_m:
                    # 平台常数：P 在窗中心取值（真源推导）；窗内各候选同分，
                    # 选中位由位移代价与平局规则决定（有意语义：减少无谓横移）
                    hug_sum += params.pass_value + params.hug_value_limit * _hug_p(
                        (params.hug_d_lo_m + params.hug_d_hi_m) / 2.0, params)
                else:
                    # 窗上沿外：logistic 连续衰减（上沿处 ≈105，约 27 分出窗
                    # 跳变——与下沿 0→132 并列为待测边界行为）
                    hug_sum += params.pass_value + params.hug_value_limit * _hug_p(
                        d, params)
                n_hug += 1
                continue
            if not (params.attribution_half_m < d
                    <= params.attribution_half_m + params.pass_band_m):
                continue
            if c.t_meet_s is not None \
                    and need <= c.t_meet_s <= params.horizon_s:
                pass_sum += params.pass_value
        out.append(CandidateScore(
            x, coin_value + pass_sum + hug_sum
            - params.shift_cost_per_m * abs(x - executed_x),
            False, coin_value, pass_sum,
            params.shift_cost_per_m * abs(x - executed_x), n_reach, n_far,
            tuple(coin_ts), hug_sum, n_hug))

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
