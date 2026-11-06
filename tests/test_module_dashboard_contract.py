# -*- coding: utf-8 -*-
"""数据看板能力声明契约：模块自述 → core 下发 → 前端消费。

锁住四件事：

1. **资格位归模块**：``HAS_DASHBOARD`` / ``DASHBOARD_CONTRACT``（版本 > 0）/ 覆盖
   ``read_today_stats`` 任一成立即候选；版本 0 是「已登记、尚未实现」的占位，
   不进入候选集；
2. **元信息单点**：``get_module_info`` 的 ``has_dashboard`` 与资格位一致且为布尔；
3. **下发行齐备**：``get_initial_state.modules[]`` 每项带 ``has_dashboard``；
   内置步骤不参与看板中心，恒 False；
4. **两套入口**：显式标志 / 新契约与旧的今日统计兼容桥并存——显式资格不要求
   今日统计，覆盖今日统计钩子则单独构成旧模块资格。

前端渲染器对后端 block 词表的覆盖由 ``tests/test_frontend_dashboard_center.py``
锁定；本文件只锁资格位与后端下发。

为什么要静态/单元锁：core 只下发一个布尔，渲染与选中状态都在前端；一旦声明、
元信息、列表三处漂移，前端会出现「候选集里有它但点开是空」或「有看板却永远不出现」。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.base import ActivityModule, declares_dashboard, overrides_classmethod
from maaracing_master.core.builtin_steps import BUILTIN_STEPS
from maaracing_master.core.dashboard_contract import DASHBOARD_CONTRACT_VERSION
from maaracing_master.core.registry import MODULE_REGISTRY, get_module_info


def test_base_default_is_false():
    """未声明的模块默认没有看板：新增模块不会因为基类而意外进入看板中心。"""
    assert ActivityModule.HAS_DASHBOARD is False


def test_every_module_declares_a_boolean_flag():
    """资格位：HAS_DASHBOARD 是布尔，DASHBOARD_CONTRACT 是 0 或受支持的整数版本。"""
    for module_id, cls in MODULE_REGISTRY.items():
        legacy = getattr(cls, "HAS_DASHBOARD", False)
        assert isinstance(legacy, bool), f"{module_id}: HAS_DASHBOARD 必须是布尔"
        contract = getattr(cls, "DASHBOARD_CONTRACT", None)
        assert contract is None or type(contract) is int, (
            f"{module_id}: DASHBOARD_CONTRACT 必须是整数版本"
        )
        if contract not in (None, 0):
            assert contract == DASHBOARD_CONTRACT_VERSION, (
                f"{module_id}: 声明了不受支持的看板契约版本 {contract}"
            )
        assert get_module_info(module_id)["has_dashboard"] is declares_dashboard(cls), (
            f"{module_id}: get_module_info.has_dashboard 与资格位不一致"
        )


def test_contract_version_alone_counts_as_dashboard(monkeypatch):
    """新模块可以只声明 DASHBOARD_CONTRACT；has_dashboard 由它派生。"""
    from maaracing_master.core import registry as reg

    class _ContractOnly(ActivityModule):
        DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION

    monkeypatch.setattr(reg, "MODULE_REGISTRY", {"contract_only": _ContractOnly})
    monkeypatch.setattr(reg, "module_validity", lambda mid: (None, None))
    monkeypatch.setattr(reg, "module_expired", lambda mid, now=None: False)
    assert reg.get_module_info("contract_only")["has_dashboard"] is True


def test_zero_contract_is_reserved_not_candidate(monkeypatch):
    """版本 0 = 已登记、尚未实现：保留能力位，但不进入候选集。"""
    from maaracing_master.core import registry as reg

    class _Reserved(ActivityModule):
        DASHBOARD_CONTRACT = 0

    assert declares_dashboard(_Reserved) is False
    monkeypatch.setattr(reg, "MODULE_REGISTRY", {"reserved": _Reserved})
    monkeypatch.setattr(reg, "module_validity", lambda mid: (None, None))
    monkeypatch.setattr(reg, "module_expired", lambda mid, now=None: False)
    assert reg.get_module_info("reserved")["has_dashboard"] is False


def test_speedrush_declares_dashboard():
    """极速狂飙接入回合战绩看板：契约版本 1，元信息下发看板资格。"""
    cls = MODULE_REGISTRY["speedrush"]
    assert cls.DASHBOARD_CONTRACT == DASHBOARD_CONTRACT_VERSION
    assert get_module_info("speedrush")["has_dashboard"] is True


def test_legacy_hook_override_is_detected():
    """旧钩子覆盖是看板资格位的一部分：覆盖 read_today_stats 即算，基类不算。"""
    class _ReferenceModule(ActivityModule):
        @classmethod
        def read_today_stats(cls):
            return {"bucket": "2026-11-02", "summary": None, "games": []}

    assert overrides_classmethod(_ReferenceModule, "read_today_stats") is True
    assert overrides_classmethod(ActivityModule, "read_today_stats") is False


def test_legacy_today_stats_alone_counts_as_dashboard(monkeypatch):
    """旧模块只覆盖 read_today_stats，也能进入看板候选集。"""
    from maaracing_master.core import registry as reg

    class _LegacyOnly(ActivityModule):
        @classmethod
        def read_today_stats(cls):
            return {"bucket": "2026-11-02", "summary": None, "games": []}

    monkeypatch.setattr(reg, "MODULE_REGISTRY", {"legacy_only": _LegacyOnly})
    monkeypatch.setattr(reg, "module_validity", lambda mid: (None, None))
    monkeypatch.setattr(reg, "module_expired", lambda mid, now=None: False)
    assert reg.get_module_info("legacy_only")["has_dashboard"] is True


def test_flag_and_today_stats_are_distinct_declarations():
    """显式资格不要求今日统计；今日统计钩子单独构成旧模块资格。"""

    class _DashboardOnly(ActivityModule):
        HAS_DASHBOARD = True

    class _StatsOnly(ActivityModule):
        @classmethod
        def read_today_stats(cls):
            return {"bucket": "2026-11-02", "summary": None, "games": []}

    assert _DashboardOnly.HAS_DASHBOARD is True
    assert _DashboardOnly.read_today_stats() is None
    assert _StatsOnly.HAS_DASHBOARD is False
    assert _StatsOnly.read_today_stats()["bucket"] == "2026-11-02"


def test_builtin_steps_never_declare_dashboards():
    """内置步骤只跑一次，没有数据看板；它们不得获得看板资格。"""
    for step_id, cls in BUILTIN_STEPS.items():
        assert getattr(cls, "HAS_DASHBOARD", False) is False, (
            f"{step_id}: 内置步骤不应声明看板"
        )


def test_module_list_payload_carries_dashboard_flag():
    """初始状态的模块列表每项都带 has_dashboard；步骤恒 False，模块与元信息一致。"""
    try:
        from maaracing_master.core.sidecar import SidecarService
    except Exception as exc:  # noqa: BLE001 —— 完整运行时依赖缺失时整项跳过
        pytest.skip(f"sidecar 需要完整运行时依赖：{exc}")

    svc = object.__new__(SidecarService)  # _module_list 不碰实例状态
    items = SidecarService._module_list(svc)
    assert items, "模块列表不应为空"

    by_id = {item["id"]: item for item in items}
    for item in items:
        assert isinstance(item.get("has_dashboard"), bool), (
            f"{item['id']} 缺少布尔型 has_dashboard"
        )

    assert by_id["speedrush"]["has_dashboard"] is True
    assert by_id["launch_game"]["has_dashboard"] is False
    assert by_id["close_game"]["has_dashboard"] is False

    for module_id, item in by_id.items():
        if item["kind"] != "module":
            continue
        assert item["has_dashboard"] is get_module_info(module_id)["has_dashboard"]
