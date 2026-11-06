# -*- coding: utf-8 -*-
"""pad_source 槽位锁定回归锁（speedrush 录制器 / 宏录制器共用）。

锁的是「锁错槽位会把整局录成全零」这条实机教训：唯一已连接槽位直接锁、多槽位
挑第一个有输入活动者、探测超时锁首个并告警；锁定后只读该槽位，不再扫其余槽。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from maaracing_master.core.xinput import PadState
from maaracing_master.plugins.speedrush import pad_source as ps


def _state(index: int, *, lx: int = 0, ly: int = 0, lt: int = 0,
           rt: int = 0, buttons: int = 0) -> PadState:
    return PadState(index=index, packet=1, buttons=buttons,
                    left_trigger=lt, right_trigger=rt,
                    left_stick=(lx, ly), right_stick=(0, 0))


class _Reader:
    """按槽位返回预设状态的假读取器，并记录每次读取的槽位。"""

    def __init__(self, states: dict[int, PadState | None]) -> None:
        self.states = states
        self.calls: list[int] = []

    def read_state(self, index: int) -> PadState | None:
        self.calls.append(index)
        return self.states.get(index)


@pytest.fixture
def reader(monkeypatch) -> _Reader:
    r = _Reader({0: None, 1: None, 2: None, 3: None})
    monkeypatch.setattr(ps, "xinput", r)
    return r


@pytest.fixture
def clock(monkeypatch):
    """可控单调时钟：返回可拨动的 [当前秒]。"""
    now = [0.0]
    monkeypatch.setattr(ps, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


class TestPadActive:
    def test_stick_threshold_is_strict(self):
        assert not ps.pad_active(_state(0, lx=ps.PAD_ACTIVE_THRESHOLD))
        assert ps.pad_active(_state(0, lx=ps.PAD_ACTIVE_THRESHOLD + 1))
        assert ps.pad_active(_state(0, ly=-ps.PAD_ACTIVE_THRESHOLD - 1))

    def test_trigger_threshold_is_strict(self):
        assert not ps.pad_active(_state(0, lt=10, rt=10))
        assert ps.pad_active(_state(0, lt=11))
        assert ps.pad_active(_state(0, rt=11))

    def test_any_button_counts(self):
        assert ps.pad_active(_state(0, buttons=0x0001))


class TestSlotLocking:
    def test_explicit_slot_reads_only_that_slot(self, reader):
        reader.states[2] = _state(2)
        sampler = ps.PadSampler(slot=2)
        st, counted = sampler.poll(probe_deadline=999.0)
        assert st.index == 2
        assert counted is True
        assert reader.calls == [2]

    def test_no_connected_slot_is_a_miss(self, reader, clock):
        sampler = ps.PadSampler()
        assert sampler.poll(probe_deadline=5.0) == (None, True)
        assert sampler.slot is None
        assert reader.calls == [0, 1, 2, 3]

    def test_single_connected_locks_without_input(self, reader, clock):
        logs: list[tuple[str, str]] = []
        reader.states[1] = _state(1)
        sampler = ps.PadSampler(log=lambda msg, level: logs.append((level, msg)))
        st, counted = sampler.poll(probe_deadline=5.0)
        assert (st.index, counted) == (1, True)
        assert sampler.slot == 1
        assert logs and logs[0][0] == "INFO" and "唯一已连接" in logs[0][1]

    def test_multiple_slots_pick_the_active_one(self, reader, clock):
        logs: list[tuple[str, str]] = []
        reader.states[0] = _state(0)                 # 静止的虚拟手柄
        reader.states[2] = _state(2, lx=9000)        # 物理手柄
        sampler = ps.PadSampler(log=lambda msg, level: logs.append((level, msg)))
        st, counted = sampler.poll(probe_deadline=5.0)
        assert (st.index, counted) == (2, True)
        assert sampler.slot == 2
        assert logs and "有输入活动" in logs[0][1]

    def test_multiple_idle_waits_without_counting(self, reader, clock):
        reader.states[0] = _state(0)
        reader.states[3] = _state(3)
        sampler = ps.PadSampler()
        assert sampler.poll(probe_deadline=5.0) == (None, False)
        assert sampler.slot is None

    def test_timeout_locks_first_and_warns(self, reader, clock):
        logs: list[tuple[str, str]] = []
        reader.states[1] = _state(1)
        reader.states[3] = _state(3)
        sampler = ps.PadSampler(log=lambda msg, level: logs.append((level, msg)))
        clock[0] = 5.0
        st, counted = sampler.poll(probe_deadline=5.0)
        assert (st.index, counted) == (1, True)
        assert sampler.slot == 1
        assert logs and logs[0][0] == "WARNING" and "锁定首个" in logs[0][1]

    def test_after_lock_only_locked_slot_is_read(self, reader, clock):
        reader.states[2] = _state(2, lx=9000)
        sampler = ps.PadSampler()
        sampler.poll(probe_deadline=5.0)
        reader.calls.clear()
        sampler.poll(probe_deadline=5.0)
        sampler.poll(probe_deadline=5.0)
        assert reader.calls == [2, 2]
