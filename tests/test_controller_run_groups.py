# -*- coding: utf-8 -*-
"""controller 生命周期归组：启动行入「运行启动」组、收尾行入「运行收尾」组。

真机反馈（2026-09-21）：配置注入/找到窗口/静音/连接/WGC/音量恢复/结束后行为这批
行散作裸行。定案：controller 以 session 组收拢两段生命周期，异常路径组以 failure
收尾（契约 §8 终态：卡片只来自组，散行只剩有意无组的跨切面行）。

手段：object.__new__ 绕开构造器 + 记录桩 logger（带 group()）+ 打桩外设
（create_module/wgc/手柄/音量），只断言分组形状与顺序，不测各子系统行为。
"""
from __future__ import annotations

import threading
import types

import pytest

ctl = pytest.importorskip("maaracing_master.core.controller")


class _Recorder:
    """带 group() 的记录桩：log 收 (组, 级别, 文本)，group() 返回可 end 的假句柄。"""

    def __init__(self):
        self.events = []
        self._n = 0

    def log(self, msg, level="INFO", channel=None, *, group_id=None, fields=None):
        self.events.append(("log", group_id, level, msg))

    def group(self, title, kind="session", channel=None):
        self._n += 1
        gid = f"g-{self._n}"
        rec = self
        outer = {"err": False, "warn": False}

        class _H:
            id = gid

            def log(self, msg, level="INFO", *, fields=None):
                if level == "ERROR":
                    outer["err"] = True
                elif level == "WARNING":
                    outer["warn"] = True
                rec.events.append(("log", gid, level, msg))

            def end(self, outcome=None):
                o = outcome or ("failure" if outer["err"]
                                else "warning" if outer["warn"] else "success")
                rec.events.append(("group_end", gid, o))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        rec.events.append(("group_start", gid, kind, title))
        return _H()


class _FakeModule:
    STAGE_ORDER = ["待机"]

    def __init__(self):
        self.calls = []
        self.on_start = None

    def required_capabilities(self, config):
        return set()

    def set_module_config(self, cfg):
        self.calls.append(("config", dict(cfg)))

    def start(self, start_from):
        self.calls.append(("start", start_from))
        if self.on_start:
            self.on_start()

    def cleanup(self):
        self.calls.append(("cleanup",))


@pytest.fixture
def env(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(ctl, "logger", rec)
    c = object.__new__(ctl.MaaRacingMasterController)
    c._lifecycle_lock = threading.Lock()
    c.stop_event = threading.Event()
    c._active_module = None
    c._running = False
    c._manual_stop = False
    c._mute_game_enabled = False
    c._auto_close_game = False
    c._last_run_natural = False
    c._hwnd = 12345          # 跳过真实 connect 路径
    c._ctx = None
    c._wgc_capture = None
    c.controller = None
    c._destroy_gpad = lambda: None
    c._stop_wgc_capture = lambda: None
    c._start_wgc_capture = lambda grp=None: rec.log(
        "WGC 中心采集器已就绪（全模块截图统一走缓存）", "INFO",
        group_id=grp.id if grp is not None else None)
    mod = _FakeModule()
    monkeypatch.setattr(ctl, "create_module", lambda mid, ctx: mod)
    fake_ctx = types.SimpleNamespace(capabilities=set())
    monkeypatch.setattr(type(c), "ctx", property(lambda self: fake_ctx))
    return c, rec, mod


def _markers(rec):
    return [e for e in rec.events if e[0] in ("group_start", "group_end")]


def test_happy_path_two_groups_in_order(env):
    c, rec, mod = env
    c.start_module("fake", None, {"max_daily_loops": 50})
    assert rec.events[0] == ("group_start", "g-1", "session", "运行启动")
    assert ("log", "g-1", "INFO",
            "[controller] 模块'fake'配置已注入: ['max_daily_loops']") in rec.events
    assert ("log", "g-1", "INFO",
            "WGC 中心采集器已就绪（全模块截图统一走缓存）") in rec.events
    seq = _markers(rec)
    assert seq == [("group_start", "g-1", "session", "运行启动"),
                   ("group_end", "g-1", "success"),
                   ("group_start", "g-2", "session", "运行收尾"),
                   ("group_end", "g-2", "success")]
    # 正常完成行归收尾组；启动段没有任何裸行
    assert ("log", "g-2", "INFO",
            "运行结束：任务正常完成，按「运行结束后」选项执行") in rec.events
    assert [e for e in rec.events if e[0] == "log" and e[1] is None] == []
    assert mod.calls[0][0] == "config" and ("start", None) in mod.calls


def test_startup_exception_group_ends_failure(env, monkeypatch):
    c, rec, _ = env

    def boom(mid, ctx):
        raise RuntimeError("模块不存在")

    monkeypatch.setattr(ctl, "create_module", boom)
    with pytest.raises(RuntimeError):
        c.start_module("fake")
    assert rec.events[0] == ("group_start", "g-1", "session", "运行启动")
    assert ("group_end", "g-1", "failure") in rec.events


def test_connect_failure_lines_in_start_group(env, monkeypatch):
    c, rec, mod = env
    c._hwnd = 0
    c.controller = None
    monkeypatch.setattr(ctl, "find_game_hwnd", lambda group_id=None: 0)
    c.start_module("fake")
    # 未找到窗口 ERROR 在启动组内 → 组终态自动落 failure，模块不启动
    assert ("log", "g-1", "ERROR", "未找到游戏窗口") in rec.events
    assert ("log", "g-1", "ERROR", "窗口连接失败，模块终止") in rec.events
    assert ("group_end", "g-1", "failure") in rec.events
    assert not any(call[0] == "start" for call in mod.calls)
    # 收尾组照常存在（资源清理不受启动失败豁免）
    assert ("group_start", "g-2", "session", "运行收尾") in rec.events


def test_manual_stop_line_grouped(env):
    c, rec, mod = env
    mod.on_start = lambda: setattr(c, "_manual_stop", True)
    c.start_module("fake")
    assert ("log", "g-2", "INFO",
            "运行结束：手动停止，「运行结束后」开关不生效") in rec.events
    assert ("group_end", "g-2", "success") in rec.events
