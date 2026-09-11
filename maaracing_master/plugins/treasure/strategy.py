# -*- coding: utf-8 -*-
"""巅峰鉴宝 · 出价策略模块 V3（2026-09-05 秒杀规则重构）。

设计文档：docs/treasure_tick_dynamic_step_report.md + 多轮策略讨论 + 401 场复盘。
V3 核心变更（用户推导 + treasure.db 规则验证）：
  - 收入铁律：玩家收入 = 拍中者(总值-成交价)；未拍中者仅当赢家亏钱时按顺位
    吃 15%/10%/5% 分红——「卡第二」唯一合法前提 = 第一名必亏。
  - 成交铁律：当回合第一名/第二名 ≥ K_r 即秒杀成交（K=2.0/1.6/1.3/1.1/1.0）。
  - 火力基准 M：出价可回放，对手「历史最高报价」= 已证明支付意愿下限。
    预测基准从「上轮 opp_max」改为 M——V2 在实测被钓鱼出价骗
    （748,900→500,300→末轮 766,810 秒杀），错失利润线内的确定性反杀。
  - 单一决策树：P_win=ceil(K_r×(M+缓冲)) ≤ 买入线 → win 反杀拍中；
    否则 → 卡第二吃分红彩票（出价不花钱，赢家亏钱才有分红）。
    「捡漏」不再是独立分支——M 低时 P_win 自然低，低价拍中即捡漏。
  - 删除 willingness（对手降价收缩缓冲）模型：它专为「上轮=火力」的错误
    基准打补丁，新基准 M 天然免疫钓鱼降价。
V2 历史（2026-08-16 数据驱动重构）：
  - 确定性超价 u 与预测缓冲 D 分离：u=1（最小货币单位），D 基于数据分布
  - 双层缓冲：基础缓冲（查价格桶） × 利润强度缩放（0.5~1.5）
  - 全局兜底上限 GLOBAL_CAP（GUI 可调，默认 5 万）
  - 赚钱/赚蛋策略模式切换（蛋模式放宽利润线）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# ----------------------------------------------------------------------
# 参数（可调，见 docs/treasure_tick_feedback_report_20260816.md）
# ----------------------------------------------------------------------
VAL_COEF: float = 1.28          # 真实估值系数 = V̂ / max(H)（实测 median=1.265）
PROFIT_FLOOR: float = 0.10      # 赚钱模式捡漏利润线：成交价 ≤ (1-FLOOR) × V̂
u: int = 1                       # 游戏最小货币单位（实测 gcd=1）
GLOBAL_CAP: int = 50000          # 全局兜底上限（GUI 可调：每局最多接受亏多少）
STRATEGY_MODE: str = "profit"    # profit=赚钱, egg=赚蛋
BALANCE_UNKNOWN: int = -1        # 余额哨兵：OCR 未读到（未知）时传入，策略兜底视为充足

# 预测缓冲分桶（跨回合 Δopp 口径，向全局 p50=35000 收缩后值）
# 每桶格式: (base_low, base_high, buffer_p50)
BUFFER_BUCKETS: tuple = (
    (0,        110000,  49000),   # 便宜局
    (110000,   145000,  33000),   # 中等
    (145000,   175000,  27000),   # 中等偏高
    (175000,   220000,  39000),   # 偏高
    (220000,   float('inf'), 33000),  # 贵局
)
BUFFER_FALLBACK: int = 35000      # 全局 p50 fallback

# 争第二缓冲（口径A，更保守，防止 overshoot 变第一）
BUFFER_SECOND_BUCKETS: tuple = (
    (0,        110000,  32000),
    (110000,   145000,  31000),
    (145000,   175000,  32000),
    (175000,   220000,  29000),
    (220000,   float('inf'), 39000),
)
BUFFER_SECOND_FALLBACK: int = 25000

# 缩放系数范围
SCALE_MIN: float = 0.5
SCALE_MAX: float = 1.5
SCALE_REF: float = 0.15    # 基准利润率 15%

# 回合阈值 K_r
K_RATIOS: tuple = (2.0, 1.6, 1.3, 1.1, 1.0)

# 决策类型
DECISION_OBSERVE = "observe"
DECISION_NORMAL = "normal"
DECISION_WIN = "win"
DECISION_TARGET_SECOND = "target_second"
DECISION_LURE = "lure"
DECISION_PASS = "pass"       # 主动放弃，不出价

STRATEGY_LABEL: str = "V3 秒杀火力基准（赚钱/赚蛋）"


# ----------------------------------------------------------------------
# 数据结构（保持与 v0.3.5 兼容）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RoundSnapshot:
    """上一轮完整公开快照（策略唯一对手信息源）。"""
    epoch: int
    round_no: int
    h: int
    our_bid: int
    opponent_bids: tuple[int, int, int]
    opponent_ids: tuple[int, int, int]

    def bid_of(self, player_id: int) -> int:
        return self.opponent_bids[self.opponent_ids.index(player_id)]

    def is_complete(self) -> bool:
        if not (self.h > 0 and self.our_bid > 0):
            return False
        # 0 = 对手掉线/没出价（视为有效最低价，可参与捡漏/赚蛋）；
        # 仅 -1 = 未读到才算信息缺失。构建快照时已保证 4 槽全部 locked，不会出现 -1。
        return all(b >= 0 for b in self.opponent_bids)


@dataclass(frozen=True)
class LureState:
    opponent_id: int
    previous_bid: int


@dataclass(frozen=True)
class BidContext:
    round_no: int
    h_seen: tuple[int, ...]
    last_round: Optional[RoundSnapshot]
    balance: int
    our_last_bid: Optional[int] = None
    # 已完成回合逐轮的「对手最高出价」历史，按回合升序。
    # V3 用途：与上一轮快照共同构造对手已证明火力 M = max(历史各轮最高, 上轮对手最高)。
    opp_high_history: tuple[int, ...] = ()


@dataclass(frozen=True)
class BidDecision:
    price: int
    decision: str
    vhat: float
    max_win_bid: Optional[int]
    opponent_max: Optional[int]
    trigger_bid: Optional[int]
    reason: str
    # V2 新增
    buffer_used: Optional[int] = None
    scale_used: Optional[float] = None


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------
def lookup_buffer(base: int, buckets: tuple, fallback: int) -> int:
    """查价格桶取缓冲值，超出范围用 fallback。"""
    for lo, hi, val in buckets:
        if lo <= base < hi:
            return val
    return fallback


def ceil_u(x: int, unit: int = u) -> int:
    """向上取整到最小单位 u。"""
    return int(math.ceil(x / unit)) * unit


def predict_second_quantile(opp_max: int, opp_second: int,
                            round_no: int, mode: str) -> int:
    """预测本轮第二名出价（基于上一轮信息）。

    密封机制下当回合对手报价未知，预测基准：
    - 有上轮完整快照 → 用 opp_max（原第一）作为本轮第二名的预测基准
    - 在 win 场景下，需要超越的是原第一（他可能维持或加价）
    - 在 second 场景下，需要防的是原第三/第二
    """
    # 简单预测：对手平均加价约 3.5 万，用 opp_max + fallback 作为预测
    return opp_max + BUFFER_FALLBACK


# ----------------------------------------------------------------------
# 决策器
# ----------------------------------------------------------------------
class BidStrategy:
    """出价策略决策器 V2。

    参数（可由外部传入覆盖）：
      - risk_cap: 全局兜底上限（意外接盘的最大可接受亏损）
      - mode: "profit"（赚钱）/ "egg"（赚蛋）
    """

    def __init__(self, risk_cap: int = GLOBAL_CAP, mode: str = STRATEGY_MODE) -> None:
        self.VAL_COEF: float = VAL_COEF
        self.PROFIT_FLOOR: float = PROFIT_FLOOR
        self.u: int = u
        self.risk_cap: int = risk_cap
        self.mode: str = mode
        self._lure_state: Optional[LureState] = None
        self.TICK: int = BUFFER_FALLBACK  # 兼容旧接口引用

    # ---------- 内部工具 ----------

    def _vhat(self, h_seen: tuple[int, ...]) -> float:
        """宝贝估值 = max(H) × 估值系数。"""
        return self.VAL_COEF * max(h_seen) if h_seen else 0.0

    def _profit_floor(self) -> float:
        """当前模式的利润线比例（仅赚钱模式使用；赚蛋模式无利润线概念）。"""
        return self.PROFIT_FLOOR

    def _max_win_bid(self, vhat: float) -> Optional[int]:
        """赚钱模式捡漏利润上限：成交价 ≤ (1-利润线) × V̂。"""
        if vhat <= 0:
            return None
        return int(math.floor(vhat * (1 - self._profit_floor())))

    def _egg_buy_cap(self, vhat: float) -> Optional[int]:
        """赚蛋模式买入上限：最多亏 risk_cap → 成交价 ≤ V̂ + risk_cap。"""
        if vhat <= 0:
            return None
        return int(math.floor(vhat)) + self.risk_cap

    def _predict_buffer(self, base: int, for_second: bool = False) -> int:
        """第一层：查价格桶得基础缓冲值。"""
        buckets = BUFFER_SECOND_BUCKETS if for_second else BUFFER_BUCKETS
        fallback = BUFFER_SECOND_FALLBACK if for_second else BUFFER_FALLBACK
        return lookup_buffer(base, buckets, fallback)

    def _scale_by_intensity(self, vhat: float, opp_max: int, is_second: bool) -> float:
        """第二层：按这局能赚多少缩放 0.5~1.5 倍。

        捡漏（is_second=False）：
          强度 = (V̂ - 预计成交价) / V̂
        卡第二（is_second=True）：
          强度 = (opp_max - V̂) × 15% / V̂
        """
        if vhat <= 0:
            return 1.0
        if is_second:
            # 分红强度 = 第一名亏损额 × 15% ÷ 宝贝价值
            loss = max(0, opp_max - vhat)
            intensity = (loss * 0.15) / vhat
        else:
            # 捡漏强度 = 利润 ÷ 宝贝价值
            est_cost = opp_max + BUFFER_FALLBACK
            profit = max(0, vhat - est_cost)
            intensity = profit / vhat if vhat > 0 else 0.0
        scale = intensity / SCALE_REF
        return max(SCALE_MIN, min(SCALE_MAX, scale))

    def _buffer(self, base: int, vhat: float, opp_max: int,
                is_second: bool = False) -> int:
        """双层缓冲：基础缓冲 × 强度缩放（V3 起不再乘意愿系数——基准已是火力峰值）。"""
        base_buf = self._predict_buffer(base, for_second=is_second)
        scale = self._scale_by_intensity(vhat, opp_max, is_second=is_second)
        return int(round(base_buf * scale))

    def _global_cap(self, vhat: float) -> int:
        """全局兜底上限 = 估值 + 可接受亏损（V̂ + risk_cap）。

        用户直觉（2026-08-18）："V̂ 十几万、能接受亏 5 万 → 最高就应出二十几万"。
        原来写成 max(risk_cap, V̂×0.15)，只取到 low 的 5 万，把卡第二 upper 钳死在 5 万，
        对手价一高区间就走空弃权（见 log 20260818_000240 R3）。V̂+risk_cap 才是
        "最多出到估值、再最多亏 risk_cap"的正确预算。risk_cap 本身不变。"""
        if vhat <= 0:
            return self.risk_cap
        return int(math.floor(vhat)) + self.risk_cap

    # ---------- 主决策 ----------

    def decide(self, ctx: BidContext) -> BidDecision:
        r = ctx.round_no
        # 余额语义：-1（BALANCE_UNKNOWN）= OCR 未读到（未知，兜底视为充足，只受兜底 cap 约束）；
        # 0 = 真实没钱（所有出价被限制）；>0 = 正常。
        if ctx.balance == BALANCE_UNKNOWN:
            balance = 2_000_000_000      # 未知余额 → 视为充足（避免 max(...,1)=1 误伤出价）
        else:
            balance = max(ctx.balance, 0)
        vhat = self._vhat(ctx.h_seen)

        last = ctx.last_round
        opp: list[int] = []
        if last is not None and last.is_complete():
            opp = sorted(last.opponent_bids, reverse=True)

        # ---------- R1/R2：观察（不出价，只记录） ----------
        # 出 min(H,余额) 的深层含义：H ≤ max(H) < 0.9×V̂ 恒成立，observe 价天然在
        # 利润线内——R1/R2 秒杀线极高（甩开 2.0/1.6 倍才成交），偶尔撞上即是低价拍中。
        if r <= 2:
            price = min(ctx.h_seen[-1], balance) if ctx.h_seen else 1
            return BidDecision(
                price=price, decision=DECISION_OBSERVE, vhat=vhat,
                max_win_bid=None, opponent_max=opp[0] if opp else None,
                trigger_bid=None,
                reason=f"R{r} observe: 出 min(H,余额)={price}（观察对手加价意愿）",
            )

        # ---------- 对手已证明火力 M（V3 核心，401 场复盘定稿） ----------
        # M = max(已完成各回合对手最高价历史, 上一轮快照对手最高)。出价可回放：
        # 对手中途降价（钓鱼蓄力）随时能回到历史峰值，V2 用「上轮价」当基准实测被
        # 骗（748,900→500,300→末轮 766,810 秒杀成交），错失利润线内的确定性反杀。
        m_power = max(opp) if opp else 0
        for h in ctx.opp_high_history:
            if h > m_power:
                m_power = h

        # 无任何对手有效信息（无快照/对手全 0）→ 不出击价：出 min(H,余额) 等低价
        # 捡漏（H 价必在利润线内），不再走"嘲讽 250"（无分红顺位意义，已删除）。
        if m_power <= 0:
            price = min(ctx.h_seen[-1], balance) if ctx.h_seen else 1
            return BidDecision(
                price=price, decision=DECISION_OBSERVE, vhat=vhat,
                max_win_bid=None, opponent_max=0, trigger_bid=None,
                reason=f"R{r} 无对手火力信息: 出 min(H,余额)={price} 等捡漏",
            )

        kr = K_RATIOS[r - 1] if 1 <= r <= 5 else 1.0
        # ---------- 秒杀判定（收入铁律：钱只在第一名和亏钱第一名的分红里） ----------
        # 买入线：profit=捡漏利润线 0.9×V̂；egg=V̂+risk_cap（搏蛋放宽到可接受亏损）。
        # 杀价 P_win = ceil(K_r × (M + 缓冲))：当第一名并把第二名（≤M+缓冲）甩开
        # K_r 倍即当回合成交。M 低时杀价自然低——"捡漏"就是本分支的特例，不再独立。
        line = self._max_win_bid(vhat) if self.mode == "profit" else self._egg_buy_cap(vhat)
        buf = self._buffer(m_power, vhat, m_power, is_second=False)
        p_win = int(math.ceil(kr * (m_power + buf)))

        # ① 杀价在买入线与余额内 → 反杀拍中（绝不裁剪后买入——2026-08-16 教训：
        #    裁剪价买入=赌一把接盘；超线宁可转②，未拍中出价本身不花钱）。
        if line is not None and p_win <= line and p_win <= balance:
            return BidDecision(
                price=p_win, decision=DECISION_WIN, vhat=vhat,
                max_win_bid=line, opponent_max=m_power,
                trigger_bid=p_win,
                buffer_used=buf, scale_used=self._scale_by_intensity(vhat, m_power, False),
                reason=f"R{r} win(秒杀): ceil({kr}×({m_power}+{buf}))={p_win} ≤ 买入线={line}",
            )

        # ② 杀不动（对手火力已逼近/超过买入线）→ 卡第二吃分红彩票。
        #    分红唯一来源=赢家亏钱（第一名亏→第2名15%/第3名10%/第4名5%）；
        #    赢家盈利则未拍中者收入为 0，但出价不成交就不花钱——卡第二是免费彩票。
        #    upper 用 M 卡安全垫：对手约 30% 概率退出/降价，防止意外当第一接盘。
        return self._try_second(
            r, m_power,
            opp[1] if len(opp) >= 2 else 0,
            opp[2] if len(opp) >= 3 else 0, vhat, self._global_cap(vhat), balance, ctx, opp
        )

    # ---------- 卡第二吃分红 ----------

    def _try_second(self, r, opp_max, opp_second, opp_third, vhat,
                    cap, balance, ctx, opp) -> BidDecision:
        """卡第二：双尾预测（V3 起由 decide 分支②调用，opp_max=对手已证明火力 M）。

        - lower = 第三名 + 缓冲（防被第三超）
        - upper = min(M − u, 兜底上限 cap, 余额)（防对手退出/降价把我方顶成
          第一名意外接盘——对手退出率≈30% 是实测数据，安全垫刚需）
        - lower > upper → PASS
        """
        # 竞争者：要压过的是第三名（如果存在）或第二名
        competitor = opp_third if opp_third > 0 else opp_second
        if competitor <= 0:
            return self._make_pass(vhat, opp_max, f"R{r} 无竞争者可压，pass")

        buf = self._buffer(competitor, vhat, opp_max, is_second=True)
        lower = competitor + buf + self.u

        # 安全上限：严格低于第一名，且不超兜底，且不超余额
        upper = min(opp_max - self.u, cap, balance)

        if lower >= upper:
            # 尝试用 lower = competitor + u 挤一挤
            tight = competitor + self.u
            if tight < upper:
                return BidDecision(
                    price=tight, decision=DECISION_TARGET_SECOND, vhat=vhat,
                    max_win_bid=None, opponent_max=opp_max,
                    trigger_bid=None, buffer_used=0, scale_used=0,
                    reason=f"R{r} target_second(紧贴): {competitor}+{self.u}={tight} < {upper}（缓冲挤不下，用紧贴价）",
                )
            return self._make_pass(vhat, opp_max,
                                   f"R{r} 争第二区间空: lower={lower} >= upper={upper}，pass")

        return BidDecision(
            price=lower, decision=DECISION_TARGET_SECOND, vhat=vhat,
            max_win_bid=None, opponent_max=opp_max,
            trigger_bid=None, buffer_used=buf, scale_used=self._scale_by_intensity(vhat, opp_max, True),
            reason=f"R{r} target_second: {competitor}+{buf}+{self.u}={lower} < {upper}（cap={cap}, balance={balance}）",
        )

    # ---------- 辅助 ----------

    def _make_pass(self, vhat, opp_max, reason: str) -> BidDecision:
        return BidDecision(
            price=0, decision=DECISION_PASS, vhat=vhat,
            max_win_bid=None, opponent_max=opp_max,
            trigger_bid=None, reason=reason,
        )

    # ---------- 兼容旧接口 ----------
    def _pick_lure_target(self, snapshot: RoundSnapshot) -> Optional[LureState]:
        """V2 保留 lure 接口，但建议不主动用（用户接受小亏反噬时启用）。"""
        if snapshot is None or not snapshot.is_complete():
            return None
        opp = snapshot.opponent_bids
        leader_idx = opp.index(max(opp))
        leader_id = snapshot.opponent_ids[leader_idx]
        leader_bid = opp[leader_idx]
        # 仅当 leader 出价明显超估值时才 lure
        h = snapshot.h
        if h > 0 and leader_bid / h > 1.5:
            return LureState(opponent_id=leader_id, previous_bid=leader_bid)
        return None