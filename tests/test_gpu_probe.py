# -*- coding: utf-8 -*-
"""显卡探针单测：在飞标记语义 + 停顿现场抓拍（回归锁 2026-11-07 停顿归因）。"""
import time

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush.gpu_probe import GpuProbe


def setup_function():
    dml_lock.RUN_WATCH.exit()          # 防御清场：防其他用例泄漏在飞标记


def teardown_function():
    dml_lock.RUN_WATCH.exit()


def test_run_watch_marker_semantics():
    dml_lock.RUN_WATCH.enter("yolo")
    since, who = dml_lock.RUN_WATCH.snapshot()
    assert who == "yolo" and since is not None
    time.sleep(0.01)
    since2, _ = dml_lock.RUN_WATCH.snapshot()
    assert since2 == since              # 同一次在飞起点不变
    dml_lock.RUN_WATCH.exit()
    assert dml_lock.RUN_WATCH.snapshot() == (None, "")


def test_stall_watch_dumps_scene(tmp_path):
    p = GpuProbe()
    p._trace_dir = tmp_path
    p._sink.bind_session(tmp_path, "20260101_000000_1")
    try:
        dml_lock.RUN_WATCH.enter("yolo")
        p._watch(time.perf_counter())               # 刚进 run：未超阈，不拍
        assert p._dumped is None
        time.sleep(0.25)
        p._watch(time.perf_counter())               # 超 200ms：拍现场
        assert p._dumped is not None
        p._watch(time.perf_counter())               # 同一次在飞：不重复拍
        files = list(tmp_path.glob("stall_*_yolo.txt"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "PERFORMANCE" in text and "faulthandler" in text
    finally:
        dml_lock.RUN_WATCH.exit()
        p._sink.flush(2.0)
        res = p._sink.close_session(2.0)
    assert res.ok


def test_stall_watch_resets_after_run_ends(tmp_path):
    p = GpuProbe()
    p._trace_dir = tmp_path
    dml_lock.RUN_WATCH.enter("moge")
    dml_lock.RUN_WATCH.exit()
    p._watch(time.perf_counter())                   # 已不在飞：不拍
    assert p._dumped is None
    assert list(tmp_path.glob("stall_*")) == []
