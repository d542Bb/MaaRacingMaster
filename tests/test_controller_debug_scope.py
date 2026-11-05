# -*- coding: utf-8 -*-
"""控制器接入运行级调试流（P0）：作用域必须在上下文/会话释放之前收口。"""
from __future__ import annotations

import threading
import types

import pytest

ctl = pytest.importorskip("maaracing_master.core.controller")
from maaracing_master.core.debug import NavigationDebugger


class _Recorder:
    def __init__(self):
        self.events = []
        self._n = 0

    def log(self, msg, level="INFO", channel=None, *, group_id=None, fields=None):
        self.events.append(("log", group_id, level, msg))

    def group(self, title, kind="session", channel=None):
        self._n += 1
        gid = f"g-{self._n}"

        class _H:
            id = gid

            def log(self, msg, level="INFO", *, fields=None):
                pass

            def end(self, outcome=None):
                pass

        return _H()


class _FakeModule:
    STAGE_ORDER = ["待机"]

    def required_capabilities(self, config):
        return set()

    def set_module_config(self, cfg):
        pass

    def start(self, start_from):
        pass

    def cleanup(self):
        pass


def _controller(monkeypatch, tmp_path, order):
    rec = _Recorder()
    monkeypatch.setattr(ctl, "logger", rec)
    c = object.__new__(ctl.MaaRacingMasterController)
    c._lifecycle_lock = threading.Lock()
    c.stop_event = threading.Event()
    c._active_module = None
    c._running = False
    c._manual_stop = False
    c._mute_game_enabled = False
    c._last_run_natural = False
    c._last_step_outcome = None
    c._teardown_failed = False
    c._step_active = False
    c._hwnd = 12345
    c.controller = object()
    c._destroy_gpad = lambda grp=None: None
    c._stop_wgc_capture = lambda: order.append("stop_capture")
    c._start_wgc_capture = lambda grp=None: None
    c._ctx = types.SimpleNamespace(close=lambda: order.append("ctx_close"))
    monkeypatch.setattr(ctl, "create_module", lambda mid, ctx: _FakeModule())
    monkeypatch.setattr(type(c), "ctx",
                        property(lambda self: types.SimpleNamespace(capabilities=set())))
    c.debug = NavigationDebugger(tmp_path)
    c.debug.begin_run("ctl_test")
    orig_close = c.debug.close_segment

    def _close(seg, outcome="completed"):
        order.append(("scope_close", outcome))
        return orig_close(seg, outcome)

    monkeypatch.setattr(c.debug, "close_segment", _close)
    return c


def test_module_scope_closes_before_ctx(monkeypatch, tmp_path):
    order = []
    c = _controller(monkeypatch, tmp_path, order)
    try:
        c.start_module("fake", None, {})
        assert order[0][0] == "scope_close"
        assert order.index("ctx_close") > order.index(order[0])
        assert c.debug.status()["depth"] == 0
    finally:
        c.debug.end_run()


def test_module_save_declaration_reaches_scope(monkeypatch, tmp_path):
    """模块声明 DEBUG_SAVE=self 时，统一调试流不应再复制一份帧到运行目录。"""
    order = []
    c = _controller(monkeypatch, tmp_path, order)

    class _SelfModule(_FakeModule):
        DEBUG_SAVE = "self"

    monkeypatch.setattr(ctl, "create_module", lambda mid, ctx: _SelfModule())
    seen = {}
    orig_open = c.debug.open_scope

    def _open(owner, label, **kw):
        seen.update(kw)
        return orig_open(owner, label, **kw)

    monkeypatch.setattr(c.debug, "open_scope", _open)
    try:
        c.start_module("fake", None, {})
        assert seen.get("save") == "self"
    finally:
        c.debug.end_run()


def test_builtin_scope_closes_before_session(monkeypatch, tmp_path):
    order = []
    c = _controller(monkeypatch, tmp_path, order)

    class _Step:
        ID = "fake_step"
        NAME = "假步骤"

        def __init__(self, app, grp=None):
            self.last_outcome = "completed"

        def required_resources(self, config):
            return frozenset()

        def run(self, cfg, stop_event, log, session):
            return True

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            order.append("session_close")
            return True

    monkeypatch.setattr(ctl, "StepSession", _Session)
    from maaracing_master.core import builtin_steps
    monkeypatch.setitem(builtin_steps.BUILTIN_STEPS, "fake_step", _Step)
    try:
        assert c.run_builtin_step("fake_step", {})
        assert order[0][0] == "scope_close"
        assert order.index("session_close") > order.index(order[0])
        assert c.debug.status()["depth"] == 0
    finally:
        c.debug.end_run()
