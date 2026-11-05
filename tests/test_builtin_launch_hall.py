# -*- coding: utf-8 -*-
"""启动步骤的三条路径与结局映射（ADR-0018）。

三条路径由**业务配置**决定，不能用"资源集为空"反推：
  wait_hall=false      → 照旧完成（进程起了、窗口交付了，只是不等大厅）
  wait_hall=true + intent → skipped（没连、没认、没清弹窗，不能说"已完成"）
  正常运行              → 按导航结局映射为 completed / stopped / failed
"""
from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest

from maaracing_master.core import builtin_steps as bs


class _App:
    def __init__(self, intent: bool = False, mode: str = "gamepad"):
        self.intent_mode = intent
        self.click_mode = mode
        self.hwnd = 0
        self.adopted: list[int] = []

    def adopt_window(self, hwnd):
        self.adopted.append(hwnd)
        self.hwnd = hwnd

    def forget_window(self):
        self.hwnd = 0

    def request_exit(self):
        pass


class _FakeGraph:
    def __init__(self, run_ok: bool, outcome: str, sink: dict):
        self._run_ok = run_ok
        self.last_outcome = outcome
        self._sink = sink

    def run(self, entry, reached=None, *, deadline_s=None):
        self._sink["run_args"] = (entry, reached, deadline_s)
        return self._run_ok


class _Session:
    def __init__(self, start_ok: bool = True, run_ok: bool = True,
                 outcome: str = "reached"):
        self.needs = frozenset({"game_link"})
        self.start_ok = start_ok
        self.run_ok = run_ok
        self.outcome = outcome
        self.started = False
        self.graph_calls = 0
        self.seen: dict = {}

    def start(self):
        self.started = True
        return self.start_ok

    def graph(self, pipeline, image_dir):
        self.graph_calls += 1
        self.seen["pipeline"] = Path(pipeline)
        self.seen["image"] = Path(image_dir)
        return _FakeGraph(self.run_ok, self.outcome, self.seen)


@pytest.fixture()
def window_ready(monkeypatch):
    """窗口能正常拿到：不真的起进程，也不碰 Win32。"""
    monkeypatch.setattr(bs.game_process, "detect_game_exe",
                        lambda: Path("g112-Win64-Shipping.exe"))
    monkeypatch.setattr(bs.game_process, "game_running", lambda name: False)
    monkeypatch.setattr(bs.game_process, "spawn",
                        lambda exe, args, detached=True: types.SimpleNamespace(pid=1))
    monkeypatch.setattr(bs.game_process, "wait_for_window",
                        lambda name, timeout, stop: 1234)


def _run(cfg=None, app=None, session=None, logs=None):
    logs = logs if logs is not None else []
    app = app or _App()
    session = session or _Session()
    stop = threading.Event()
    step = bs.LaunchGameStep(app)
    ok = step.run(dict(cfg or {}), stop, lambda m, lv="INFO": logs.append((lv, m)), session)
    return ok, step, session, logs


def test_wait_hall_off_keeps_old_semantics(window_ready):
    """关掉大厅等待 = 旧行为：窗口交付即完成，不建任何游戏链路。"""
    ok, step, session, _ = _run({"wait_hall": False})
    assert ok is True
    assert step.last_outcome == "completed"
    assert session.started is False and session.graph_calls == 0


def test_intent_mode_reports_skipped_not_completed(window_ready):
    """仅显示意图：不会确认，也就没验证大厅——结果必须是「跳过」。"""
    ok, step, session, logs = _run({}, app=_App(intent=True))
    assert ok is True
    assert step.last_outcome == "skipped"
    assert session.started is False and session.graph_calls == 0
    assert any(lv == "WARNING" and "跳过进入大厅" in m for lv, m in logs)


def test_normal_run_reaches_hall(window_ready):
    ok, step, session, logs = _run({"hall_timeout_s": 120})
    assert ok is True
    assert step.last_outcome == "completed"
    assert session.seen["run_args"] == (bs.STARTUP_ENTRY, bs.STARTUP_DONE, 120.0)
    assert any("已进入游戏大厅" in m for _lv, m in logs)


def test_deadline_maps_to_failed_with_actionable_log(window_ready):
    ok, step, _s, logs = _run({"hall_timeout_s": 90},
                              session=_Session(run_ok=False, outcome="deadline"))
    assert ok is False
    assert step.last_outcome == "failed"
    assert any(lv == "ERROR" and "未进入游戏大厅" in m for lv, m in logs)
    assert any("关闭「进入大厅」" in m for _lv, m in logs)


def test_stopped_maps_to_warning_not_error(window_ready):
    ok, step, _s, logs = _run({}, session=_Session(run_ok=False, outcome="stopped"))
    assert ok is False
    assert step.last_outcome == "stopped"
    assert [lv for lv, m in logs if "未完成进入大厅" in m] == ["WARNING"]


def test_load_failure_points_at_resource_dir(window_ready):
    ok, step, _s, logs = _run({}, session=_Session(run_ok=False, outcome="load_failed"))
    assert ok is False
    assert step.last_outcome == "failed"
    assert any("core/resources/startup" in m for _lv, m in logs)


def test_session_start_failure_fails_without_running_graph(window_ready):
    ok, step, session, logs = _run({}, session=_Session(start_ok=False))
    assert ok is False
    assert step.last_outcome == "failed"
    assert session.graph_calls == 0
    assert any(lv == "ERROR" and "游戏链路建立失败" in m for lv, m in logs)


def test_required_resources_follow_config():
    step = bs.LaunchGameStep(_App(mode="gamepad"))
    assert step.required_resources({"wait_hall": False}) == frozenset()
    assert step.required_resources({"wait_hall": True}) == frozenset(
        {"game_link", "wgc_capture", "gamepad"})

    real = bs.LaunchGameStep(_App(mode="real"))
    assert real.required_resources({"wait_hall": True}) == frozenset(
        {"game_link", "wgc_capture"})

    intent = bs.LaunchGameStep(_App(intent=True))
    assert intent.required_resources({"wait_hall": True}) == frozenset()
