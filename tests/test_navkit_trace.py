#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2 trace 记录器单测。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from maaracing_master.core.navkit import FrameTrace, TraceWriter, json_safe


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
    with TraceWriter(tmp_path, session_name="session_20260906_080000") as writer:
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


def test_trace_writer_default_session_name_matches_debug_form(tmp_path: Path):
    """独立 trace 会话与 debug 截图会话同名形态：纯 YYYYMMDD_HHMMSS。"""
    with TraceWriter(tmp_path) as writer:
        writer.write(FrameTrace(frame=1, stage="大厅", round_no=None, timestamp_ms=1))
    assert re.fullmatch(r"\d{8}_\d{6}", writer.session_dir.name)
    assert (writer.session_dir / "trace.jsonl").is_file()


def test_json_safe_does_not_leak_paths_or_objects():
    class Box:
        pass

    converted = json_safe({"box": (1, 2), "obj": Box()})
    assert converted["box"] == [1, 2]
    assert isinstance(converted["obj"], str)
