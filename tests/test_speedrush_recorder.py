# -*- coding: utf-8 -*-
"""speedrush 驾驶录制器的回归锁。

覆盖：帧落盘与索引、手柄样本落盘（注入假手柄，不依赖真实硬件）、meta 计数、
队列满时的丢帧计数（反压纪律）、stop 幂等。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from maaracing_master.core import xinput
from maaracing_master.plugins.speedrush.recorder import (
    SCHEMA_VERSION,
    DriveRecorder,
    make_session_dir,
)


def _frame(w: int = 64, h: int = 48) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fake_pad(index: int):
    """注入用假手柄：槽 0 有连、其余无。"""
    if index != 0:
        return None
    return xinput.PadState(
        index=0, packet=7, buttons=xinput.BUTTON_A,
        left_trigger=210, right_trigger=0,
        left_stick=(-12000, 300), right_stick=(0, 0))


@pytest.fixture(autouse=True)
def _no_real_pad(monkeypatch):
    """默认隔离真实 XInput：除非用例自行覆盖，一律视为无手柄，避免测试依赖硬件。"""
    monkeypatch.setattr(xinput, "read_state", lambda index: None)


def test_records_frames_and_writes_index(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s1", pad_poll_hz=50.0)
    rec.start()
    for i in range(5):
        rec.record_frame(_frame(), frame_id=100 + i, ts_ns=1_000_000 + i * 33_000_000)
    rec.stop()

    lines = _read_jsonl(tmp_path / "s1" / "frames.jsonl")
    assert len(lines) == 5
    for i, row in enumerate(lines):
        assert row["seq"] == i + 1
        # frame_id 与 ts_ns 必须原样落盘：跨源对齐就靠它们，丢了就退化成"按序号猜"
        assert row["frame_id"] == 100 + i
        assert row["ts_ns"] == 1_000_000 + i * 33_000_000
        assert (tmp_path / "s1" / "frames" / row["file"]).is_file()


def test_records_pad_samples(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(xinput, "read_state", _fake_pad)
    rec = DriveRecorder(tmp_path / "s2", pad_poll_hz=200.0)
    rec.start()
    # 给采样线程留出产样本的时间（200Hz × 0.15s ≈ 30 条，取宽限判据）
    import time
    deadline = time.monotonic() + 2.0
    while rec.stats["pad_samples"] < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    rec.stop()

    rows = _read_jsonl(tmp_path / "s2" / "pads.jsonl")
    assert len(rows) >= 3, "手柄采样线程未产出样本"
    for row in rows:
        assert row["lx"] == -12000 and row["ly"] == 300
        assert row["lt"] == 210 and row["rt"] == 0
        assert row["btn"] == xinput.BUTTON_A
        assert row["ts_ns"] > 0


def test_meta_counts_and_schema(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s3", jpeg_quality=80, pad_poll_hz=50.0)
    rec.start()
    rec.record_frame(_frame(w=32, h=16), frame_id=1, ts_ns=123)
    rec.stop(reason="drive_phase_end")

    meta = json.loads((tmp_path / "s3" / "meta.json").read_text(encoding="utf-8"))
    assert meta["schema"] == SCHEMA_VERSION
    assert meta["frames_written"] == 1
    assert meta["frames_dropped"] == 0
    assert meta["jpeg_quality"] == 80
    assert meta["frame_w"] == 32 and meta["frame_h"] == 16
    assert meta["stop_reason"] == "drive_phase_end"
    assert meta["monotonic_start_ns"] > 0


def test_queue_full_counts_drop_without_blocking(tmp_path) -> None:
    """反压纪律：队列满时丢帧计数，绝不阻塞采集侧。"""
    rec = DriveRecorder(tmp_path / "s4")
    rec._running = True  # 只测入队路径，不起后台线程
    for _ in range(rec._frame_q.maxsize):
        rec._frame_q.put_nowait((0, 0, 0, 0.0, _frame(2, 2)))
    rec.record_frame(_frame(2, 2), frame_id=1, ts_ns=1)  # 队满 → 应计数而非等待
    assert rec._frames_dropped == 1
    assert rec._frames_written == 0


def test_stop_is_idempotent(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s5")
    rec.start()
    rec.record_frame(_frame(), frame_id=1, ts_ns=1)
    rec.stop()
    rec.stop()  # 重复调用不得抛异常、不得重写 meta
    assert rec.stats["frames_written"] == 1


def test_record_after_stop_is_ignored(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s6")
    rec.start()
    rec.stop()
    rec.record_frame(_frame(), frame_id=1, ts_ns=1)
    assert rec.stats["frames_written"] == 0
    assert rec.stats["frames_dropped"] == 0


def test_make_session_dir_uses_timestamp(tmp_path) -> None:
    d = make_session_dir(tmp_path)
    assert d.parent == tmp_path
    assert len(d.name) == len("20260916_120000")