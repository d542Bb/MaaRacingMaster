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
    """策略侧 `settle_income >= 0` 确实接受 0 → 「瞬时 0」只能在上游拦截。

    锁住这条分工：策略层无时效概念（facts 里只有值），因此不得把「读数稳不稳」
    的判据搬到策略层去；它必须发生在 OCR 消费段（`_settle_field_stable`）。
    """
    import pathlib
    import re
    policy = pathlib.Path("maaracing_master/plugins/treasure/resources/policy/"
                          "treasure.policy.json").read_text(encoding="utf-8")
    m = re.search(r'"id":\s*"settle_ready_click".*?"when":\s*\{(.*?)\}',
                  policy, re.S)
    assert m, "settle_ready_click 规则改名 → 本锁须同步更新"
    assert '"settle_income"' in m.group(1) and '"gte": 0' in m.group(1), \
        "该规则以 settle_income >= 0 判定「收入已读出」——0 被接受，故上游必须有闸"