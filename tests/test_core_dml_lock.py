# -*- coding: utf-8 -*-
"""DML 错峰节拍原语（core.dml_lock._TickBeat）的回归锁。

深度 worker 只在控制拍刚放下 DML 锁的节拍上开工，这一层必须满足三条：
初始安静（没有控制拍时不误触发）、note 之后等拍者立刻醒且序号推进、
超时按给定上限返回（worker 不得被一个停住的控制拍卡死）。
"""
from __future__ import annotations

import threading
import time

from maaracing_master.core import dml_lock


def test_tick_beat_starts_quiet():
    beat = dml_lock._TickBeat()
    seq, t = beat.snapshot()
    assert seq == 0
    assert t == float("-inf")


def test_tick_beat_note_wakes_waiter_and_advances_seq():
    beat = dml_lock._TickBeat()
    woke = []
    th = threading.Thread(target=lambda: woke.append(beat.wait_newer(0, 2.0)))
    th.start()
    time.sleep(0.02)
    beat.note()
    th.join(2.0)
    assert not th.is_alive()
    assert woke == [True]
    seq, t = beat.snapshot()
    assert seq == 1
    assert t > 0.0


def test_tick_beat_note_before_wait_is_not_lost():
    """先发拍、后等拍：序号已前进，等拍者必须立即返回（无丢失唤醒）。"""
    beat = dml_lock._TickBeat()
    beat.note()
    assert beat.wait_newer(0, 0.1) is True


def test_tick_beat_wait_newer_times_out():
    beat = dml_lock._TickBeat()
    beat.note()
    seq, _ = beat.snapshot()
    t0 = time.perf_counter()
    assert beat.wait_newer(seq, 0.05) is False
    assert time.perf_counter() - t0 >= 0.04
