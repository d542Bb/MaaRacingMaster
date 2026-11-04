# -*- coding: utf-8 -*-
"""紧急停止作用域：只在程序接管操作期检测按键，人工驾驶/录制阶段跳过。"""
from __future__ import annotations

import pytest

ctl = pytest.importorskip("maaracing_master.core.controller")


def _controller_with(module):
    c = object.__new__(ctl.MaaRacingMasterController)
    c._active_module = module
    return c


class _Manual:
    def manual_input_active(self):
        return True


class _Auto:
    def manual_input_active(self):
        return False


class _Broken:
    def manual_input_active(self):
        raise RuntimeError("bad module")


def test_manual_phase_skips_emergency_detection():
    assert _controller_with(_Manual())._manual_input_active() is True


def test_program_phase_keeps_emergency_detection():
    assert _controller_with(_Auto())._manual_input_active() is False


def test_no_module_or_broken_module_defaults_to_detection():
    assert _controller_with(None)._manual_input_active() is False
    assert _controller_with(_Broken())._manual_input_active() is False
