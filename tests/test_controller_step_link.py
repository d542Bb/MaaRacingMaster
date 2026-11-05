# -*- coding: utf-8 -*-
"""内置步骤的执行态与资源所有权（ADR-0018）。

锁四件事：
1. `execution_active` 覆盖模块与步骤两种执行体，而 `_running` / `module_active`
   保持"有模块在跑"的原义（GUI 运行显示与看板依赖它们）；
2. 步骤执行期间 `_step_active` 置真、结束复位（含异常路径）；
3. 收尾未完成（框架未收摊）时保留资源、标污染、拒绝下一轮；
4. 虚拟手柄按「谁创建谁负责」销毁：步骤不拆模块建的设备。
"""
from __future__ import annotations

import threading

import pytest

from maaracing_master.core import builtin_steps as bs
from maaracing_master.core import controller as ctl


class _Grp:
    def __init__(self):
        self.id = "g-step"
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO"):
        self.lines.append((level, msg))

    def end(self, outcome=None):
        pass


def _ctl() -> ctl.MaaRacingMasterController:
    c = object.__new__(ctl.MaaRacingMasterController)
    c._running = False
    c._step_active = False
    c._active_module = None
    c._manual_stop = False
    c.stop_event = threading.Event()
    c._last_run_natural = False
    c._last_step_outcome = None
    c._teardown_failed = False
    c._gpad = None
    c._gpad_owner = None
    c._ctx = None
    c._wgc_capture = None
    return c


class _Step:
    NAME = "假步骤"
    seen_active = None

    def __init__(self, app, grp=None):
        self.app = app
        self.last_outcome = "failed"

    def required_resources(self, cfg):
        return frozenset()

    def run(self, cfg, stop_event, log, session):
        _Step.seen_active = self.app._step_active
        self.last_outcome = cfg.get("outcome", "completed")
        return cfg.get("ok", True)


class _Session:
    def __init__(self, app, needs, grp=None):
        self.needs = needs

    def close(self):
        return _Session.close_ok

    close_ok = True


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(ctl, "_run_group", lambda title, kind="session": _Grp())
    monkeypatch.setattr(bs, "BUILTIN_STEPS", {"fake": _Step})
    monkeypatch.setattr(ctl, "StepSession", _Session)
    _Step.seen_active = None
    _Session.close_ok = True
    return _ctl()


def test_execution_active_covers_module_and_step_without_touching_running():
    c = _ctl()
    module = object()

    # 空闲
    assert (c.execution_active, c._running, c.module_active) == (False, False, False)

    # 模块执行中
    c._running, c._active_module = True, module
    assert (c.execution_active, c._running, c.module_active) == (True, True, True)

    # 内置步骤执行中：execution_active 为真，但"有模块在跑"仍为假
    c._running, c._active_module = False, None
    c._step_active = True
    assert (c.execution_active, c._running, c.module_active) == (True, False, False)

    # 两者同时（理论上不该发生，但判据必须仍然为真）
    c._running, c._active_module = True, module
    assert c.execution_active is True


def test_step_active_is_true_during_run_and_reset_after(wired):
    ok = wired.run_builtin_step("fake", {"ok": True, "outcome": "completed"})
    assert ok is True
    assert _Step.seen_active is True, "步骤执行期间 _step_active 必须为真（sleep 依赖它）"
    assert wired._step_active is False
    assert wired.last_step_outcome == "completed"
    assert wired._running is False, "_running 的语义是「有模块在跑」，步骤不得置真"


def test_step_active_resets_even_when_step_raises(wired, monkeypatch):
    class _Boom(_Step):
        def run(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(bs, "BUILTIN_STEPS", {"fake": _Boom})
    with pytest.raises(RuntimeError):
        wired.run_builtin_step("fake", {})
    assert wired._step_active is False


def test_teardown_failure_keeps_resources_and_blocks_next_round(wired, monkeypatch):
    _Session.close_ok = False
    ok = wired.run_builtin_step("fake", {})
    assert ok is False, "收尾未完成必须判失败（覆盖步骤自己的成功）"
    assert wired._teardown_failed is True
    assert wired.last_step_outcome == "completed"  # 步骤本身的结果仍可查

    with pytest.raises(RuntimeError, match="收尾未完成"):
        wired.run_builtin_step("fake", {})
    with pytest.raises(RuntimeError, match="收尾未完成"):
        wired.start_module("whatever")


def test_gpad_is_destroyed_only_when_the_step_created_it():
    c = _ctl()
    destroyed = []
    c._destroy_gpad = lambda grp=None: destroyed.append("gpad")
    c._stop_wgc_capture = lambda: destroyed.append("wgc")
    c._gpad = object()

    c._gpad_owner = "module"
    c.release_step_session()
    assert destroyed == ["wgc"], "模块建的设备不能被子会话拆掉"

    destroyed.clear()
    c._gpad_owner = "step"
    c.release_step_session()
    assert destroyed == ["gpad", "wgc"]


def test_release_closes_context_before_gpad_and_capture():
    c = _ctl()
    order = []
    c._ctx = type("C", (), {"close": lambda self: order.append("ctx")})()
    c._destroy_gpad = lambda grp=None: order.append("gpad")
    c._stop_wgc_capture = lambda: order.append("wgc")
    c._gpad_owner = "step"

    c.release_step_session()
    assert order == ["ctx", "gpad", "wgc"]
    assert c._ctx is None, "Context 关闭后必须置空，否则下次复用已关闭的实例"
