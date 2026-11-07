# -*- coding: utf-8 -*-
"""跳转图接入运行级调试流（P0）：导航段生命周期与状态发布。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

ng = pytest.importorskip("maaracing_master.core.nav_graph")
from maaracing_master.core.debug import NavigationDebugger  # noqa: E402 —— importorskip 须先于被门模块导入生效


def _graph(tmp_path):
    d = NavigationDebugger(tmp_path)
    d.enabled = True
    d.begin_run("nav_test")
    g = object.__new__(ng.NavGraph)
    g.ctx = SimpleNamespace(debug=d)
    g._nav_state = {}
    g._nav_lock = threading.Lock()
    g._clicker = None
    g._outcome = None
    g._log_gid = None          # _debug_scope 在「无上层声明」分支会记 DEBUG
    return g, d


def test_publish_state_and_scope_lifecycle(tmp_path):
    g, d = _graph(tmp_path)
    try:
        with g._debug_scope("进入大厅"):
            assert d.status()["depth"] == 1
            assert d.status()["owner"] == "nav"
            g._publish_nav("startup.boot", phase="hit", template="boot", score=0.97,
                           box=(10, 10, 30, 30))
            g._publish_intent("startup.boot", 20, 20, "pending")
            state = g._debug_state()
            assert state["nav"]["node"] == "startup.boot"
            assert state["nav"]["template"] == "boot"
            assert state["intent"]["point"] == (20.0, 20.0)
            assert d.status()["stage"] == "startup.boot"
        assert d.status()["depth"] == 0
        assert d._wait_drain(2.0)
        lines = (tmp_path / "index.jsonl").read_text(encoding="utf-8")
        assert "segment_start" in lines and "segment_end" in lines
        end = [json.loads(x) for x in lines.splitlines() if '"segment_end"' in x][-1]
        assert end["owner"] == "nav"
        assert end["outcome"] == "failed"     # 未设置结局时按失败收尾，不谎报成功
    finally:
        d.end_run()


def test_pointer_none_without_clicker(tmp_path):
    g, d = _graph(tmp_path)
    try:
        assert g._debug_pointer() is None
    finally:
        d.end_run()


def test_scope_closes_even_on_exception(tmp_path):
    g, d = _graph(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            with g._debug_scope("x"):
                raise RuntimeError("boom")
        assert d.status()["depth"] == 0
    finally:
        d.end_run()


# ---------- A4：内层段继承栈底 scope 的存盘策略 ----------


def test_inner_nav_segment_inherits_scope_save(tmp_path):
    """模块声明 `DEBUG_SAVE="self"` → 内层 nav 段必须继承，不能吃默认 "on"。

    这是本契约的核心缺口：`_debug_scope` 原先不传 save，内层段一律 "on"，
    驾驶时段就会出现 nav 段与模块 writer 双写。
    """
    g, d = _graph(tmp_path)
    try:
        scope = d.open_scope("speedrush", "speedrush", save="self")
        with g._debug_scope("进入驾驶页"):
            assert d._stack[-1].owner == "nav"
            assert d._stack[-1].save == "self"
        d.close_segment(scope, "completed")
    finally:
        d.end_run()


def test_scope_save_reads_bottom_scope_not_top(tmp_path):
    """`scope_save()` 读栈底 scope：栈顶正是要按这个值开的新段。"""
    g, d = _graph(tmp_path)
    try:
        assert d.scope_save() is None            # 栈空：无上层声明
        scope = d.open_scope("speedrush", "speedrush", save="self")
        assert d.scope_save() == "self"
        inner = d.open_segment("nav", "x")       # 内层段自身默认仍是 "on"
        assert inner.save == "on"
        assert d.scope_save() == "self"          # 不受栈顶影响
        d.close_segment(inner, "completed")
        d.close_segment(scope, "completed")
        assert d.scope_save() is None
    finally:
        d.end_run()


def test_no_scope_keeps_default_strategy(tmp_path):
    """无上层声明时沿用既有默认（"on"），且不把 None 当 "on" 静默传下去。"""
    g, d = _graph(tmp_path)
    try:
        with g._debug_scope("x"):
            assert d._stack[-1].save == "on"
    finally:
        d.end_run()


# ---------- 显式覆盖：UI 导航要图、驾驶不要图 ----------


def test_explicit_save_overrides_scope_declaration(tmp_path):
    """显式 `save="on"` 优先于栈底 `self`——模块的 UI 导航段要图就走这条。

    驾驶侧不靠这个开关（`probe()` 不开段），所以覆盖只影响调用方声明的那一段，
    不会把整个运行项的归属翻掉。
    """
    g, d = _graph(tmp_path)
    try:
        scope = d.open_scope("speedrush", "speedrush", save="self")
        with g._debug_scope("点确认上阵", save="on"):
            assert d._stack[-1].save == "on"
        with g._debug_scope("驾驶页锚点"):          # 不传：仍继承 self
            assert d._stack[-1].save == "self"
        d.close_segment(scope, "completed")
    finally:
        d.end_run()


def test_nav_segment_save_fps_is_three_hz(tmp_path):
    """nav 段的**落盘**频率统一取 `NAV_SAVE_FPS`（3Hz），预览不受此限。

    与 `RUN_SAMPLE_HZ`（10Hz）分开的理由：UI 导航一局几十段，10Hz 会落出上千张；
    3Hz 够看清页面切换。peep 仍取默认，面板预览要跟手。
    """
    g, d = _graph(tmp_path)
    try:
        assert ng.NAV_SAVE_FPS == pytest.approx(3.0)
        with g._debug_scope("x"):
            seg = d._stack[-1]
            assert seg.save_fps == pytest.approx(3.0)
            assert seg.peep_fps == pytest.approx(10.0)   # RUN_SAMPLE_HZ 不变
    finally:
        d.end_run()
