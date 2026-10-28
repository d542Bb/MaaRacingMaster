# -*- coding: utf-8 -*-
"""线程段：清单过滤、占用与心跳读数、身份复用、标签兑现。

对应契约 §3.3（清单可见性）、§3.4（身份契约）、§3.5（心跳）与 §7 验收
第 14、25、26 条。Win32 调用用假提供器替掉，测试不依赖真实线程时序。
"""

from __future__ import annotations

import threading

import pytest

import maaracing_master.core.thread_probe as tp
from maaracing_master.core.thread_probe import ThreadProbe, beat


@pytest.fixture(autouse=True)
def _clean_beats():
    """心跳计数是模块级全局，逐测试清空——否则前一个测试的计数会漏到下一个。"""
    tp._BEATS.clear()
    yield
    tp._BEATS.clear()


class FakeThread:
    def __init__(self, tid: int, name: str):
        self.native_id = tid
        self.name = name


def _patch(monkeypatch, threads, times):
    """把线程枚举与时间读取都换成可控假实现。

    `times` 是 tid → 每次调用依次返回的 (creation, cpu_s) 列表。
    """
    monkeypatch.setattr(tp.threading, "enumerate",
                        lambda: [FakeThread(t, n) for t, n in threads])
    state = {t: list(v) for t, v in times.items()}

    def fake_times(tid):
        seq = state.get(tid)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(tp, "thread_times", fake_times)


def test_default_named_threads_are_skipped(monkeypatch):
    """`Thread-N` 是解释器起的名字，进清单只是噪声。"""
    _patch(monkeypatch, [(1, "Thread-7"), (2, "speedrush-depth-worker")],
           {1: [(100, 0.0)], 2: [(200, 0.0)]})
    p = ThreadProbe(min_age_s=0)
    p.sample(None)
    out = p.sample(None)
    assert [r["name"] for r in out["rows"]] == ["speedrush-depth-worker"]


def test_short_lived_thread_never_listed(monkeypatch):
    """每帧新建即退的线程：第一次看到就记下基线、不进清单；下一拍已消失。"""
    _patch(monkeypatch, [(1, "frame-worker")], {1: [(100, 0.0)]})
    p = ThreadProbe(min_age_s=0)
    assert p.sample(None)["rows"] == []            # 首次：只记基线
    monkeypatch.setattr(tp.threading, "enumerate", lambda: [])   # 已退出
    assert p.sample(None)["rows"] == []
    assert p._first_seen == {} and p._prev_cpu == {}   # 状态清理干净


def test_min_age_filters_threads_younger_than_interval(monkeypatch):
    """活不满一个采样周期的线程不进清单（防两拍连中）。"""
    _patch(monkeypatch, [(1, "w")], {1: [(100, 0.0), (100, 0.0)]})
    p = ThreadProbe(min_age_s=10.0)
    p.sample(None)
    assert p.sample(None)["rows"] == []            # 才 0 秒，不够 10 秒


def test_cpu_pct_and_beat_hz(monkeypatch):
    """占用按两次累计值差分；心跳按调用次数差分。"""
    _patch(monkeypatch, [(1, "worker")], {1: [(100, 0.0), (100, 0.5)]})
    # beat() 按调用线程的系统线程号记账，这里把「当前线程」对齐到假线程
    monkeypatch.setattr(tp.threading, "get_native_id", lambda: 1)
    p = ThreadProbe(min_age_s=0)
    p.sample(None)                                  # 基线
    beat()
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)  # 假装过了一秒
    out = p.sample(80.0)
    row = out["rows"][0]
    assert row["cpu_pct"] == pytest.approx(50.0, abs=0.01)
    assert row["hz"] == pytest.approx(1.0, abs=0.01)
    assert row["identity_ok"] is True
    assert out["total_pct"] == pytest.approx(50.0, abs=0.01)
    assert out["unattributed_pct"] == pytest.approx(30.0, abs=0.01)


def test_thread_without_beat_shows_none(monkeypatch):
    """没调过心跳的线程频率是「—」而不是 0——0 的含义是「调过但停了」。"""
    _patch(monkeypatch, [(1, "quiet")], {1: [(100, 0.0), (100, 0.1)]})
    p = ThreadProbe(min_age_s=0)
    p.sample(None)
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)
    assert p.sample(None)["rows"][0]["hz"] is None


def test_identity_change_invalidates_baseline(monkeypatch):
    """验收 14：同一 tid 换创建时间 → 旧基线作废、不继承历史、不出负差分。"""
    _patch(monkeypatch, [(1, "worker")], {1: [(100, 0.0), (100, 5.0), (200, 0.0)]})
    p = ThreadProbe(min_age_s=0)
    p.sample(None)                                  # 基线（creation=100, cpu=0）
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)
    assert p.sample(None)["rows"][0]["cpu_pct"] == pytest.approx(500.0)
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)
    row = p.sample(None)["rows"][0]                 # creation 变 200
    assert row["identity_ok"] is False
    assert row["cpu_pct"] is None                   # 不给继承来的数字


def test_negative_delta_marks_identity_unknown(monkeypatch):
    """心跳差分出负值说明身份判定漏了：降级为身份未知，不硬算。"""
    _patch(monkeypatch, [(1, "worker")], {1: [(100, 5.0), (100, 1.0)]})
    p = ThreadProbe(min_age_s=0)
    p.sample(None)
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)
    row = p.sample(None)["rows"][0]
    assert row["identity_ok"] is False and row["cpu_pct"] is None


def test_labels_bind_by_name_and_report_missing(monkeypatch):
    """标签按名字匹配；整场没出现的声明收尾时能列出来。"""
    _patch(monkeypatch, [(1, "speedrush-depth-worker")], {1: [(100, 0.0)]})
    p = ThreadProbe(min_age_s=0)
    p.set_labels({"speedrush-depth-worker": "深度链", "lane-obs": "车道观测"})
    p.sample(None)
    out = p.sample(None)
    assert out["rows"][0]["label"] == "深度链"
    assert p.missing_labels() == ["lane-obs"]


def test_unreadable_thread_times_degrades(monkeypatch):
    """读不到线程时间（非 Windows / 线程已退）：只给名字，不崩、不猜占用。"""
    monkeypatch.setattr(tp.threading, "enumerate",
                        lambda: [FakeThread(9, "worker")])
    monkeypatch.setattr(tp, "thread_times", lambda tid: None)
    p = ThreadProbe(min_age_s=0)
    p.sample(None)
    out = p.sample(None)
    assert out["rows"][0]["name"] == "worker"
    assert out["rows"][0]["cpu_pct"] is None
    assert out["total_pct"] is None and out["unattributed_pct"] is None


def test_rows_capped_and_hidden_counted(monkeypatch):
    """上限 10 条按占用排序，超出的合并计数（面板显示「其他 N 个」）。"""
    threads = [(i, f"w{i}") for i in range(15)]
    times = {i: [(100, 0.0), (100, float(i) / 10.0)] for i in range(15)}
    _patch(monkeypatch, threads, times)
    p = ThreadProbe(min_age_s=0, max_rows=10)
    p.sample(None)
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)
    out = p.sample(None)
    assert len(out["rows"]) == 10
    assert out["hidden"] == 5
    assert out["rows"][0]["name"] == "w14"          # 占用最高的排最前


def test_real_threads_smoke():
    """真实线程路径冒烟：能枚举、不抛异常（CI 上非 Windows 也成立）。"""
    p = ThreadProbe(min_age_s=0)
    p.set_labels({threading.current_thread().name: "主线程"})
    p.sample(None)
    out = p.sample(None)
    names = [r["name"] for r in out["rows"]]
    assert threading.current_thread().name in names


def test_unattributed_never_negative(monkeypatch):
    """验收 25：未归因 = 进程占用 − 清单合计，恒 ≥0。

    清单合计超过进程占用时（tid 复用后旧基线没作废、或同一线程被重复计数），
    差值会被钳到 0 而不是给出负数——负数读起来像「清单吃超了」，是纯噪声。
    """
    names = [(1, "a"), (2, "b"), (3, "c")]
    times = {1: [(10, 0.0), (10, 5.0)], 2: [(11, 0.0), (11, 5.0)],
             3: [(12, 0.0), (12, 5.0)]}
    _patch(monkeypatch, names, times)
    p = ThreadProbe(min_age_s=0)
    p.sample(None)                                  # 基线
    monkeypatch.setattr(p, "_prev_mono", p._prev_mono - 1.0)
    out = p.sample(10.0)                            # 清单合计 15 > 进程 10

    listed = sum(r["cpu_pct"] or 0.0 for r in out["rows"])
    assert out["total_pct"] == pytest.approx(listed, abs=0.01)
    assert out["unattributed_pct"] == 0.0
    assert out["unattributed_pct"] >= 0
