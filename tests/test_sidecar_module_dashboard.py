# -*- coding: utf-8 -*-
"""数据看板路由：新契约、旧模块兼容桥、错误分支与「不建实例」。

导入 core.sidecar 会拉 maa 等重依赖；缺失时整文件 SKIP（同其他 sidecar 行为锁）。
"""
from __future__ import annotations

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.base import ActivityModule
    from maaracing_master.core.dashboard_contract import DASHBOARD_CONTRACT_VERSION
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"看板路由测试需要完整运行时依赖：{_ERR}"
)


class _NoInstantiate(ActivityModule):
    """构造即失败：只要路由通过，就说明看板取数没有建实例。"""

    def __init__(self, ctx=None):  # pragma: no cover —— 被调用即测试失败
        raise AssertionError("看板路由不得构造模块实例")


class _NewDashboard(_NoInstantiate):
    ID = "new_dashboard"
    NAME = "新看板"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION

    @classmethod
    def read_dashboard(cls):
        return {
            "version": DASHBOARD_CONTRACT_VERSION,
            "title": "测试看板",
            "blocks": [{"type": "text", "text": "ok"}],
        }


class _LegacyDashboard(_NoInstantiate):
    ID = "legacy_dashboard"
    NAME = "旧看板"

    @classmethod
    def read_today_stats(cls):
        return {"bucket": "2026-11-02", "summary": None, "games": []}


class _NoContent(_NoInstantiate):
    ID = "no_content"
    NAME = "无内容"
    HAS_DASHBOARD = True


class _Reserved(_NoInstantiate):
    ID = "reserved"
    NAME = "未实现看板"
    DASHBOARD_CONTRACT = 0


class _BadVersion(_NoInstantiate):
    ID = "bad_version"
    NAME = "坏版本"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION + 1

    @classmethod
    def read_dashboard(cls):
        return {"version": DASHBOARD_CONTRACT_VERSION, "blocks": []}


class _MissingImpl(_NoInstantiate):
    ID = "missing_impl"
    NAME = "缺实现"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION


class _Raises(_NoInstantiate):
    ID = "raises"
    NAME = "抛错"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION

    @classmethod
    def read_dashboard(cls):
        raise RuntimeError("boom")


def _svc(monkeypatch, modules):
    monkeypatch.setattr(sc, "MODULE_REGISTRY", modules)
    return object.__new__(SidecarService)


def test_new_contract_dashboard_is_normalized(monkeypatch):
    svc = _svc(monkeypatch, {"new_dashboard": _NewDashboard})
    ok, data, err = svc.get_module_dashboard({"module_id": "new_dashboard"})
    assert ok and err is None
    assert data["title"] == "测试看板"
    assert data["blocks"] == [{"type": "text", "text": "ok"}]


def test_legacy_today_stats_bridge(monkeypatch):
    """冻结鉴宝的路径：不改模块代码，core 把今日统计包成兼容块。"""
    svc = _svc(monkeypatch, {"legacy_dashboard": _LegacyDashboard})
    ok, data, err = svc.get_module_dashboard({"module_id": "legacy_dashboard"})
    assert ok and err is None
    assert data["title"] == "今日看板"
    assert data["blocks"][0]["type"] == "today_stats"
    assert data["blocks"][0]["data"]["bucket"] == "2026-11-02"


def test_has_dashboard_without_content_returns_empty_blocks(monkeypatch):
    svc = _svc(monkeypatch, {"no_content": _NoContent})
    ok, data, err = svc.get_module_dashboard({"module_id": "no_content"})
    assert ok and err is None
    assert data["blocks"] == []


def test_zero_contract_reports_not_implemented(monkeypatch):
    """版本 0 是已登记未实现：路由给出明确原因，不当成普通「未声明」。"""
    svc = _svc(monkeypatch, {"reserved": _Reserved})
    ok, data, err = svc.get_module_dashboard({"module_id": "reserved"})
    assert not ok and data is None
    assert "尚未实现" in err


def test_real_speedrush_dashboard_is_reserved(monkeypatch):
    """真极速狂飙当前停在版本 0：路由拒绝，前端候选集不会包含它。"""
    svc = object.__new__(SidecarService)
    ok, data, err = svc.get_module_dashboard({"module_id": "speedrush"})
    assert not ok and data is None
    assert "尚未实现" in err


def test_unsupported_contract_version_rejected(monkeypatch):
    svc = _svc(monkeypatch, {"bad_version": _BadVersion})
    ok, data, err = svc.get_module_dashboard({"module_id": "bad_version"})
    assert not ok and data is None
    assert "版本不支持" in err


def test_declared_contract_without_read_dashboard_rejected(monkeypatch):
    svc = _svc(monkeypatch, {"missing_impl": _MissingImpl})
    ok, data, err = svc.get_module_dashboard({"module_id": "missing_impl"})
    assert not ok and data is None
    assert "未实现 read_dashboard" in err


def test_read_dashboard_exception_is_reported(monkeypatch):
    svc = _svc(monkeypatch, {"raises": _Raises})
    ok, data, err = svc.get_module_dashboard({"module_id": "raises"})
    assert not ok and data is None
    assert "读看板失败" in err


def test_builtin_step_rejected(monkeypatch):
    svc = _svc(monkeypatch, {})
    ok, data, err = svc.get_module_dashboard({"module_id": "launch_game"})
    assert not ok and data is None
    assert "内置步骤" in err


def test_unknown_module_rejected(monkeypatch):
    svc = _svc(monkeypatch, {})
    ok, data, err = svc.get_module_dashboard({"module_id": "no_such_module"})
    assert not ok and data is None
    assert "模块不存在" in err


def test_real_treasure_uses_legacy_bridge():
    """真鉴宝不实现 read_dashboard；路由仍能取到 today_stats 兼容块。"""
    svc = object.__new__(SidecarService)
    ok, data, err = svc.get_module_dashboard({"module_id": "treasure"})
    assert ok and err is None
    assert data["blocks"][0]["type"] == "today_stats"
