# -*- coding: utf-8 -*-
"""core/cpu_time.py 平台原语锁定。

只依赖标准库，因此在 CI（只装 pytest、无 maa/opencv）里也会**真实执行**，
不像鉴宝模块的测试那样整文件跳过。

这里锁的是一个具体事故：FILETIME 的 tick 是 100 纳秒，把它写成 `100e-7` 秒
（= 1e-5）会让 CPU 秒数放大 100 倍，而放大后的数字依然"看着像回事"——
唯一能抓它的方式就是与 time.process_time() 对拍，所以这条对拍本身就是测试的重点。
"""
from __future__ import annotations

import sys
import time

import pytest

from maaracing_master.core.cpu_time import IS_WINDOWS, process_cpu_seconds


def test_module_imports_on_any_platform():
    """非 Windows 导入不得抛（CI 是 Linux runner）。"""
    assert IS_WINDOWS == (sys.platform == "win32")


@pytest.mark.skipif(not IS_WINDOWS, reason="GetProcessTimes 仅 Windows")
def test_matches_stdlib_process_time():
    """与 time.process_time() 对拍：单线程烧 0.2 秒，两者增量必须同量级。

    容差给到 0.5~2.0 倍——足以放过调度噪声，又能在单位错一个数量级时立刻变红。
    """
    a, ta = process_cpu_seconds(), time.process_time()
    assert a is not None, "Windows 上 GetProcessTimes 不应取不到"
    deadline = time.perf_counter() + 0.2
    x = 0
    while time.perf_counter() < deadline:
        x += x + 1
    d_ctypes = process_cpu_seconds() - a
    d_stdlib = time.process_time() - ta
    assert d_stdlib > 0.05, f"预热不足，对拍无效：{d_stdlib}"
    assert 0.5 <= d_ctypes / d_stdlib <= 2.0, (
        f"ctypes 增量 {d_ctypes:.4f}s vs stdlib {d_stdlib:.4f}s — 比值 "
        f"{d_ctypes / d_stdlib:.2f}，疑似 FILETIME tick 单位错")


@pytest.mark.skipif(not IS_WINDOWS, reason="GetProcessTimes 仅 Windows")
def test_monotonic_and_repeatable():
    """累计 CPU 时间只增不减；连续调用同值（无进度时不应抖动）。"""
    a = process_cpu_seconds()
    b = process_cpu_seconds()
    assert b >= a
    assert abs(b - a) < 0.05
