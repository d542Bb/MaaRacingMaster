# -*- coding: utf-8 -*-
"""启动步骤的路径与结局映射（ADR-0018 / ADR-0020）。

两条路径：
  正常运行            → 起进程、交窗口、跑启动链，按导航结局映射为 completed / stopped / failed
  仅显示意图          → 不执行大厅链路，skipped（没连、没认、没清弹窗，不能说"已完成"）
资源需求与业务结果分开判定，不互相推导。
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
                        lambda exe, args: types.SimpleNamespace(pid=1))
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


def test_running_game_is_adopted_not_relaunched(monkeypatch):
    """游戏已在运行时不拉第二个进程，直接接管现有窗口交给连接层。"""
    spawned: list = []
    monkeypatch.setattr(bs.game_process, "detect_game_exe",
                        lambda: Path("g112-Win64-Shipping.exe"))
    monkeypatch.setattr(bs.game_process, "game_running", lambda name: True)
    monkeypatch.setattr(bs.game_process, "find_window_by_exe", lambda name: 5678)
    monkeypatch.setattr(bs.game_process, "spawn",
                        lambda exe, args: spawned.append(exe) or types.SimpleNamespace(pid=1))
    app = _App()
    ok, step, _s, logs = _run({}, app=app, session=_Session())
    assert ok is True and step.last_outcome == "completed"
    assert spawned == []
    assert app.hwnd == 5678
    assert any("已在运行" in m for _lv, m in logs)


def test_running_game_without_window_waits_for_it(monkeypatch):
    """进程在但窗口还没出（刚双击完、还在加载）：等一轮再交给连接层。"""
    spawned: list = []
    monkeypatch.setattr(bs.game_process, "detect_game_exe",
                        lambda: Path("g112-Win64-Shipping.exe"))
    monkeypatch.setattr(bs.game_process, "game_running", lambda name: True)
    monkeypatch.setattr(bs.game_process, "find_window_by_exe", lambda name: 0)
    monkeypatch.setattr(bs.game_process, "spawn",
                        lambda exe, args: spawned.append(exe) or types.SimpleNamespace(pid=1))
    monkeypatch.setattr(bs.game_process, "wait_for_window",
                        lambda name, timeout, stop: 7777)
    app = _App()
    ok, step, _s, logs = _run({}, app=app, session=_Session())
    assert ok is True and step.last_outcome == "completed"
    assert app.hwnd == 7777 and spawned == []
    assert any("已在运行" in m for _lv, m in logs)


def test_running_game_without_window_never_fakes_completion(monkeypatch):
    """等到超时还是没有窗口 → 本步骤失败并说清真因，不能放行让下游报「链路建立失败」。"""
    monkeypatch.setattr(bs.game_process, "detect_game_exe",
                        lambda: Path("g112-Win64-Shipping.exe"))
    monkeypatch.setattr(bs.game_process, "game_running", lambda name: True)
    monkeypatch.setattr(bs.game_process, "find_window_by_exe", lambda name: 0)
    monkeypatch.setattr(bs.game_process, "wait_for_window",
                        lambda name, timeout, stop: 0)
    app = _App()
    session = _Session()
    ok, step, _s, logs = _run({}, app=app, session=session)
    assert ok is False and step.last_outcome == "failed"
    assert app.hwnd == 0 and session.started is False
    assert any(lv == "ERROR" and "未等到游戏窗口" in m for lv, m in logs)


def test_window_timeout_falls_back_to_launcher(monkeypatch):
    """等窗口超时改走官方启动器再等一轮，不在第一跳就判失败。"""
    launched: list = []
    monkeypatch.setattr(bs.game_process, "detect_game_exe",
                        lambda: Path("g112-Win64-Shipping.exe"))
    monkeypatch.setattr(bs.game_process, "game_running", lambda name: False)
    monkeypatch.setattr(bs.game_process, "spawn",
                        lambda exe, args: launched.append(exe.name) or types.SimpleNamespace(pid=1))
    monkeypatch.setattr(bs.game_process, "detect_launcher_exe", lambda: Path("Launcher.exe"))
    remaining = [0, 4321]  # 第一轮超时，回退后第二轮拿到窗口
    monkeypatch.setattr(bs.game_process, "wait_for_window",
                        lambda name, timeout, stop: remaining.pop(0))
    ok, step, _s, logs = _run({})
    assert ok is True and step.last_outcome == "completed"
    assert launched == ["g112-Win64-Shipping.exe", "Launcher.exe"]
    assert any("改走官方启动器" in m for _lv, m in logs)


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
    assert any("把本步骤移出任务列表" in m for _lv, m in logs)


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


def test_required_resources_follow_click_mode():
    """资源需求只由点击方式与运行模式决定，配置面里没有能改它的项。"""
    assert bs.LaunchGameStep(_App(mode="gamepad")).required_resources({}) == frozenset(
        {"game_link", "wgc_capture", "gamepad"})
    assert bs.LaunchGameStep(_App(mode="real")).required_resources({}) == frozenset(
        {"game_link", "wgc_capture"})
    assert bs.LaunchGameStep(_App(intent=True)).required_resources({}) == frozenset()
