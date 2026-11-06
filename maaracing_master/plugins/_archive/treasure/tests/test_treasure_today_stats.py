# -*- coding: utf-8 -*-
"""鉴宝今日看板读侧契约：schema 兼容与缺列兜底。

读侧按 PRAGMA 实有列取交集、缺列兜底，不依赖写侧启动时机。core 侧的路由分发机制
在 `tests/test_sidecar_today_stats.py` 单测，与本文件无关。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from maaracing_master.core import paths as core_paths
from maaracing_master.plugins._archive.treasure.store import read_today_stats

pytestmark = pytest.mark.retired


def _today_bucket() -> str:
    now = datetime.now()
    day = now.date() if now.hour >= 5 else now.date() - timedelta(days=1)
    return day.isoformat()


def _make_db(tmp_path, monkeypatch, summary_cols, summary_vals,
             games_cols=None, games_vals=None):
    """在 tmp_path 造 treasure/treasure.db，并把 data_dir 真源（core.paths）指过去。"""
    (tmp_path / "treasure").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(tmp_path / "treasure" / "treasure.db"))
    conn.execute(
        f"CREATE TABLE daily_summary (bucket TEXT PRIMARY KEY, "
        f"{', '.join(f'{c} INTEGER NOT NULL DEFAULT 0' for c in summary_cols)})",
    )
    conn.execute(
        "INSERT INTO daily_summary (bucket, "
        + ", ".join(summary_cols)
        + ") VALUES (?" + ", ?" * len(summary_cols) + ")",
        (_today_bucket(), *summary_vals),
    )
    gcols = games_cols or []
    conn.execute(
        "CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(["bucket TEXT", *gcols]) + ")"
    )
    if games_vals:
        conn.execute(
            "INSERT INTO games (bucket, "
            + ", ".join(c.split()[0] for c in gcols)
            + ") VALUES (?" + ", ?" * len(gcols) + ")",
            (_today_bucket(), *games_vals),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(core_paths, "data_dir", lambda: tmp_path)


def _read():
    payload = read_today_stats()
    assert set(payload) == {"bucket", "summary", "games"}
    return payload


def test_board_survives_old_schema_without_new_columns(tmp_path, monkeypatch):
    """旧库（无 egg_coin/egg_score）→ 不抛错，缺列按 0 兜底，已有列原样返回。"""
    _make_db(
        tmp_path, monkeypatch,
        summary_cols=["games", "win", "fail", "profit_sum", "income_sum",
                      "highest_score", "egg_red", "egg_yellow", "egg_blue"],
        summary_vals=[6, 4, 2, 12345, 67890, 999, 3, 1, 0],
    )
    payload = _read()
    s = payload["summary"]
    assert s is not None
    assert s["games"] == 6 and s["win"] == 4 and s["profit_sum"] == 12345
    assert s["egg_red"] == 3
    assert s["egg_coin"] == 0 and s["egg_score"] == 0


def test_board_full_with_new_schema(tmp_path, monkeypatch):
    _make_db(
        tmp_path, monkeypatch,
        summary_cols=["games", "win", "fail", "profit_sum", "income_sum",
                      "highest_score", "egg_red", "egg_yellow", "egg_blue",
                      "egg_coin", "egg_score"],
        summary_vals=[6, 4, 2, 12345, 67890, 999, 3, 1, 0, 250000, 30000],
    )
    s = _read()["summary"]
    assert s["egg_coin"] == 250000 and s["egg_score"] == 30000


def test_board_games_rows_survive_missing_optional_columns(tmp_path, monkeypatch):
    """games 缺 strategy_mode → 该行照返回，字段为 None（不连坐整表）。"""
    _make_db(
        tmp_path, monkeypatch,
        summary_cols=["games"], summary_vals=[1],
        games_cols=["game_seq INTEGER", "ts TEXT", "auction_result TEXT",
                    "settle_profit INTEGER", "egg_red INTEGER",
                    "egg_yellow INTEGER", "egg_blue INTEGER"],
        games_vals=[1, "2026-09-14T13:20:00", "win", -5000, 1, 0, 0],
    )
    payload = _read()
    assert payload["games"], "旧 schema games 行不应被整表丢弃"
    g = payload["games"][0]
    assert g["game_seq"] == 1 and g["auction_result"] == "win"
    assert g["strategy_mode"] is None
    assert g["total_price"] is None


def test_board_missing_db_is_empty(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch, summary_cols=["games"], summary_vals=[0])
    (tmp_path / "treasure" / "treasure.db").unlink()
    payload = read_today_stats()
    assert payload["summary"] is None and payload["games"] == []


def test_module_declares_today_stats_hook():
    """契约接线锁：TreasureModule 必须覆写看板钩子——core 路由靠它分发，
    漏写 = GUI 看板静默空掉（无 schema 机检可依，靠本锁）。"""
    from maaracing_master.core.base import ActivityModule
    from maaracing_master.plugins._archive.treasure.module import TreasureModule

    assert TreasureModule.read_today_stats is not ActivityModule.read_today_stats
