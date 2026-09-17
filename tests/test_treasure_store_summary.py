# -*- coding: utf-8 -*-
"""落盘当日汇总口径（2026-09-17 定案，看板「银币盈亏」的数据源）。

问题（真机库，2026-09-17 桶）：`daily_summary.profit_sum` 对所有场次累加
`settle_profit`——未中场的那个数是**中标者**的盈亏（RULES §7 术语表：利润只属于
拍中者），把它记成我方的等于替对手记账，当日存出 −980,069 这种无意义的数。

定案口径（本文件锁）：
- `profit_sum` = 我方竞拍净利，只累加**我方拍中**场的 `settle_profit`；
- `income_sum` = 我方本场收入，全部场次都累加（拍中=利润、未拍中=中标者亏钱时的
  5/10/15% 分红，见 RULES §3）；
- 看板「银币盈亏」= `income_sum` + 蛋奖励银币（`egg_coin`）——分红是纯收入，
  只算拍中场利润会把当天赚到的分红整个漏掉。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from maaracing_master.plugins.treasure.store import TreasureStore


class _FakeModule:
    """flush_game_record / record_egg_claim 读到的字段全集（无逻辑，纯数据）。"""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._session_daily_done_count = 0
        self._auction_result = None
        self._settle_final_price = None
        self._settle_total_price = None
        self._settle_profit = None
        self._settle_my_income = None
        self._daily_high_score = None
        self._h_prices: list[int] = []
        self._our_bids: list[int] = []
        self._player_bids: dict = {}
        self._my_rank = None
        self._my_balance = None

    def _refresh_daily_bucket(self) -> None:
        pass


def _game(m, *, result: str, final: int, total: int, income: int, score=None):
    """按一场结束时的字段状态落盘（利润由 total−final 派生，与生产一致）。"""
    m._auction_result = result
    m._settle_final_price = final
    m._settle_total_price = total
    m._settle_profit = total - final
    m._settle_my_income = income
    m._daily_high_score = score
    m._session_daily_done_count += 1


def _summary_row(store, bucket: str):
    conn = store._conn
    cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_summary)").fetchall()]
    row = conn.execute("SELECT * FROM daily_summary WHERE bucket = ?", (bucket,)).fetchone()
    assert row is not None, "summary 行应已写入"
    return dict(zip(cols, row))


@pytest.fixture
def store_and_module():
    with tempfile.TemporaryDirectory() as d:
        m = _FakeModule(Path(d) / "data")
        store = TreasureStore(m)
        yield store, m
        store.close_db()


def test_profit_sum_counts_only_our_wins(store_and_module):
    """未中场的利润（中标者的钱）不得进 profit_sum；分红只进 income_sum。"""
    store, m = store_and_module
    # 我方拍中：赚 9,798
    _game(m, result="win", final=293_184, total=302_982, income=9_798, score=9_798)
    store.flush_game_record()
    # 未中：中标者亏 208,311，我方按顺位拿 15% 分红 31,246
    _game(m, result="fail", final=548_600, total=340_289, income=31_246)
    store.flush_game_record()

    s = _summary_row(store, store.current_bucket_str())
    assert s["games"] == 2 and s["win"] == 1 and s["fail"] == 1
    assert s["profit_sum"] == 9_798, "profit_sum 只算我方拍中场的利润"
    assert s["income_sum"] == 9_798 + 31_246, "income_sum 含未中场分红"
    assert s["highest_score"] == 9_798


def test_board_value_includes_dividends_and_egg_coin(store_and_module):
    """看板口径：(income_sum + egg_coin) —— 分红与蛋奖励都是银币收入。"""
    store, m = store_and_module
    _game(m, result="win", final=144_345, total=149_518, income=5_173)
    store.flush_game_record()
    _game(m, result="fail", final=113_400, total=93_156, income=2_024)
    store.flush_game_record()
    store.record_egg_claim({"red": 1, "yellow": 0, "blue": 0}, coin=25_000, score=3_000)

    s = _summary_row(store, store.current_bucket_str())
    assert s["profit_sum"] == 5_173
    assert s["income_sum"] == 5_173 + 2_024
    assert s["income_sum"] + s["egg_coin"] == 5_173 + 2_024 + 25_000


def test_loss_win_keeps_negative_profit_in_sum(store_and_module):
    """我方亏本拍中（收入为负，RULES §3 允许）：负利润如实进 profit_sum。"""
    store, m = store_and_module
    _game(m, result="win", final=315_001, total=276_422, income=-38_579)
    store.flush_game_record()
    _game(m, result="fail", final=200_000, total=300_000, income=0)
    store.flush_game_record()

    s = _summary_row(store, store.current_bucket_str())
    assert s["profit_sum"] == -38_579
    assert s["income_sum"] == -38_579, "未中且中标者赚钱 → 无分红，收入 0"


def test_unknown_result_is_not_counted_as_our_profit(store_and_module):
    """竞拍结果漏读（auction_result 为 None）时不得把别人的盈亏当我方的。"""
    store, m = store_and_module
    _game(m, result=None, final=256_200, total=341_095, income=300_000)
    store.flush_game_record()

    s = _summary_row(store, store.current_bucket_str())
    assert s["profit_sum"] == 0
    assert s["win"] == 0 and s["fail"] == 0