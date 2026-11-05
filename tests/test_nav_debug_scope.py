# -*- coding: utf-8 -*-
"""跳转图接入运行级调试流（P0）：导航段生命周期与状态发布。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ng = pytest.importorskip("maaracing_master.core.nav_graph")
from maaracing_master.core.debug import NavigationDebugger


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
        lines = (tmp_path / "nav_test" / "index.jsonl").read_text(encoding="utf-8")
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
