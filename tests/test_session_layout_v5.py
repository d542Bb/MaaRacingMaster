# -*- coding: utf-8 -*-
"""会话优先布局 v5 的契约锁（目录 / 身份 / 清单 / 帧索引 / 开关）。

锁的是行为契约，不是实现细节：
- 一次进程启动一个会话编号，日志开关不改变它；
- 取证只在 `debug/<会话>/<模块>/<行为>/`，路径不出现轮次后缀；
- 每个行为目录有清单与最小帧索引，回放只认清单；
- 中途开关复用同一会话目录。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from maaracing_master.core import session_layout as sl
from maaracing_master.core.debug import NavigationDebugger
from maaracing_master.core.logger import Logger
from maaracing_master.core.trace_sink import TraceSink
from maaracing_master.plugins.speedrush import session_layout as sr_layout


def _frame(value: int = 1) -> np.ndarray:
    return np.full((16, 16, 3), value % 256, dtype=np.uint8)


def _wait(d: NavigationDebugger, timeout: float = 3.0) -> None:
    assert d._wait_drain(timeout)


def _read_jsonl(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


@pytest.fixture
def session_root(tmp_path):
    root = tmp_path / "debug" / "20260101_000000"
    root.mkdir(parents=True)
    return root


# ---------- 会话身份与开关 ----------


def test_session_token_exists_without_file_logging(tmp_path, monkeypatch):
    monkeypatch.setattr("maaracing_master.core.logger.debug_dir",
                        lambda: tmp_path / "debug")
    log = Logger(tmp_path / "logs")
    token = log.session_token
    assert token
    assert log.session_dir is None          # 日志开关关：没有日志落点
    log.set_file_logging(True)
    assert log.session_token == token
    assert log.session_dir == tmp_path / "logs" / token
    log.set_file_logging(False)
    assert log.session_token == token       # 关日志不换会话
    assert log.session_dir is None


def test_midway_logging_reuses_same_session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("maaracing_master.core.logger.debug_dir",
                        lambda: tmp_path / "debug")
    log = Logger(tmp_path / "logs")
    token = log.session_token
    log.log("before disk", "INFO")
    log.set_file_logging(True)
    log.log("after disk", "INFO")
    log.set_file_logging(False)
    files = list((tmp_path / "logs" / token).glob("MaaRM_*.log"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "before disk" not in body        # 开关前只进内存
    assert "after disk" in body
    assert [p.name for p in (tmp_path / "logs").iterdir()] == [token]


# ---------- 调试取证布局 ----------


def test_debug_layout_writes_behavior_manifest_and_index(session_root):
    d = NavigationDebugger(session_root)
    d.enabled = True
    d.begin_run("20260101_000000_r1")
    try:
        scope = d.open_scope("speedrush", "极速狂飙")
        nav = d.open_segment("nav", "点比赛", peep_fps=1000, save_fps=1000)
        nav.offer(_frame(7), fid=42, ts_ns=123456789)
        _wait(d)
        nav.close("reached")
        _wait(d)
        d.close_segment(scope, "completed")
    finally:
        d.end_run()

    behaviors = sl.list_behavior_manifests(session_root)
    assert [(m, b) for m, b, _, _ in behaviors] == [("speedrush", "nav")]
    _, _, bdir, manifest = behaviors[0]
    assert manifest["schema"] == sl.SESSION_SCHEMA_VERSION
    assert manifest["session"] == session_root.name
    assert manifest["module"] == "speedrush"
    assert manifest["behavior"] == "nav"
    assert manifest["frames_index"] == "frames.jsonl"

    rows = sl.read_frame_index(bdir, manifest)
    assert len(rows) == 1
    assert rows[0]["seq"] > 0          # 会话级流序号（控制事件也占号）
    assert rows[0]["ts_mono_ns"] == 123456789
    assert rows[0]["fid"] == 42
    assert (bdir / rows[0]["file"]).is_file()

    # 会话时间线记行为路径与运行身份，不把帧目录名绑到轮次
    index = _read_jsonl(session_root / "index.jsonl")
    starts = [r for r in index if r["type"] == "segment_start"]
    nav_start = next(r for r in starts if r["owner"] == "nav")
    assert nav_start["behavior"] == "speedrush/nav"
    assert nav_start["run_id"] == "20260101_000000_r1"
    assert all("_r1" not in str(p.relative_to(session_root))
               for p in session_root.rglob("*.jpg"))


def test_debug_enabled_mid_run_still_lands_in_session(session_root):
    d = NavigationDebugger(session_root)
    d.begin_run("20260101_000000_r1")
    try:
        seg = d.open_segment("nav", "进入大厅", peep_fps=1000, save_fps=1000)
        seg.offer(_frame(1), fid=1)
        _wait(d)                       # debug 关：不落盘
        assert not (session_root / "index.jsonl").exists()
        d.enabled = True               # 中途打开调试
        seg.offer(_frame(2), fid=2)
        _wait(d)
        seg.close("reached")
        _wait(d)
    finally:
        d.end_run()
    assert (session_root / "index.jsonl").is_file()
    assert sl.list_behavior_manifests(session_root)


# ---------- 行为清单与回放读取 ----------


def test_list_sessions_reads_only_v5_layout(tmp_path):
    root = tmp_path / "debug"
    sess = root / "20260101_000000"
    sess.mkdir(parents=True)
    sl.write_manifest(sess, "speedrush", "drive", frames_index="frames.jsonl")
    (root / "speedrush").mkdir()      # 旧顶层取证根不是会话
    (root / "not_a_session").mkdir()
    assert sl.list_sessions(root) == ["20260101_000000"]


def test_resolve_behavior_file_blocks_traversal(session_root):
    bdir = sl.behavior_dir(session_root, "speedrush", "drive")
    bdir.mkdir(parents=True)
    (session_root / "outside.jpg").write_bytes(b"x")
    assert sl.resolve_behavior_file(bdir, "../outside.jpg") is None
    assert sl.resolve_behavior_file(bdir, "ok.jpg") is not None


def test_bad_manifest_is_ignored(session_root):
    bdir = sl.behavior_dir(session_root, "speedrush", "drive")
    bdir.mkdir(parents=True)
    (bdir / "manifest.json").write_text('{"schema": 4}', encoding="utf-8")
    assert sl.read_manifest(bdir) is None
    assert sl.list_behavior_manifests(session_root) == []


# ---------- 流水身份字段 ----------


def test_trace_rows_carry_session_and_round(tmp_path):
    sink = TraceSink("demo")
    sink.bind_session(tmp_path, "20260101_000000")
    sink.set_run_id("20260101_000000_r3")
    sink.append({"fid": 1, "x": 2})
    sink.close_session(2.0)
    rows = _read_jsonl(tmp_path / "trace" / "demo.jsonl")
    data = next(r for r in rows if "type" not in r)
    assert data["session"] == "20260101_000000"
    assert data["round"] == 3
    assert data["run_id"] == "20260101_000000_r3"


# ---------- 极速狂飙控制取证 ----------


def test_drive_manifest_lists_segments_and_builds_index(session_root):
    behavior = sr_layout.drive_behavior_dir(session_root)
    seg1 = behavior / "20260101_101010_p1"
    seg2 = behavior / "20260101_101010_p2"
    seg1.mkdir(parents=True)
    seg2.mkdir(parents=True)
    (seg1 / "frames.jsonl").write_text(
        json.dumps({"seq": 1, "ts_ns": 10, "file": "raw/0001_raw.jpg"}) + "\n",
        encoding="utf-8")
    (seg2 / "frames.jsonl").write_text(
        json.dumps({"seq": 1, "ts_ns": 20, "file": "raw/0001_raw.jpg"}) + "\n",
        encoding="utf-8")

    sr_layout.upsert_drive_segment(behavior, round_no=1, phase=1,
                                   stamp="20260101_101010")
    sr_layout.upsert_drive_segment(behavior, round_no=1, phase=2,
                                   stamp="20260101_101010")
    sr_layout.rebuild_drive_index(behavior)

    doc = sl.read_manifest(behavior)
    assert doc["module"] == "speedrush" and doc["behavior"] == "drive"
    assert [s["phase"] for s in doc["segments"]] == [1, 2]
    rows = sl.read_frame_index(behavior, doc)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["file"] == "20260101_101010_p1/raw/0001_raw.jpg"
    assert rows[1]["file"] == "20260101_101010_p2/raw/0001_raw.jpg"
    assert (behavior / "manifest.json").is_file()
