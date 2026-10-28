# -*- coding: utf-8 -*-
"""性能采样器单测（记录流三分方案 §5.2 契约）。

逐条锁死：指标三态（ok/timeout/unavailable/parse 错误的显式区分与粘性）、
低频背景档、事件倾倒的样本唯一落盘（含重叠窗口去重）、窗口中断记
incomplete、环形缓冲随会话切换重置、关闸期样本不进新会话文件、
sidecar 聚合纯函数（资源面覆盖、不可用显式化）。

采样循环用手动 tick 驱动（不起线程）；指标源用实例属性遮蔽注入。
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

from maaracing_master.core.perf_sampler import (
    PerfSampler, build_perf_payload)


def make_sampler(tmp_path, **kw) -> PerfSampler:
    s = PerfSampler(interval_s=0.01, gpu_every=1, keep_every=5,
                    scan_every=10_000, gpu_runner=lambda t: "50, 3000, 8192",
                    **kw)
    s._sample_system = lambda: {"cpu_pct": 12.0, "mem_pct": 50.0,
                                "mem_used_gb": 8.0, "mem_total_gb": 16.0}
    s._sample_procs = lambda: [
        {"pid": 1, "name": "self", "cpu_pct": 30.0, "mem_mb": 200.0}]
    return s


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_sample_shape_and_gpu_ok(tmp_path):
    s = make_sampler(tmp_path)
    s.on_session_open(tmp_path, "t_1")
    sample = s.tick_once()
    assert sample["sample_id"] == 1 and sample["_p"] is False
    assert sample["cpu_pct"] == 12.0 and sample["gpu"]["status"] == "ok"
    assert sample["gpu"]["util_pct"] == 50 and sample["gpu"]["age_s"] is not None
    snap = s.snapshot()
    assert snap["available"] is True and snap["sample_id"] == 1
    assert "_p" not in snap                                # 内部键不外泄
    assert s.on_session_closing(time.monotonic() + 1).ok
    s.stop()


def test_gpu_states_explicit(tmp_path):
    calls = []
    s = make_sampler(tmp_path)
    s.on_session_open(tmp_path, "t_1")
    # 超时态
    s._gpu_runner = lambda t: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=1))
    s.tick_once()
    assert s._gpu["status"] == "timeout"
    # 永久不可用（粘性：之后不再调 runner）
    s._gpu_runner = lambda t: (_ for _ in ()).throw(
        FileNotFoundError("nvidia-smi"))
    s.tick_once()
    assert s._gpu["status"] == "unavailable"
    s._gpu_runner = lambda t: calls.append(1) or "1, 2, 3"
    s.tick_once()
    assert s._gpu["status"] == "unavailable" and not calls   # 粘住不重试
    # 解析失败
    s2 = make_sampler(tmp_path)
    s2.on_session_open(tmp_path, "t_2")
    s2._gpu_runner = lambda t: "garbage"
    s2.tick_once()
    assert s2._gpu["status"] == "error:parse"
    s.on_session_closing(time.monotonic() + 1)
    s2.on_session_closing(time.monotonic() + 1)
    s.stop()
    s2.stop()


def test_bg_tier_and_close_flush_unique(tmp_path):
    s = make_sampler(tmp_path)
    s.on_session_open(tmp_path, "t_1")
    for _ in range(12):
        s.tick_once()
    p = tmp_path / "perf.jsonl"
    during = [r for r in _lines(p) if r.get("type") == "sample"]
    assert [r["sample_id"] for r in during] == [5, 10]      # 低频背景档
    assert s.on_session_closing(time.monotonic() + 1).ok
    rows = _lines(p)
    ids = [r["sample_id"] for r in rows if r.get("type") == "sample"]
    assert sorted(ids) == list(range(1, 13))                # 收尾全量补齐，唯一不重
    assert rows[-1]["type"] == "end"
    s.stop()


def test_event_dump_backfill_window_and_uniqueness(tmp_path):
    s = make_sampler(tmp_path)
    s.on_session_open(tmp_path, "t_1")
    for _ in range(8):
        s.tick_once()                                       # id5 已被背景档落盘
    assert s.record_event("speedrush", "tick_gap", {"gap_ms": 220.0},
                          pre_s=60.0, post_s=0.0) == 1
    s.tick_once()                                           # 处理前置窗 + 本拍在窗内
    time.sleep(0.08)                                        # Windows 粗钟 ~15.6ms：跨量子后窗口才到期
    s.tick_once()                                           # 窗口到期 → 事件记录行
    p = tmp_path / "perf.jsonl"
    rows = _lines(p)
    samples = [r for r in rows if r.get("type") == "sample"]
    ids = [r["sample_id"] for r in samples]
    assert len(ids) == len(set(ids))                        # 每样本只写一次
    evt = next(r for r in rows if r.get("type") == "perf_event")
    assert evt["source"] == "speedrush" and evt["reason"] == "tick_gap"
    assert evt["sample_from"] == 1 and evt["sample_to"] == 10
    assert evt["incomplete"] is False      # 完整窗口（缺失说明可因 pre 窗超环而诚实存在）
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_overlapping_windows_no_duplicate_samples(tmp_path):
    s = make_sampler(tmp_path)
    s.on_session_open(tmp_path, "t_1")
    for _ in range(6):
        s.tick_once()
    s.record_event("speedrush", "tick_gap", None, pre_s=60.0, post_s=0.0)
    s.record_event("speedrush", "stall", None, pre_s=60.0, post_s=0.0)
    s.tick_once()                                           # 两个前置窗一次处理
    time.sleep(0.08)                                        # Windows 粗钟：跨量子后双窗口到期
    s.tick_once()
    rows = _lines(tmp_path / "perf.jsonl")
    ids = [r["sample_id"] for r in rows if r.get("type") == "sample"]
    assert len(ids) == len(set(ids))                        # 重叠去重
    evts = [r for r in rows if r.get("type") == "perf_event"]
    assert sorted(e["event_id"] for e in evts) == [1, 2]    # 各自的事件记录
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_close_interrupts_active_window_incomplete(tmp_path):
    s = make_sampler(tmp_path)
    s.on_session_open(tmp_path, "t_1")
    s.tick_once()
    s.record_event("speedrush", "tick_gap", None, pre_s=60.0, post_s=30.0)
    s.tick_once()                                           # 进入后置窗
    assert s.on_session_closing(time.monotonic() + 1).ok
    rows = _lines(tmp_path / "perf.jsonl")
    evt = next(r for r in rows if r.get("type") == "perf_event")
    assert evt["incomplete"] is True
    assert "截断" in evt["missing"]
    assert rows[-1]["type"] == "end"
    s.stop()


def test_ring_resets_on_session_switch_and_gate_off(tmp_path):
    s = make_sampler(tmp_path)
    for _ in range(3):                                      # 关闸期：只进环不落盘
        s.tick_once()
    assert s.stats()["ring"] == 3
    s.on_session_open(tmp_path, "t_1")
    assert s.stats()["ring"] == 0                           # 环随 token 切换重置
    for _ in range(4):
        s.tick_once()
    assert (tmp_path / "perf.jsonl").exists()
    s.on_session_closing(time.monotonic() + 1)
    for _ in range(2):                                      # 又回到关闸期：环继续攒
        s.tick_once()
    assert s.stats()["ring"] == 6
    s2dir = tmp_path / "s2"
    s2dir.mkdir()
    s.on_session_open(s2dir, "t_2")                         # 新会话重置
    assert s.stats()["ring"] == 0
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_snapshot_unavailable_when_never_ticked():
    s = PerfSampler()
    snap = s.snapshot()
    assert snap["available"] is False and "error" in snap


def test_build_perf_payload_carries_rows_and_flags():
    """聚合只做拼装：领域行由 perf_contract 产出后原样透传，不二次解释。"""
    rows = [{"key": "tick_rate", "label": "驾驶拍率", "kind": "rate",
             "category": "player", "value": 12.4, "display": "12.4 次/秒",
             "level": "ok", "reason": None, "hint": None}]
    ok_snap = {"available": True, "cpu_pct": 12.0, "sample_id": 7,
               "procs": [{"pid": 1, "name": "self", "cpu_pct": 300.0,
                          "mem_mb": 100.0}],
               "gpu": {"status": "ok", "util_pct": 50}, "mem_pct": 50.0,
               "mem_used_gb": 8.0, "mem_total_gb": 16.0}
    p = build_perf_payload(rows, ok_snap)
    assert p["schema_version"] == 3
    assert p["rows"] == rows                                # 领域行原样透传
    assert p["sample_id"] == 7                              # 走势图据此去重推进
    assert p["cpu"]["available"] and p["cpu"]["cores"] > 0  # 资源面来自采样器
    assert 0 < p["cpu"]["load_p50"] < 1
    assert p["gpu"]["util_pct"] == 50
    p2 = build_perf_payload(rows, {"available": False})
    assert p2["cpu"] == {"available": False}                # 不可用显式化
    assert p2["rows"] == rows                               # 监控坏了不影响模块数据
    assert build_perf_payload(None, {"available": False})["sampler_available"] is False
    assert build_perf_payload(None, {"available": False})["rows"] == []
