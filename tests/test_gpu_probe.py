# -*- coding: utf-8 -*-
"""显卡探针的回归锁：停顿抓拍的时序与产物。

抓拍要能回答「我方线程卡在哪」，就必须赶在 run 还在飞的时候把栈落盘——
外部 nvidia-smi 在驱动停摆期自己会被卡住（生产实测 844ms），所以栈优先、
且抓拍不占盯梢线程。正常短 run 与已结束的 run 都不得触发抓拍。
"""
import threading
import time

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush import gpu_probe
from maaracing_master.plugins.speedrush.gpu_probe import GpuProbe


def setup_function():
    dml_lock.RUN_WATCH.exit()          # 防御清场：防其他用例泄漏在飞标记


def teardown_function():
    dml_lock.RUN_WATCH.exit()


def _join_dump(p: GpuProbe) -> None:
    if p._dump_th is not None:
        p._dump_th.join(5.0)
        assert not p._dump_th.is_alive()


def test_stall_watch_dumps_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_probe, "_cmd_output",
                        lambda cmd: f"(外部取证 {cmd[-1]})\n")
    p = GpuProbe()
    p._trace_dir = tmp_path
    p._sink.bind_session(tmp_path, "20260101_000000_1")
    try:
        dml_lock.RUN_WATCH.enter("yolo")
        p._watch(time.perf_counter())               # 刚进 run：未超阈，不拍
        assert p._dumped is None
        time.sleep(0.25)
        p._watch(time.perf_counter())               # 超阈：拍现场
        _join_dump(p)
        p._watch(time.perf_counter())               # 同一次在飞：不重复拍
        files = list(tmp_path.glob("stall_*_yolo.txt"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        # 三问齐全，且我方线程栈排在外部取证之前
        assert text.index("faulthandler") < text.index("PERFORMANCE")
        assert text.index("PERFORMANCE") < text.index("pmon")
        assert "most recent call first" in text     # 栈真的落进去了
        assert "栈抓拍时在飞: yolo 已" in text
        assert "外部取证结束时在飞: yolo 已" in text
    finally:
        dml_lock.RUN_WATCH.exit()
        _join_dump(p)
        p._sink.flush(2.0)
        res = p._sink.close_session(2.0)
    assert res.ok


def test_stall_dump_stacks_first_without_blocking_watch(tmp_path, monkeypatch):
    """外部取证卡住时：盯梢立刻返回，且栈已落盘（旧实现两条都不成立）。"""
    gate = threading.Event()
    external_started = threading.Event()

    def stuck_cmd(cmd):
        external_started.set()
        gate.wait(3.0)
        return "(外部取证卡住)\n"

    monkeypatch.setattr(gpu_probe, "_cmd_output", stuck_cmd)
    p = GpuProbe()
    p._trace_dir = tmp_path
    dml_lock.RUN_WATCH.enter("moge")
    try:
        time.sleep(0.25)
        t0 = time.perf_counter()
        p._watch(time.perf_counter())
        assert time.perf_counter() - t0 < 0.1       # 盯梢不等外部调用
        assert external_started.wait(2.0)
        files = list(tmp_path.glob("stall_*_moge.txt"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "most recent call first" in text     # 外部还卡着，栈已在盘上
        assert "外部取证卡住" not in text
    finally:
        gate.set()
        dml_lock.RUN_WATCH.exit()
        _join_dump(p)


def test_stall_watch_fires_again_for_new_run(tmp_path, monkeypatch):
    """一场对局停顿 1~2 次：去重只压同一次在飞，不得把哨兵永久关掉。"""
    monkeypatch.setattr(gpu_probe, "_cmd_output", lambda cmd: "(外部取证)\n")
    p = GpuProbe()
    p._trace_dir = tmp_path
    try:
        for _ in range(2):
            dml_lock.RUN_WATCH.enter("yolo")
            time.sleep(0.25)
            p._watch(time.perf_counter())
            _join_dump(p)
            dml_lock.RUN_WATCH.exit()
        assert len(list(tmp_path.glob("stall_*_yolo.txt"))) == 2
    finally:
        dml_lock.RUN_WATCH.exit()
        _join_dump(p)


def test_stall_watch_resets_after_run_ends(tmp_path):
    p = GpuProbe()
    p._trace_dir = tmp_path
    dml_lock.RUN_WATCH.enter("moge")
    dml_lock.RUN_WATCH.exit()
    p._watch(time.perf_counter())                   # 已不在飞：不拍
    assert p._dumped is None
    assert p._dump_th is None
    assert list(tmp_path.glob("stall_*")) == []