#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2 trace 记录器单测。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from maaracing_assistant.core.navkit import FrameTrace, TraceWriter, json_safe


def _touch_trace(path: Path) -> Path:
    """构造一个含 trace.jsonl 的独立 trace 会话目录。"""
    path.mkdir(parents=True, exist_ok=True)
    (path / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    return path


def test_frame_trace_has_reconstruction_fields():
    trace = FrameTrace(
        frame=7,
        stage="第1回合出价",
        round_no=1,
        scores={"round_big_banner": 0.91},
        hit_anchor="round_big_banner",
        active_used={"round_big_banner", "smart_bid_btn"},
        intent={"key": "bid_main_red_btn", "center": (0.5, 0.8)},
        click_result={"ok": False, "device_lost": False},
        timestamp_ms=123,
    )
    data = trace.as_dict()
    assert data["frame"] == 7
    assert data["scores"]["round_big_banner"] == 0.91
    assert sorted(data["active_used"]) == ["round_big_banner", "smart_bid_btn"]
    assert data["intent"]["center"] == [0.5, 0.8]
    assert "plan_version" not in data  # 执行通路唯一后该字段已退役


def test_trace_writer_appends_compact_jsonl(tmp_path: Path):
    with TraceWriter(tmp_path, keep_sessions=10, session_name="session_20260906_080000") as writer:
        writer.write(FrameTrace(frame=1, stage="大厅", round_no=None, timestamp_ms=1))
        writer.write({"frame": 2, "stage": "出价", "scores": {"x": 0.5}})
    path = tmp_path / "session_20260906_080000" / "trace.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["frame"] == 1
    assert json.loads(lines[1])["frame"] == 2


def test_trace_writer_rejects_invalid_session_name(tmp_path: Path):
    with pytest.raises(ValueError):
        TraceWriter(tmp_path, session_name="not-a-session")


def test_trace_prunes_old_sessions(tmp_path: Path):
    for name in ("session_20260901_000000", "session_20260902_000000", "session_20260903_000000"):
        _touch_trace(tmp_path / name)
    writer = TraceWriter(tmp_path, keep_sessions=2, session_name="session_20260904_000000")
    removed = writer.prune()
    writer.close()
    assert (tmp_path / "session_20260904_000000").exists()   # keep=2：当前会话 + 次新会话保留
    assert (tmp_path / "session_20260903_000000").exists()
    assert not (tmp_path / "session_20260902_000000").exists()
    assert not (tmp_path / "session_20260901_000000").exists()
    assert len(removed) == 2


def test_trace_writer_default_session_name_matches_debug_form(tmp_path: Path):
    """独立 trace 会话与 debug 截图会话同名形态：纯 YYYYMMDD_HHMMSS。"""
    with TraceWriter(tmp_path) as writer:
        writer.write(FrameTrace(frame=1, stage="大厅", round_no=None, timestamp_ms=1))
    assert re.fullmatch(r"\d{8}_\d{6}", writer.session_dir.name)
    assert (writer.session_dir / "trace.jsonl").is_file()


def test_trace_prune_never_touches_frame_or_empty_dirs(tmp_path: Path):
    """含 raw/ 的截图会话与无 trace.jsonl 的空目录，prune 一律不碰。"""
    frames = tmp_path / "20260101_000000"
    (frames / "raw").mkdir(parents=True)
    (frames / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "20260102_000000").mkdir()          # 空目录：非 trace 产物
    stale = _touch_trace(tmp_path / "20260905_000000")
    writer = TraceWriter(tmp_path, keep_sessions=1, session_name="20260906_000000")
    removed = writer.prune()
    writer.close()
    assert frames.exists()
    assert (tmp_path / "20260102_000000").exists()
    assert removed == [stale]


def test_trace_prune_sorts_legacy_and_new_names_by_timestamp(tmp_path: Path):
    """遗留 session_ 前缀与纯时间戳混合时，按去掉前缀后的时间戳排序。"""
    oldest = _touch_trace(tmp_path / "session_20260901_000000")
    newer = _touch_trace(tmp_path / "20260905_000000")
    writer = TraceWriter(tmp_path, keep_sessions=2, session_name="20260906_000000")
    removed = writer.prune()
    writer.close()
    assert removed == [oldest]
    assert newer.exists()


def test_json_safe_does_not_leak_paths_or_objects():
    class Box:
        pass

    converted = json_safe({"box": (1, 2), "obj": Box()})
    assert converted["box"] == [1, 2]
    assert isinstance(converted["obj"], str)
