# -*- coding: utf-8 -*-
"""BidStrategy V2/V3/V4 出价策略单元测试（pytest 断言版）。

替代原 maaracing_master/modules/test_bid_strategy.py 的纯打印冒烟脚本：
原脚本只有 print 无断言，无法作为 CI 通过/失败判定；本测试以真实运行结果为基线，
把决策类型与出价锁定为回归断言，防止后续改动悄悄破坏策略行为。

V4（2026-09-15）改动：卡第二分支不再「紧贴第三名」，改为**出到买入线**；
「全局兜底上限」（原 GLOBAL_CAP / GUI「每局最多接受亏多少」）随之下线
（买入线恒严于它，旋钮在出价决策上已无作用）。
相关用例的期望值已随之更新，理由见 strategy.py 模块头 V4 段与
docs/plan/bid_audit_20260915/出价审计报告_20260915.md。
"""

from __future__ import annotations

from strategy import (
    BidContext,
    BidDecision,
    BidStrategy,
    RoundSnapshot,
    DECISION_OBSERVE,
    DECISION_WIN,
    DECISION_TARGET_SECOND,
    DECISION_PASS,
    BALANCE_UNKNOWN,
)


def snap(round_no, h, our, opp_bids, epoch=1, my_rank=4):
    """构造快照：my_rank=4 表示我方在槽4，对手槽1~3。"""
    return RoundSnapshot(
        epoch=epoch,
        round_no=round_no,
        h=h,
        our_bid=our,
        opponent_bids=tuple(opp_bids),
        opponent_ids=(1, 2, 3),
    )


def ctx(round_no, h_seen, last, balance, our_last=None):
    return BidContext(
        round_no=round_no,
        h_seen=tuple(h_seen),
        last_round=last,
        balance=balance,
        our_last_bid=our_last,
    )


def assert_decision(name, dec: BidDecision, expect_decision: str, expect_price: int):
    assert dec.decision == expect_decision, (
        f"{name}: 期望 decision={expect_decision}，实际={dec.decision}（{dec.reason}）"
    )
    assert dec.price == expect_price, (
        f"{name}: 期望 price={expect_price}，实际={dec.price}（{dec.reason}）"
    )


def line_of(h_max: int) -> int:
    """买入线 = floor(0.9 × 1.28 × max(H))。"""
    return int(0.9 * 1.28 * h_max)


# ----------------------------------------------------------------------
# R1/R2 观察
# ----------------------------------------------------------------------
def test_r1_observe():
    dec = BidStrategy().decide(ctx(1, (30000,), None, 1000000))
    assert_decision("R1 observe", dec, DECISION_OBSERVE, 30000)


def test_r2_observe():
    dec = BidStrategy().decide(ctx(2, (30000, 40000), None, 1000000))
    assert_decision("R2 observe", dec, DECISION_OBSERVE, 40000)


def test_r1_balance_short():
    # H>余额 → observe 出 min(H,余额)=100000
    dec = BidStrategy().decide(ctx(1, (500000,), None, 100000))
    assert_decision("R1 H>余额", dec, DECISION_OBSERVE, 100000)


# ----------------------------------------------------------------------
# 卡第二 = 出到买入线（V4 口径）
# ----------------------------------------------------------------------
def test_r4_cool_pick_bargain_becomes_second():
    # H=(30000,35000,40000) → 买入线 46080；捡漏失败转卡第二，出到线
    dec = BidStrategy().decide(
        ctx(4, (30000, 35000, 40000),
            snap(3, 40000, 40000, (20000, 30000, 40000)), 1000000)
    )
    assert_decision("R4 冷静局捡漏转卡第二", dec, DECISION_TARGET_SECOND, 46080)
    assert dec.price == line_of(40000)
    assert dec.max_win_bid == line_of(40000), "卡第二也应回报买入线，供上层审计"


def test_r4_firefight_clamp_second():
    # 有人烧钱(80000>51200) → 卡第二，出到买入线（V3 为紧贴 48001）
    dec = BidStrategy().decide(
        ctx(4, (30000, 35000, 40000),
            snap(3, 40000, 30000, (30000, 50000, 80000)), 1000000)
    )
    assert_decision("R4 有人烧钱→卡第二(出到买入线)", dec, DECISION_TARGET_SECOND, 46080)


def test_r4_medium_firefight_second():
    dec = BidStrategy().decide(
        ctx(4, (30000, 35000, 40000),
            snap(3, 40000, 20000, (20000, 30000, 60000)), 1000000)
    )
    assert_decision("R4 烧钱中等→卡第二(出到买入线)", dec, DECISION_TARGET_SECOND, 46080)


def test_r4_slight_firefight_second():
    dec = BidStrategy().decide(
        ctx(4, (30000, 35000, 40000),
            snap(3, 40000, 20000, (20000, 30000, 52000)), 1000000)
    )
    assert_decision("R4 轻微烧钱→卡第二(出到买入线)", dec, DECISION_TARGET_SECOND, 46080)


def test_r5_snapshot_incomplete_observe():
    # 快照含 -1（信息缺失，未读到报价）→ is_complete=False → 无火力信息。
    # V3 语义：无对手信息不再 pass/嘲讽 250，出 min(H,余额) 等低价捡漏。
    # 注意：0 是掉线/没出价的有效报价，不再视为缺失（见 test_r5_disconnected_bidder_second）。
    dec = BidStrategy().decide(
        ctx(5, (30000, 35000, 40000),
            snap(4, 40000, 30000, (-1, 44000, 45000)), 1000000)
    )
    assert_decision("R5 快照不完整(-1)→observe 捡漏价", dec, DECISION_OBSERVE, 40000)


def test_r5_disconnected_bidder_second():
    # 对手槽读值 0（掉线/没出价）视为有效最低价，快照仍完整可参与捡漏/卡第二：
    # 对手报价 45000/44000/0 均无人烧钱 → 捡漏失败转卡第二，出到买入线
    dec = BidStrategy().decide(
        ctx(5, (30000, 35000, 40000),
            snap(4, 40000, 30000, (0, 44000, 45000)), 1000000)
    )
    assert_decision("R5 掉线玩家参与→卡第二(出到买入线)", dec, DECISION_TARGET_SECOND, 46080)


# ----------------------------------------------------------------------
# 真机 2026-09-14 事故回归：我方=0 连坐作废快照 → 误判「无竞争者可压」→ 0 出价卡死
# ----------------------------------------------------------------------
def test_r3_our_zero_snapshot_valid_not_pass():
    """我方=0（放弃/执行事故）不得作废快照——对手三位 >0 是真实公开信息。"""
    last = snap(2, 136600, 0, (87600, 131000, 92500), epoch=2)
    assert last.is_complete(), "我方=0 且对手位次 >0 的快照应判完整"
    dec = BidStrategy().decide(ctx(3, (111100, 136600), last, 200000))
    assert dec.decision != DECISION_PASS, f"我方=0 被误判为无竞争者: {dec.reason}"
    assert dec.price > 0, f"不得出 0 价: {dec.reason}"


def test_r3_missing_snapshot_history_fire_observes_not_pass():
    """上轮快照不完整且历史火力>0：卡第二缺位次依据 → 退回观察价，不把「没数据」当「对手全 0」。"""
    broken = snap(2, 136600, -1, (87600, 131000, 92500), epoch=2)
    assert not broken.is_complete()
    c = BidContext(round_no=3, h_seen=(111100, 136600), last_round=broken,
                   balance=200000, opp_high_history=(131000,))
    dec = BidStrategy().decide(c)
    assert_decision("缺快照+有历史火力→observe", dec, DECISION_OBSERVE, 136600)


def test_r5_three_high_tight_second():
    dec = BidStrategy().decide(
        ctx(5, (30000, 35000, 40000),
            snap(4, 40000, 30000, (41000, 42000, 43000)), 1000000)
    )
    assert_decision("R5 三高→卡第二(出到买入线)", dec, DECISION_TARGET_SECOND, 46080)


# ----------------------------------------------------------------------
# 预算/边界
# ----------------------------------------------------------------------
def test_r1_budget_short():
    dec = BidStrategy().decide(ctx(1, (500000,), None, 100000))
    assert_decision("R1 预算不足", dec, DECISION_OBSERVE, 100000)


def test_r4_no_snapshot_observe():
    # V3：无快照（完全无对手信息）→ observe 式 min(H,余额) 低价捡漏，不再 pass
    dec = BidStrategy().decide(ctx(4, (30000, 35000, 40000), None, 1000000))
    assert_decision("无快照 R4 → observe 捡漏价", dec, DECISION_OBSERVE, 40000)


# ----------------------------------------------------------------------
# 策略模式（赚蛋已删除，恒为赚钱；mode 参数从 BidStrategy 接口移除）
# ----------------------------------------------------------------------
def test_profit_hot_cool_becomes_second():
    # H max=200000 → V̂=256000，买入线 230400
    dec = BidStrategy().decide(
        ctx(4, (180000, 190000, 200000),
            snap(3, 200000, 150000, (120000, 150000, 180000)), 1000000)
    )
    assert_decision("R4 冷静高价→卡第二", dec, DECISION_TARGET_SECOND, 230400)


# ----------------------------------------------------------------------
# V3 钓鱼局反杀回归（treasure.db id=401 真实局面：P3 出价 582200→748900→
# 500300（钓鱼降价）→766810（末轮秒杀成交盈利 269k）。V2 用上轮价 500300 当
# 火力基准只出 566665 错失；V3 用对手已证明火力 M=748900 出 874779 反杀秒杀）
# ----------------------------------------------------------------------
def test_r4_phishing_bait_rekill_401():
    dec = BidStrategy().decide(BidContext(
        round_no=4,
        h_seen=(710600, 665000, 732400, 775900),
        last_round=RoundSnapshot(
            epoch=3, round_no=3, h=732400, our_bid=504401,
            opponent_bids=(391200, 500300, 252600), opponent_ids=(1, 3, 4),
        ),
        balance=BALANCE_UNKNOWN,
        our_last_bid=504401,
        opp_high_history=(582200, 748900, 500300),
    ))
    assert_decision("R4 钓鱼局 V3 反杀(401 回归)", dec, DECISION_WIN, 874779)
    # 874779 / 真实成交 766810 = 1.141 ≥ K4=1.1 → 当回合秒杀成立，利润确定性
    assert dec.max_win_bid == 893836  # 买入线 0.9×V̂ 未破，杀价在线内


def test_r3_phishing_crash_not_tricked():
    """R3 同一局面：杀价仍超线（M=748900 未放松），但 V4 卡第二改为出到买入线。

    旧口径（紧贴第三名）在此局面只出 116001，输给 R3 实际第二名 500300；
    新口径出到买入线 843724 ≥ 1.3 × 500300 = 650390 → 当回合即可反杀成交。
    即「不被钓鱼骗」的保护由买入线承担，不再由 M−u 承担。
    """
    dec = BidStrategy().decide(BidContext(
        round_no=3,
        h_seen=(710600, 665000, 732400),
        last_round=RoundSnapshot(
            epoch=2, round_no=2, h=665000, our_bid=665000,
            opponent_bids=(492700, 100000, 502200), opponent_ids=(1, 3, 4),
        ),
        balance=BALANCE_UNKNOWN,
        our_last_bid=665000,
        opp_high_history=(582200, 748900),
    ))
    assert_decision("R3 对手崩价不被骗→卡第二(出到买入线)", dec, DECISION_TARGET_SECOND, 843724)
    assert dec.price == line_of(732400)
    # 反杀条件成立：843724 / 500300 = 1.686 ≥ K3=1.3
    assert dec.price / 500300 >= 1.3


# ----------------------------------------------------------------------
# 余额三态（未知 -1 / 真实 0 / 正常）
# ----------------------------------------------------------------------
def test_balance_zero_pick_bargain_pass():
    # 真实余额 0 → 没钱，买入线被余额钳到 0 → pass
    dec = BidStrategy().decide(
        ctx(4, (180000, 190000, 200000),
            snap(3, 200000, 150000, (120000, 150000, 180000)), 0)
    )
    assert_decision("余额0 R4 捡漏→不出", dec, DECISION_PASS, 0)


def test_balance_zero_firefight_pass():
    dec = BidStrategy().decide(
        ctx(4, (30000, 35000, 40000),
            snap(3, 40000, 20000, (20000, 30000, 80000)), 0)
    )
    assert_decision("余额0 R4 烧钱→pass", dec, DECISION_PASS, 0)


def test_balance_unknown_pick_bargain_second():
    # 余额未知(-1) → 兜底视为充足，捡漏失败转卡第二
    dec = BidStrategy().decide(
        ctx(4, (180000, 190000, 200000),
            snap(3, 200000, 150000, (120000, 150000, 180000)), BALANCE_UNKNOWN)
    )
    assert_decision("余额未知 R4 捡漏→卡第二", dec, DECISION_TARGET_SECOND, 230400)


def test_balance_unknown_firefight_second():
    dec = BidStrategy().decide(
        ctx(4, (30000, 35000, 40000),
            snap(3, 40000, 20000, (20000, 30000, 80000)), BALANCE_UNKNOWN)
    )
    assert_decision("余额未知 R4 烧钱→卡第二", dec, DECISION_TARGET_SECOND, 46080)


def test_balance_below_line_clamped():
    """余额低于买入线 → 出价被钳到余额，而不是买入线。"""
    dec = BidStrategy().decide(
        ctx(4, (180000, 190000, 200000),
            snap(3, 200000, 150000, (120000, 150000, 180000)), 100000)
    )
    assert_decision("余额 10 万 < 买入线 23.04 万 → 钳到余额", dec, DECISION_TARGET_SECOND, 100000)
