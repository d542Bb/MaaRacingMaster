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
    assert payload["subtitle"] == date.today().isoformat() + " 00:00 起"
    assert [block["type"] for block in payload["blocks"]] == ["stats", "list"]
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


# ---------------- 比分合理性门（ScoreGate / summarize 走闸） ----------------
# 判据与参数的校准依据见 dashboard_store 常量注；这里锁行为：
# 正常序列（含对手快突进）零误拒，单样本尖峰/塌坑（双向）全部被拒且自愈。

_NS = 650_000_000  # 采样间隔：实测节奏 0.63~0.68s（真步进 ≈ 速率 × 此时长）


def _feed(gate, values, *, start_ns=1_000_000_000, step_ns=_NS):
    """顺序喂入序列，返回每次判定结果；dt 可用列表逐项覆盖。"""
    outs = []
    for i, v in enumerate(values):
        outs.append(gate.feed(start_ns + i * step_ns, v))
    return outs


def _rows_from_samples(samples, *, ticks_per_sample=10, tick_ns=50_000_000):
    """HUD 样本序列 → 逐拍流水行（同一样本值重复多拍，同 summarize 真实输入）。"""
    rows = []
    for ts, value in samples:
        for k in range(ticks_per_sample):
            rows.append({"ts_ns": ts + k * tick_ns, "state": "DRIVE",
                         "hud_score": value})
    return rows


def test_gate_clean_growth_accepts_all_and_lands_on_last():
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    outs = _feed(gate, [1, 51, 101, 151, 201, 251, 301, 351, 401, 451, 501])
    assert outs[-1] is True
    assert gate.accepted == 501
    assert gate.rejected == 0


def test_gate_rejects_single_sample_spike_at_end():
    """实测 214,760：尖峰是最后一拍时，末分保持最近被采纳值（本次修复的回归锁）。"""
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    _feed(gate, [1824, 2083, 2341, 2600, 2860, 3120, 3380, 3640, 3900, 4160,
                 4420, 4709])
    assert gate.feed(1_000_000_000 + 11 * _NS, 214_760) is False
    assert gate.accepted == 4709
    assert gate.rejected == 1


def test_gate_rejects_single_sample_dip_and_self_heals():
    """实测 6948 → 719 → 6992：塌坑被拒，真值从原轨迹继续。"""
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    _feed(gate, [6898, 6930, 6948])
    assert gate.feed(1_000_000_000 + 3 * _NS, 719) is False
    assert gate.accepted == 6948
    assert gate.feed(1_000_000_000 + 4 * _NS, 6992) is True
    assert gate.accepted == 6992


def test_gate_bootstrap_corrects_inflated_first_read():
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    outs = _feed(gate, [214_760, 30, 60, 90])
    assert outs[0] is False
    assert outs[-1] is True
    assert gate.accepted == 90


def test_gate_bootstrap_corrects_deflated_first_read():
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    outs = _feed(gate, [719, 6970, 6990])
    assert outs[0] is False
    assert outs[-1] is True
    assert gate.accepted == 6990


def test_gate_opponent_fast_burst_not_rejected():
    """对手实测单步 +574（912/秒）是真突进——用户口径的回归锁：闸不得误杀。"""
    gate = dashboard_store.ScoreGate(dashboard_store.OPP_RATE_CAP)
    outs = _feed(gate, [1092, 1143, 1177, 1751, 1770, 1825])
    assert outs[0] is False          # 首拍是引导候选，按设计不采纳
    assert all(outs[1:])
    assert gate.accepted == 1825


def test_gate_opponent_huge_spike_rejected():
    gate = dashboard_store.ScoreGate(dashboard_store.OPP_RATE_CAP)
    _feed(gate, [5061, 5121, 5272, 5352, 5487])
    assert gate.feed(1_000_000_000 + 5 * _NS, 414_920) is False
    assert gate.accepted == 5487


def test_gate_level_shift_re_establishment():
    """实测 1102 → 1683（871/秒）的真跃迁：第二拍起基线跟上，不焊死在旧电平。"""
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    _feed(gate, [1064, 1083, 1102])
    assert gate.feed(1_000_000_000 + 3 * _NS, 1683) is False
    # 基线时刻不随拒绝推进，间隔累计后允许增量自然覆盖跃迁；即使仍被拒，
    # 连续两拍被拒也走解卡重新引导——两条路都落到「跟上新电平」。
    gate.feed(1_000_000_000 + 4 * _NS, 1704)
    gate.feed(1_000_000_000 + 5 * _NS, 1724)
    assert gate.feed(1_000_000_000 + 6 * _NS, 1796) is True
    assert gate.accepted == 1796


def test_gate_plateau_does_not_widen_allowance():
    """长平台把允许增量钳在 _STEP_MAX_S 内：冻结期混入的假读仍然出不去。"""
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    _feed(gate, [5000, 5021], step_ns=_NS)
    # 平台 10 秒后一笔 +8000：按 500/秒 × 钳制 3 秒 = 1500，拒
    assert gate.feed(1_000_000_000 + 2 * _NS + 10_000_000_000, 13021) is False
    assert gate.accepted == 5021


def test_gate_same_sample_repeat_feed_is_cached():
    gate = dashboard_store.ScoreGate(dashboard_store.EGO_RATE_CAP)
    _feed(gate, [1000, 1021])
    ts = 1_000_000_000 + 2 * _NS
    assert gate.feed(ts, 214_760) is False
    assert gate.feed(ts, 214_760) is False   # 同样本重复喂不重复计数
    assert gate.rejected == 1


def test_summarize_trace_rows_gated_final():
    rows = _rows_from_samples([
        (1_000_000_000, 1824), (1_650_000_000, 2083), (2_300_000_000, 2341),
        (2_950_000_000, 2600), (3_600_000_000, 2860), (4_250_000_000, 3120),
        (4_900_000_000, 3380), (5_550_000_000, 3640), (6_200_000_000, 3900),
        (6_850_000_000, 4160), (7_500_000_000, 4420), (8_150_000_000, 4709),
        (8_800_000_000, 214_760),
    ])
    stats = dashboard_store.summarize_trace_rows(rows)
    assert stats.score == 4709
    assert stats.gate_rejected == 1


def test_summarize_trace_rows_clean_series_unchanged():
    """回归锁：干净序列的末分与旧口径（最后一条非空值）逐字一致。"""
    samples = [(1_000_000_000 + i * _NS, v)
               for i, v in enumerate([2873, 2966, 3060, 3154, 3248, 3342])]
    rows = _rows_from_samples(samples)
    stats = dashboard_store.summarize_trace_rows(rows)
    assert stats.score == 3342
    assert stats.gate_rejected == 0


def test_summarize_trace_rows_without_scores_stays_none():
    rows = [{"ts_ns": 1_000_000_000, "state": "DRIVE", "hud_score": None}]
    stats = dashboard_store.summarize_trace_rows(rows)
    assert stats.score is None
    assert stats.gate_rejected == 0


def test_pick_opponent_with_gated_pairs_ignores_spike_pair():
    """尖峰对（214,760 / 7,881）不入锚选面后，余量锚选回到真实末对。"""
    gates = dashboard_store.new_pair_gates()
    raw_pairs = [(4455, 7400), (4667, 7708), (4709, 7881), (214_760, 7881)]
    ts = 1_000_000_000
    pairs = []
    for ego, opp in raw_pairs:
        if gates[0].feed(ts, ego) and gates[1].feed(ts, opp):
            pairs.append((ego, opp))
        ts += _NS
    assert (214_760, 7881) not in pairs
    assert dashboard_store.pick_opponent(pairs, 4709) == 7881
