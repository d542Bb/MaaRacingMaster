# -*- coding: utf-8 -*-
"""分层红线：退役插件命名空间的冻结护栏。

归档区（`plugins/_archive/`）不参与默认真源扫描，但其命名空间仍属业务层——
core 真源占用或点名这些前缀同样是分层红线违规。本文件锁住这份冻结名单的判定。
"""
from __future__ import annotations

from pathlib import Path

from tools.pipeline import truth as ct


def _fake_core(file_name: str) -> Path:
    return ct.CORE_PIPELINE_DIR / file_name


def test_core_reference_to_retired_namespace_is_flagged():
    graph = {
        "global.hall": {"next": ["treasure.foo.dwell"]},
        "treasure.foo.dwell": {},
    }
    origin = {
        "global.hall": _fake_core("core.json"),
        "treasure.foo.dwell": Path("plugins/_archive/treasure/resources/pipeline/t.json"),
    }
    problems = ct.namespace_checks(graph, origin)
    assert any("引用模块节点" in p for p in problems), problems


def test_core_occupying_retired_namespace_is_flagged():
    graph = {"treasure.hall_dwell": {}}
    origin = {"treasure.hall_dwell": _fake_core("core.json")}
    problems = ct.namespace_checks(graph, origin)
    assert any("占用模块命名空间" in p for p in problems), problems
