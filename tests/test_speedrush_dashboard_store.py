# -*- coding: utf-8 -*-
"""极速狂飙回合战绩库：upsert、统计、列表载荷与空/坏库兜底。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from maaracing_master.core.dashboard_contract import normalize_dashboard
from maaracing_master.plugins.speedrush import dashboard_store

LABELS = {"depth": "深度", "straight": "直行", "manual": "人工",
          "macro": "宏", "control": "自动", "unknown": "—"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "speedrush.db"
    monkeypatch.setattr(dashboard_store, "db_path", lambda: path)
    return path


def _iso(day: date, hour: int, minute: int, second: int = 0) -> str:
    return datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute, second=second).isoformat(timespec="seconds")


def _write(phase: int, score: int | None, opponent: int | None, *,
           stamp: str = "s1", session: str = "t1", round_no: int = 1,
           mode: str = "depth", completed: bool = True, ticks: int = 10,
           dur: float = 40.0, started: str | None = None,
           ended: str | None = None, source: str = "live") -> str | None:
    day = date.today()
    return dashboard_store.write_phase(
        session_token=session, round_stamp=stamp, round_no=round_no,
        drive_mode=mode, phase=phase,
        stats=dashboard_store.PhaseStats(ticks, score, 42.0, dur),
        completed=completed, opponent=opponent,
        started_at=started or _iso(day, 19, 55),
        ended_at=ended or _iso(day, 19, 56),
        source=source,
    )


def _payload() -> dict:
    return dashboard_store.read_dashboard_payload(LABELS)


def _stats(payload: dict) -> dict:
    return {item["label"]: item["value"] for item in payload["blocks"][0]["items"]}


def _rows(payload: dict) -> list[dict]:
    return payload["blocks"][1]["rows"]


def test_round_upsert_uses_last_phase_score(db):
    assert _write(1, 2579, 2919) is None
    assert _write(2, 5693, 7858, started=_iso(date.today(), 19, 56),
                  ended=_iso(date.today(), 19, 57)) is None

    rows = dashboard_store.read_rows()
    assert len(rows) == 1
    payload = _payload()
    assert _stats(payload) == {
        "今日回合": "1", "今日胜场": "0", "总回合数": "1", "总胜场": "0"}
    row = _rows(payload)[0]
    assert row["leading"] == "19:55"          # 回合开始，不是阶段 2 开始
    assert row["group"] == "今日"
    assert [p["text"] for p in row["parts"]] == ["深度 · ", "5,693", " / ", "7,858"]
    assert row["trailing"] == "负 -2,165"     # 5693 - 7858，不是两段相加
    assert payload["blocks"][1]["page_size"] == 5
    normalized, warnings = normalize_dashboard(payload)
    assert warnings == ()
    assert normalized["blocks"][1]["rows"][0]["group"] == "今日"


def test_stats_count_rounds_and_wins(db):
    day = date.today()
    assert _write(1, 3000, 2800, stamp="win", started=_iso(day, 19, 10)) is None
    assert _write(2, 7000, 6000, stamp="win", started=_iso(day, 19, 11),
                  ended=_iso(day, 19, 12)) is None
    assert _write(1, 3000, 4000, stamp="loss", started=_iso(day, 20, 10)) is None
    assert _write(2, 5000, 6000, stamp="loss", started=_iso(day, 20, 11),
                  ended=_iso(day, 20, 12)) is None

    payload = _payload()
    assert _stats(payload) == {
        "今日回合": "2", "今日胜场": "1", "总回合数": "2", "总胜场": "1"}
    assert _rows(payload)[0]["trailing"] == "负 -1,000"   # 20:10 的负局在前
    assert _rows(payload)[1]["trailing"] == "胜 +1,000"


def test_partial_round_is_listed_but_not_counted_as_win(db):
    assert _write(1, 2233, 2797) is None
    payload = _payload()
    row = _rows(payload)[0]
    assert row["badge"] == {"text": "未完成", "tone": "warn"}
    assert row["text"] == "深度 · 阶段1"
    assert row["trailing"] == "2,233 分"
    assert _stats(payload)["今日胜场"] == "—"
    assert _stats(payload)["总胜场"] == "—"


def test_missing_opponent_is_honest(db):
    assert _write(1, 2579, None) is None
    assert _write(2, 5693, None, started=_iso(date.today(), 19, 56),
                  ended=_iso(date.today(), 19, 57)) is None
    payload = _payload()
    row = _rows(payload)[0]
    assert row["text"] == "深度 · 对手分未记录"
    assert row["trailing"] == "—"
    assert _stats(payload)["今日胜场"] == "—"


def test_duplicate_phase_write_is_idempotent(db):
    assert _write(2, 5693, 7858) is None
    assert _write(2, 5693, 7858, started=_iso(date.today(), 19, 56)) is None
    assert len(dashboard_store.read_rows()) == 1


def test_import_does_not_overwrite_live(db):
    assert _write(2, 6900, 8000) is None
    assert dashboard_store.existing_source("s1", "t1") == "live"
    assert _write(2, 100, 200, source="import") is None
    rows = dashboard_store.read_rows()
    assert len(rows) == 1
    assert rows[0]["phase2_score"] == 6900
    assert rows[0]["source"] == "live"


def test_empty_db_payload(db):
    payload = _payload()
    assert payload["blocks"] == [{"type": "text", "text": "还没有回合记录。"}]
    normalized, warnings = normalize_dashboard(payload)
    assert warnings == ()
    assert normalized["blocks"][0]["text"] == "还没有回合记录。"


def test_older_round_gets_date_group(db):
    today = date.today()
    yesterday = today - timedelta(days=1)
    assert _write(1, 3000, 2800, stamp="old",
                  started=_iso(yesterday, 21, 0)) is None
    assert _write(2, 6000, 5000, stamp="old",
                  started=_iso(yesterday, 21, 1),
                  ended=_iso(yesterday, 21, 2)) is None
    assert _write(1, 3000, 2800, stamp="new",
                  started=_iso(today, 19, 55)) is None
    assert _write(2, 7000, 6000, stamp="new",
                  started=_iso(today, 19, 56),
                  ended=_iso(today, 19, 57)) is None

    payload = _payload()
    rows = _rows(payload)
    assert [row["group"] for row in rows] == ["今日", yesterday.strftime("%m-%d")]
    assert _stats(payload) == {
        "今日回合": "1", "今日胜场": "1", "总回合数": "2", "总胜场": "2"}


def test_newer_schema_is_rejected(db):
    assert _write(1, 3000, 2800) is None
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(dashboard_store.StoreError):
        _payload()
