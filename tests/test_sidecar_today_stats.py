# -*- coding: utf-8 -*-
"""今日看板路由：core 按 module_id 分发到 `ActivityModule.read_today_stats` 钩子。

core 不持有任何模块的 schema；读侧实现住各活动插件。导入 core.sidecar 会拉 maa 等
重依赖，缺失时整文件 SKIP。
"""
from __future__ import annotations

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService  # noqa: F401
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"看板路由测试需要完整运行时依赖：{_ERR}"
)


def test_route_dispatches_to_module_reader(monkeypatch):
    """路由按 module_id 分发到注册表中该模块的 read_today_stats 钩子。

    用假模块而非真实插件：本测试锁的是 core 的分发机制（core 不持任何模块的 schema），
    与具体模块的读侧实现无关。
    """
    class _Stub:
        @classmethod
        def read_today_stats(cls):
            return {"bucket": "2026-01-01", "summary": {"games": 6}, "games": []}

    monkeypatch.setitem(sc.MODULE_REGISTRY, "probe", _Stub)
    svc = object.__new__(SidecarService)  # 只测纯路由，不拉完整服务
    ok, payload, err = SidecarService.get_today_stats(svc, {"module_id": "probe"})
    assert ok and err is None
    assert payload["summary"]["games"] == 6


def test_route_unknown_module_is_empty_board(monkeypatch):
    """module_id 未注册 → 空看板（不抛错；删除模块后的健壮性兜底）。"""
    monkeypatch.delitem(sc.MODULE_REGISTRY, "missing_module", raising=False)
    svc = object.__new__(SidecarService)
    ok, payload, err = SidecarService.get_today_stats(svc, {"module_id": "missing_module"})
    assert ok and err is None
    assert payload == {"bucket": None, "summary": None, "games": []}


def test_route_hookless_module_is_empty_board(monkeypatch):
    """已注册但未声明看板钩子的模块 → 空看板（本模块无看板是合法声明）。"""

    class _NoBoard:
        pass

    monkeypatch.setitem(sc.MODULE_REGISTRY, "no_board", _NoBoard)
    svc = object.__new__(SidecarService)
    ok, payload, err = SidecarService.get_today_stats(svc, {"module_id": "no_board"})
    assert ok and err is None
    assert payload == {"bucket": None, "summary": None, "games": []}
