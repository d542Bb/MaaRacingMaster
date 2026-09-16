# -*- coding: utf-8 -*-
"""切换活动模块的守卫：运行中一律拒绝，且切换过程不构造任何模块实例。

为什么"运行中不许切"是硬规则：所选模块与正在跑的模块必须始终一致。运行中切换会让
GUI 的阶段列表、当前阶段高亮、配置面板全部指向另一个模块，而跑着的仍是旧模块——
界面与实况对不上，用户会照着错的阶段名读日志。

判据用 worker 槽（从 start 受理到 worker 结束），而非模块自身的 running 标志：
"启动中/停止中"也一并拦住，不留半开的窗口。

本文件导入 core.sidecar（经 controller 拉 maa 等重依赖），缺依赖时整文件 SKIP。
"""
from __future__ import annotations

import threading

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"模块切换守卫测试需要完整运行时依赖：{_ERR}"
)


class _NoInstantiateModule:
    """构造即失败：只要测试通过，就说明切换过程没有创建过任何模块实例。"""

    ID = "fake"

    def __init__(self, ctx=None):  # pragma: no cover —— 被调用即测试失败
        raise AssertionError("切换模块不得构造模块实例")


def _svc(monkeypatch, selected="treasure", worker=None):
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._worker = worker
    svc._selected_module = selected
    svc._stages = ["旧阶段"]
    # 模块元信息按 id 造，不依赖真实 manifest 的有效期（那是会随时间腐坏的外部事实）
    monkeypatch.setattr(sc, "get_module_info", lambda mid: {
        "id": mid, "name": mid, "stages": [f"{mid}-stage"], "expired": False,
    })
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {"treasure": _NoInstantiateModule,
                                               "speedrush": _NoInstantiateModule})
    return svc


def test_select_rejected_while_worker_alive(monkeypatch):
    """运行中切换被拒，且选中状态与阶段列表都不被改动。"""
    svc = _svc(monkeypatch, worker=object())  # 非 None 即"worker 槽已占用"
    ok, data, err = svc.select_module({"module_id": "speedrush"})
    assert not ok and data is None
    assert "运行中" in err
    assert svc._selected_module == "treasure"
    assert svc._stages == ["旧阶段"]


def test_clearing_selection_rejected_while_running(monkeypatch):
    """清空选择（GUI「（空）」）也是切换，运行中一并拒绝。"""
    svc = _svc(monkeypatch, worker=object())
    ok, _, err = svc.select_module({"module_id": None})
    assert not ok and "运行中" in err
    assert svc._selected_module == "treasure"


def test_select_allowed_when_idle(monkeypatch):
    svc = _svc(monkeypatch, worker=None)
    ok, data, err = svc.select_module({"module_id": "speedrush"})
    assert ok and err is None
    assert data["module_id"] == "speedrush"
    assert svc._selected_module == "speedrush"
    assert svc._stages == ["speedrush-stage"]


def test_select_stops_other_module_instance(monkeypatch):
    """切换不构造实例：假注册表里的类一构造就抛，切换跑通即证明没人构造它。"""
    svc = _svc(monkeypatch, worker=None)
    assert svc.select_module({"module_id": "speedrush"})[0]
    assert svc.select_module({"module_id": None})[0]
