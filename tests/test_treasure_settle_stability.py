# -*- coding: utf-8 -*-
"""结算字段连续一致确认闸（P1-8）：滚动动画期的瞬时读数不得驱动决策。

真机背景（2026-09-16 会话 20260916_215905，领取分红）：结算页数字从进入就在
滚动（`settle_total_price` 15,299→321,647，`settle_profit` −233,832→+87,080），
而 `settle_my_income` 被读成 `0`。既有过滤只拦「正数且 |amt|<10」（裁位残缺），
**0 是合法终值**（本场未分红）因此被放行 → 策略侧 `settle_income >= 0`
立刻命中 `settle_ready_click`「真领取」，动画还没播完就点了第二次领取，
「跳过动画」这一步形同虚设。

判据修正（本文件锁）：`0` 是不是合法终值，不能只看值域——要看**这个值稳没稳**。
故与报价槽同口径引入「连续 N 次读取一致才固化」（`SETTLE_STABLE_FRAMES`）：
逐帧变的值永远凑不满连续一致，动画停住后才固化。

2026-09-17 追加（同一件事的第二半）：动画值**能一连稳住 ≥3 帧**（实测会话
20260917_210330 第 15 场：合计 31,013→31,361→320,605、利润 −445,987→−445,639
→−156,395，各值都稳定 ≥3 帧），所以「连续一致」只是去抖，不是定值判据。
定值判据 = **跳过动画后「领取」按钮出现**（用户权威 + 日志双证），代码里即
`_settle_collect_clicked_once`（策略两次点击的交接点）：点击前一律不固化。
本文件同时锁住由此配套的两条：利润改为 `拍品总价 − 最终竞拍价` 派生（不再独立
OCR，故薄利赢单的小额利润不会被「Hmax/20」护栏误杀）、以及策略侧
`settle_ready_click` 对**负收入**（我方亏本拍中）也必须放行。
"""
from __future__ import annotations

import pytest

try:
    from maaracing_master.plugins.treasure.module import TreasureModule
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"测试需要完整运行时依赖（maa/…）：{_ERR}"
)


class _Self:
    """`_settle_field_stable` 的最小状态桩：只要那一个累计容器。"""

    SETTLE_STABLE_FRAMES = TreasureModule.SETTLE_STABLE_FRAMES

    def __init__(self):
        self._settle_stable: dict[str, dict] = {}


def _feed(values, field: str = "settle_my_income"):
    """按顺序喂一串读数，返回每一步的固化判定。"""
    fake = _Self()
    return [TreasureModule._settle_field_stable(fake, field, v) for v in values]


# ------------------------------------------------------------------
#  滚动动画：逐帧变值 → 永不固化
# ------------------------------------------------------------------


def test_rolling_values_never_lock():
    """动画期逐帧滚动的值（含 0 的瞬时读数）一律不得固化。

    这正是旧口径的漏洞：读到 0 就当「本场收入已读出」，于是真领取提前触发。
    """
    assert _feed([0, 1_880, 0, 234_567, 0]) == [False] * 5
    # 每帧都变 → 永远不同值，无论多少帧都不固化
    assert _feed(list(range(100))) == [False] * 100


def test_zero_locks_only_after_agreement():
    """0 是合法终值：但必须连续一致 N 次才算读出（未分红 vs 动画未渲染区分开）。"""
    n = TreasureModule.SETTLE_STABLE_FRAMES
    got = _feed([0] * n)
    assert got == [False] * (n - 1) + [True], f"连续 {n} 次一致才该固化"


def test_stable_value_locks_without_extra_wait():
    """正常场景（读数本来就稳）：连续 N 次一致即固化，不额外多等。"""
    n = TreasureModule.SETTLE_STABLE_FRAMES
    assert _feed([234_567] * n)[-1] is True


def test_agreement_counter_resets_on_change():
    """中途换值 → 计数从头再来（旧的稳定累积不得被继承）。"""
    n = TreasureModule.SETTLE_STABLE_FRAMES
    got = _feed([234_567] * (n - 1) + [87_080] * n)
    assert got[: n - 1] == [False] * (n - 1)
    assert got[n - 1] is False, "换值当帧不算一致"
    assert got[-1] is True, "换值后需重新凑满连续一致"


def test_fields_are_tracked_independently():
    """各字段各自计数：一个字段的稳定不得带动另一个字段。"""
    fake = _Self()
    n = TreasureModule.SETTLE_STABLE_FRAMES
    for _ in range(n):
        TreasureModule._settle_field_stable(fake, "settle_final_price", 230_000)
    assert TreasureModule._settle_field_stable(fake, "settle_my_income", 1_880) is False
    assert TreasureModule._settle_field_stable(fake, "settle_final_price", 230_000) is True


# ------------------------------------------------------------------
#  为什么闸必须在这一层（策略侧只看数值，分不出"瞬时 0"与"真的 0"）
# ------------------------------------------------------------------


def test_policy_accepts_zero_income_so_gate_must_live_upstream():
    """策略侧判据是「收入**已读出**」而非值域 → 「瞬时 0」只能在上游拦截。

    锁住这条分工：策略层无时效概念（facts 里只有值），因此不得把「读数稳不稳」
    的判据搬到策略层去；它必须发生在 OCR 消费段（定值闸 + `_settle_field_stable`）。
    `neq: null` 与旧 `gte: 0` 对 0 一视同仁地接受（未分红也是「已读出」），
    差别在**负收入**：我方亏本拍中时收入为负（RULES §3 拍中者收入=利润，可负；
    库内 122 场），旧 `gte: 0` 会让这些场永远命中不了领取点击——定值闸把收入
    也收进「点击后才认」之后，这条就是死路（点击后读到的必然是负值）。
    """
    import pathlib
    import re
    policy = pathlib.Path("maaracing_master/plugins/treasure/resources/policy/"
                          "treasure.policy.json").read_text(encoding="utf-8")
    m = re.search(r'"id":\s*"settle_ready_click".*?"when":\s*\{(.*?)\}',
                  policy, re.S)
    assert m, "settle_ready_click 规则改名 → 本锁须同步更新"
    assert '"settle_income"' in m.group(1) and '"neq": null' in m.group(1), \
        "该规则判「收入已读出」——0 与负数都被接受，故上游必须有定值闸"
    assert '"gte": 0' not in m.group(1), \
        "值域判据会把「我方亏本拍中」的负收入挡在领取点击之外（模块卡死在领取分红）"


# ------------------------------------------------------------------
#  结算页滚动动画：定值判据（跳过动画点击）+ 利润派生
#  真机背景（会话 20260917_210330）：四行金额是滚动计数动画，
#  「跳过动画后『领取』按钮出现」才是定值；动画中间值能连稳 ≥3 帧，
#  故「连续一致」拦不住它，必须用「已跳过动画」这条时序判据。
# ------------------------------------------------------------------


def _settle_module(clicked_once: bool, h_prices=None):
    """绕过 __init__ 造一个只带结算消费所需字段的 module（构造完整模块依赖过重）。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._round_no = None
    mod._refresh_daily_bucket = lambda *a, **k: None
    mod._current_stage = "领取分红"
    mod._h_prices = list(h_prices or [])
    mod._settle_collect_clicked_once = clicked_once
    mod._settle_skip_since_ms = 0
    mod._settle_skip_retry_count = 0
    mod._settle_stable = {}
    mod._settle_profit_cross_seen = None
    mod._settle_final_price = None
    mod._settle_total_price = None
    mod._settle_profit = None
    mod._settle_my_income = None
    mod._click_retry_key = None
    mod._click_retry_stage = None
    mod._click_retry_count = 0
    mod._balance_locked = False
    mod._my_balance = None
    return mod


def _feed_settle(mod, frames: int, **amounts):
    """按 OCR 结果格式喂 frames 帧同值读数（≥SETTLE_STABLE_FRAMES 才过连续一致闸）。"""
    res = {k: {"amount": v, "text": f"{v:,}"} for k, v in amounts.items()}
    for _ in range(frames):
        mod._consume_ocr_result(dict(res))


_STABLE = TreasureModule.SETTLE_STABLE_FRAMES


def test_animation_values_never_lock_before_skip_click():
    """点击「跳过动画」之前，页面还在滚动 → 四个金额一律不落值。

    真机第 12 场：动画期读到的 利润 −292,241 / 最终竞拍价 293,184 都不是定值。
    """
    mod = _settle_module(clicked_once=False, h_prices=[254_500])
    _feed_settle(mod, _STABLE + 2, settle_final_price=293_184, settle_profit=-292_241,
          settle_total_price=943, settle_my_income=9_798)
    assert mod._settle_final_price is None
    assert mod._settle_total_price is None
    assert mod._settle_my_income is None
    assert mod._settle_profit is None, "点击前不得派生利润（两个价格都还不是定值）"


def test_profit_is_derived_from_prices_after_skip_click():
    """跳过动画后落值，且利润 = 拍品总价 − 最终竞拍价（动画期的错值不得留下）。

    真机第 12 场真值：302,982 − 293,184 = +9,798（收入行独立读到同一个数）。
    """
    mod = _settle_module(clicked_once=True, h_prices=[254_500])
    _feed_settle(mod, _STABLE, settle_final_price=293_184, settle_total_price=302_982,
          settle_my_income=9_798, settle_profit=9_798)
    assert mod._settle_final_price == 293_184
    assert mod._settle_total_price == 302_982
    assert mod._settle_profit == 9_798
    assert mod._settle_my_income == 9_798


def test_small_profit_survives_relative_floor():
    """薄利赢单的真实小额利润必须落值（旧护栏把 5,173 当裁位残缺丢弃的回归锁）。

    真机第 14 场：最终价 144,345 / 合计 149,518 → 利润 +5,173，而 Hmax 125,300 的
    1/20 = 6,265 > 5,173，旧护栏据此丢弃真值、把动画中间值 −140,144 固化下来。
    """
    mod = _settle_module(clicked_once=True, h_prices=[125_300, 109_100])
    _feed_settle(mod, _STABLE, settle_final_price=144_345, settle_total_price=149_518,
          settle_profit=-140_144, settle_my_income=5_173)
    assert mod._settle_profit == 5_173, "利润以派生值为准，不受 Hmax/20 相对下限影响"
    assert mod._settle_profit_cross_seen == -140_144, "利润行读数与派生值不符 → 哨兵须告警"


def test_profit_rederives_when_price_is_corrected():
    """价格被覆盖时利润同步重算：动画中间值先落、真值后到也不留错数据。

    真机第 15 场序列：合计 31,013 → 31,361 → 320,605（最终价 477,000 恒定）。
    """
    mod = _settle_module(clicked_once=True, h_prices=[134_300])
    _feed_settle(mod, _STABLE, settle_final_price=477_000, settle_total_price=31_013)
    assert mod._settle_profit == 31_013 - 477_000
    _feed_settle(mod, _STABLE, settle_final_price=477_000, settle_total_price=320_605)
    assert mod._settle_total_price == 320_605
    assert mod._settle_profit == -156_395, "利润须随最终合计重算，不得停留在中间值"


def test_income_not_locked_before_skip_click():
    """收入的「0」同样只是动画中间值：点击前不得驱动领取点击（2026-09-16 事故回归锁）。

    该事故里收入被读成 0（合法终值），策略侧立刻命中 settle_ready_click，
    动画没播完就点了真领取——定值闸把这类瞬时值挡在上游。
    """
    mod = _settle_module(clicked_once=False, h_prices=[200_000])
    _feed_settle(mod, _STABLE + 2, settle_my_income=0)
    assert mod._settle_my_income is None
    mod._settle_collect_clicked_once = True
    _feed_settle(mod, _STABLE, settle_my_income=0)
    assert mod._settle_my_income == 0, "点击后读到 0 才是「未分红」的真值"