# -*- coding: utf-8 -*-
"""历史回合导入：匹配、假读拒收、人工跳过、dry-run 与幂等。"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from maaracing_master.plugins.speedrush import dashboard_store
from maaracing_master.plugins.speedrush.hud import SIDE_EGO, SIDE_OPPONENT

_TOOL = (Path(__file__).resolve().parent.parent
         / "maaracing_master" / "plugins" / "speedrush" / "tools"
         / "import_rounds.py")
_SPEC = importlib.util.spec_from_file_location("import_rounds", _TOOL)
ir = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ir)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "speedrush.db"
    monkeypatch.setattr(dashboard_store, "db_path", lambda: path)
    return path


def _at(hour: int, minute: int, second: int) -> datetime:
    return datetime.combine(date.today(), datetime.min.time()).replace(
        hour=hour, minute=minute, second=second)


def _fields(ego: int, opp: int, *, rate: int | None = 42) -> dict:
    return {
        "score_a": {"value": ego, "trusted": True, "side": SIDE_EGO},
        "rate_a": {"value": rate, "trusted": rate is not None, "side": SIDE_EGO},
        "score_b": {"value": opp, "trusted": True, "side": SIDE_OPPONENT},
        "rate_b": {"value": None, "trusted": False, "side": SIDE_OPPONENT},
    }


def _write_hud(meta_dir: Path, start: tuple[int, int], end: tuple[int, int], *,
               started: datetime, dur_s: float = 36.0,
               false_no_rate: tuple[int, int] | None = None,
               false_spike: tuple[int, int] | None = None) -> None:
    """稠密采样序列（1s 一行、带 ts_ns，与真实 hud.jsonl 同契约）：
    从 start 线性走到 end，假读行缀在末尾。"""
    t0 = int(started.timestamp() * 1e9)
    steps = max(int(dur_s), 2)
    rows = []
    for index in range(steps + 1):
        f = index / steps
        ego = round(start[0] + (end[0] - start[0]) * f)
        opp = round(start[1] + (end[1] - start[1]) * f)
        rows.append({"seq": index + 1, "ts_ns": t0 + index * 1_000_000_000,
                     "fields": _fields(ego, opp)})
    seq = steps + 2
    if false_no_rate is not None:
        ego, opp = false_no_rate
        rows.append({"seq": seq, "ts_ns": t0 + (seq - 1) * 1_000_000_000,
                     "fields": _fields(ego, opp, rate=None)})
        seq += 1
    if false_spike is not None:
        ego, opp = false_spike
        rows.append({"seq": seq, "ts_ns": t0 + (seq - 1) * 1_000_000_000,
                     "fields": _fields(ego, opp, rate=400)})
    hud_dir = meta_dir / "hud"
    hud_dir.mkdir(parents=True, exist_ok=True)
    (hud_dir / "hud.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8")


def _write_meta(root: Path, stamp: str, phase: int, started: datetime,
                ended: datetime, round_no: int, *, source: str = "control") -> Path:
    meta_dir = root / f"{stamp}_p{phase}"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "meta.json").write_text(json.dumps({
        "schema": 4, "source": source, "phase": phase, "round_no": round_no,
        "started_at": started.isoformat(timespec="seconds"),
        "ended_at": ended.isoformat(timespec="seconds"),
        "stop_reason": "chain_end",
    }, ensure_ascii=False), encoding="utf-8")
    return meta_dir


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    logs = tmp_path / "logs"
    session = logs / "sess1"
    trace_dir = session / "trace"
    trace_dir.mkdir(parents=True)
    debug = tmp_path / "debug"
    drive = debug / "sess1" / "speedrush" / "drive"

    starts = [_at(19, 55, 32), _at(19, 56, 17), _at(19, 57, 37), _at(19, 58, 20)]
    ends = [_at(19, 56, 9), _at(19, 57, 3), _at(19, 58, 15), _at(19, 59, 10)]
    scores = [2579, 5693, 2233, 6899]
    durations = [37.0, 46.1, 37.5, 49.1]
    phases = [1, 2, 1, 2]
    summaries = []
    for start, score, dur, phase in zip(starts, scores, durations, phases):
        summaries.append({
            "ts": start.strftime("%Y%m%d_%H%M%S"), "phase": phase,
            "ticks": 10, "score": score, "rate": 42, "dur_s": dur,
            "conserve_segments": 0,
        })
    (trace_dir / "speedrush_summary.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in summaries) + "\n",
        encoding="utf-8")

    trace_rows = []
    for index, (start, phase) in enumerate(zip(starts, phases), 1):
        trace_rows.append({
            "type": "stage_begin", "stage_id": phase,
            "ts_wall_ns": int(start.timestamp() * 1e9),
        })
        trace_rows.append({
            "stage_id": phase, "fid": index, "state": "CRUISE",
        })
    (trace_dir / "speedrush.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in trace_rows) + "\n",
        encoding="utf-8")

    # 局 1：段 1 混入一条「虚高 4878 / 对手 888」的假读；段 2 末对 5693 / 7858
    p1 = _write_meta(drive, "20261105_195502", 1, starts[0], ends[0], 1)
    _write_hud(p1, (100, 90), (2579, 2919), started=starts[0],
               false_spike=(4878, 888))
    p2 = _write_meta(drive, "20261105_195502", 2, starts[1], ends[1], 1)
    _write_hud(p2, (2792, 3079), (5693, 7858), started=starts[1])
    # 局 2：段 1 混入一条「无速度行的串位」假读；段 2 末对 6899 / 8929
    p1 = _write_meta(drive, "20261105_195714", 1, starts[2], ends[2], 2)
    _write_hud(p1, (27, 15), (2233, 2797), started=starts[2],
               false_no_rate=(283, 22840))
    p2 = _write_meta(drive, "20261105_195714", 2, starts[3], ends[3], 2)
    _write_hud(p2, (2587, 3059), (6899, 8929), started=starts[3])
    return logs, debug


def test_build_rounds_matches_two_rounds_and_rejects_false_reads(db, tmp_path):
    logs, debug = _fixture(tmp_path)
    rounds = ir.build_rounds(logs, debug)
    assert len(rounds) == 2
    assert [r["mode"] for r in rounds] == ["depth", "depth"]
    first = rounds[0]
    assert first["p1"][0]["score"] == 2579
    assert ir._phase_record(first, 1)["opponent"] == 2919
    assert ir._phase_record(first, 2)["opponent"] == 7858
    second = rounds[1]
    assert ir._phase_record(second, 1)["opponent"] == 2797
    assert ir._phase_record(second, 2)["opponent"] == 8929


def test_manual_round_is_not_imported(db, tmp_path):
    logs, debug = _fixture(tmp_path)
    drive = debug / "sess1" / "speedrush" / "drive"
    extra = _at(21, 0, 0)
    trace_dir = logs / "sess1" / "trace"
    with (trace_dir / "speedrush_summary.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": extra.strftime("%Y%m%d_%H%M%S"), "phase": 1,
            "ticks": 10, "score": 100, "rate": 10, "dur_s": 10.0,
            "conserve_segments": 0,
        }) + "\n")
    _write_meta(drive, "20261105_210000", 1, extra,
                _at(21, 0, 10), 3, source="manual")

    rounds = ir.build_rounds(logs, debug)
    assert len(rounds) == 2


def test_main_dry_run_does_not_write(db, tmp_path, capsys):
    logs, debug = _fixture(tmp_path)
    code = ir.main(["--dry-run", "--logs-root", str(logs),
                    "--debug-root", str(debug)])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out
    assert not db.exists()


def test_main_writes_and_is_idempotent(db, tmp_path, capsys):
    logs, debug = _fixture(tmp_path)
    args = ["--logs-root", str(logs), "--debug-root", str(debug)]
    assert ir.main(args) == 0
    rows = dashboard_store.read_rows()
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"import"}
    by_stamp = {row["round_stamp"]: row for row in rows}
    assert by_stamp["20261105_195502"]["phase2_score"] == 5693
    assert by_stamp["20261105_195502"]["phase2_opponent"] == 7858
    assert by_stamp["20261105_195714"]["phase2_score"] == 6899
    assert by_stamp["20261105_195714"]["phase2_opponent"] == 8929

    assert ir.main(args) == 0
    assert len(dashboard_store.read_rows()) == 2
