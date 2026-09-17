# -*- coding: utf-8 -*-
"""鉴宝历史脏数据修复工具（scripts/repair_treasure_settle_data.py）的口径锁。

修的是 2026-09-17 之前版本在结算页留下的错值，判据全部来自 `RULES.md`
（利润 = 拍品总价 − 最终竞拍价；拍中者收入 = 利润；中标者未亏则无分红），
不是「看着像」——本文件锁住这条边界，防止将来把工具改成猜数。

另锁两个实现坑：
1. 修复后的汇总必须**从明细重建**，不能在库内旧汇总上做增量（旧 profit_sum 是旧口径，
   与逐行加总本就不等，增量叠上去等于把旧口径留下——首次实现即此错，靠落库后复验抓出）；
2. 「我方是否拍中」不能只看 `auction_result`：该字段 2026-09-06 漏读 21 场，
   只看标签会漏修那批行（09-06 有 5 行「收入 = 300000」串位脏值因此留存）。
   改看「我方槽位是否持有成交价」——成交价即赢家出价。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_treasure_settle_data.py"

try:
    _spec = importlib.util.spec_from_file_location("_repair_tool", _SCRIPT)
    assert _spec and _spec.loader
    tool = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(tool)
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"无法加载修复脚本：{_ERR}")

BUCKET = "2026-09-17"

# (game_seq, auction_result, final, total, profit, income, 我方最高出价)
# 每行都是实测过的真实错型；我方槽位 = 玩家 2（my_rank=2），故 player_bids 只放该槽。
_ROWS = [
    # ① 薄利赢单：真实利润 5,173 被旧护栏当裁位残缺丢弃记成 0
    (1, "win", 144_345, 149_518, 0, 5_173, 144_345),
    # ② 动画中间值固化：利润记成 943−293,184（真值 9,798）
    (2, "win", 293_184, 302_982, -292_241, 9_798, 293_184),
    # ③ 大厅场次卡串位：我方拍中却把「资产要求 300,000」当收入（真值 = 利润 90,363）
    (3, "win", 457_300, 547_663, 90_363, 300_000, 457_300),
    # ④ 我方亏本拍中：收入为负是合法真值（RULES §3①），不得被改成 0
    (4, "win", 513_954, 439_085, -74_869, 300_000, 513_954),
    # ⑤ 未中且中标者未亏 → 收入必须 0（旧版串位记成 300,000）
    (5, "fail", 208_000, 215_205, 7_205, 300_000, 180_000),
    # ⑦ 未中且中标者亏 151,215、收入与 5/10/15% 都不符 → 推不出来，只列不改
    (7, "fail", 338_240, 187_025, -151_215, 300_000, 300_000),
    # ⑧ 结果栏漏读（改版后横幅全盲）+ 我方持有成交价 → 我方拍中，收入 = 利润
    (8, None, 500_000, 590_363, 90_363, 300_000, 500_000),
    # ⑨ 结果栏漏读 + 我方未持有成交价 + 赢家未亏 → 收入归零
    (9, None, 500_000, 560_000, 60_000, 300_000, 400_000),
]


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "treasure.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT, game_seq INTEGER,
            auction_result TEXT, settle_final_price INTEGER, settle_total_price INTEGER,
            settle_profit INTEGER, settle_my_income INTEGER,
            my_rank INTEGER, player_bids TEXT);
        CREATE TABLE daily_summary (
            bucket TEXT PRIMARY KEY, profit_sum INTEGER, income_sum INTEGER);
        """
    )
    for seq, res, final, total, profit, income, ourmax in _ROWS:
        conn.execute(
            "INSERT INTO games (bucket, game_seq, auction_result, settle_final_price,"
            " settle_total_price, settle_profit, settle_my_income, my_rank, player_bids)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (BUCKET, seq, res, final, total, profit, income, 2,
             '{"玩家1": [10, 20, 30], "玩家2": [%d, %d]}' % (ourmax, ourmax)),
        )
    # 旧口径汇总：profit_sum 含未中场的「中标者盈亏」，income_sum 是错值加总
    conn.execute("INSERT INTO daily_summary VALUES (?,?,?)",
                 (BUCKET, sum(r[4] for r in _ROWS), sum(r[5] for r in _ROWS)))
    conn.commit()
    conn.close()
    return db


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def test_projection_equals_post_apply_state(tmp_path):
    """dry-run 投影必须等于落库后的实际汇总（增量叠旧汇总的坑由本条锁死）。"""
    db = _make_db(tmp_path)
    conn = _open(db)
    plan = tool.plan_repairs(conn)
    projected = tool.project_summaries(conn, plan)

    tool._apply(conn, plan, tool._summary_fix(conn, projected))
    conn.commit()

    assert tool._summaries(conn) == projected, "投影与实际落库结果必须一致"
    assert tool._summary_fix(conn) == [], "落库后汇总与明细应完全对齐"
    conn.close()


def test_profit_recomputed_from_prices(tmp_path):
    """利润一律按「拍品总价 − 最终竞拍价」重算（含薄利赢单与负利润两种方向）。"""
    db = _make_db(tmp_path)
    conn = _open(db)
    tool._apply(conn, tool.plan_repairs(conn), [])
    conn.commit()

    got = {r["game_seq"]: r["settle_profit"] for r in conn.execute("SELECT * FROM games")}
    assert got[1] == 5_173, "薄利赢单的小额利润必须落值（旧护栏的误杀不得复现）"
    assert got[2] == 9_798, "动画中间值必须被真值覆盖"
    assert got[4] == -74_869, "我方亏本拍中的负利润如实保留"
    conn.close()


def test_income_repaired_only_where_rule_decides(tmp_path):
    """收入只在规则能定论处改；推不出的保持原样（不发明数据）。"""
    db = _make_db(tmp_path)
    conn = _open(db)
    plan = tool.plan_repairs(conn)
    assert len(plan["win_income_fix"]) == 3, "③④⑧ 三场我方拍中 → 收入按利润重算"
    assert len(plan["fail_income_fix"]) == 2, "⑤⑨ 未中且赢家未亏 → 归零"
    assert [r[1] for r in plan["unfixable"]] == [7], "⑦ 无从推导 → 只列出"

    tool._apply(conn, plan, [])
    conn.commit()
    got = {r["game_seq"]: r["settle_my_income"] for r in conn.execute("SELECT * FROM games")}
    assert got[3] == 90_363 and got[5] == 0
    assert got[4] == -74_869, "亏本拍中的负收入是合法真值，不得被归零"
    assert got[7] == 300_000, "推不出来的行保持原样"
    assert got[8] == 90_363, "结果栏漏读但持有成交价 → 我方拍中，收入 = 利润"
    assert got[9] == 0, "结果栏漏读且未持有成交价、赢家未亏 → 收入 0"
    conn.close()


def test_auction_result_null_rows_are_still_repaired(tmp_path):
    """结果栏漏读的行也要修——只看 auction_result 会漏掉 09-06 那批串位脏值。"""
    db = _make_db(tmp_path)
    conn = _open(db)
    row = conn.execute("SELECT settle_my_income FROM games WHERE game_seq = 8").fetchone()
    assert row["settle_my_income"] == 300_000, "前置：⑧ 是漏读行 + 串位脏值"
    tool._apply(conn, tool.plan_repairs(conn), [])
    conn.commit()
    row = conn.execute("SELECT settle_my_income FROM games WHERE game_seq = 8").fetchone()
    assert row["settle_my_income"] == 90_363
    conn.close()


def test_summary_rebuilt_from_rows_with_new_caliber(tmp_path):
    """汇总按明细重建，且 profit_sum 只含我方拍中场的利润。"""
    db = _make_db(tmp_path)
    conn = _open(db)
    plan = tool.plan_repairs(conn)
    tool._apply(conn, plan, tool._summary_fix(conn, tool.project_summaries(conn, plan)))
    conn.commit()

    s = conn.execute("SELECT * FROM daily_summary").fetchone()
    # 期望值按规则算：拍中场利润 = 总价 − 成交价；未中场收入见各类处置
    labeled_win_profit = sum(r[3] - r[2] for r in _ROWS if r[1] == "win")
    assert s["profit_sum"] == labeled_win_profit == 5_173 + 9_798 + 90_363 - 74_869, \
        "profit_sum 只累加 auction_result == win 的行（与写侧 store.py 同口径）"
    expect_income = labeled_win_profit + 90_363 + 300_000  # ⑧ 漏读但拍中；⑦ 无从推导保持原值
    assert s["income_sum"] == expect_income, "income_sum = 逐行收入加总（未分红的也在内，含漏读行）"
    conn.close()