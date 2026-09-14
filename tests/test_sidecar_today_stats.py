# -*- coding: utf-8 -*-
"""今日看板读取（SidecarService.get_today_stats）的 schema 容错契约。

背景：daily_summary 新列（egg_coin/egg_score）的 ALTER 迁移由鉴宝模块写侧惰性建连时执行；
升级后存在「GUI 已启动、模块未跑过」的窗口，旧库缺列曾让看板每 3 秒报
`no such column: egg_coin` 并整块空掉。读侧必须按实有列取交集、缺列兜底，
永不依赖写侧启动时机。
本文件导入 core.sidecar（经 controller 拉 maa 等重依赖），缺依赖时整文件 SKIP。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService  # noqa: F401
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"看板读取测试需要完整运行时依赖：{_ERR}"
)


def _today_bucket() -> str:
    now = datetime.now()
    day = now.date() if now.hour >= 5 else now.date() - timedelta(days=1)
    return day.isoformat()


def _make_db(tmp_path, monkeypatch, summary_cols, summary_vals,
             games_cols=None, games_vals=None):
    """在 tmp_path 造 treasure/treasure.db，并把 data_dir 指过去。"""
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
    monkeypatch.setattr(sc, "data_dir", lambda: tmp_path)


def _read():
    svc = object.__new__(SidecarService)  # 只测纯读取方法，不拉完整服务
    ok, payload, err = SidecarService.get_today_stats(svc, None)
    assert ok and err is None
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
    monkeypatch.setattr(sc, "data_dir", lambda: tmp_path)
    payload = _read()
    assert payload["summary"] is None and payload["games"] == []
