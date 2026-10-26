# -*- coding: utf-8 -*-
"""运行任务队列（set_run_queue / set_start_from）的守卫：运行中一律拒绝，且全程不构造模块实例。

为什么"运行中不许改队列/断点"是硬规则：队列顺序与启用勾选是本次运行的编排依据，
运行中改动会让「排队中」卡片与实况对不上。判据用 worker 槽（从 start 受理到
worker 结束），"启动中/停止中"也一并拦住，不留半开的窗口。

断点起点是纯内存状态：set_start_from 只写表，start 时消费即清——不跨轮记忆，
更不跨会话（GUI 重新打开时回第一步）。

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
    not _OK, reason=f"任务队列守卫测试需要完整运行时依赖：{_ERR}"
)


class _NoInstantiateModule:
    """构造即失败：只要测试通过，就说明队列/断点操作没有创建过任何模块实例。"""

    ID = "fake"

    def __init__(self, ctx=None):  # pragma: no cover —— 被调用即测试失败
        raise AssertionError("队列/断点操作不得构造模块实例")


def _svc(monkeypatch, worker=None):
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._worker = worker
    svc._queue_order = ["treasure", "speedrush"]
    svc._queue_enabled = {"treasure": True, "speedrush": False}
    svc._start_from = {}
    svc._queue_run = None
    svc._round_result = None
    # 模块元信息按 id 造，不依赖真实 manifest 的有效期（那是会随时间腐坏的外部事实）
    monkeypatch.setattr(sc, "get_module_info", lambda mid: {
        "id": mid, "name": mid, "stages": [f"{mid}-stage"], "expired": False,
    })
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {"treasure": _NoInstantiateModule,
                                                "speedrush": _NoInstantiateModule})
    # set_run_queue 的过期校验走 module_expired；测试里默认全部未过期
    monkeypatch.setattr(sc, "module_expired", lambda mid, now=None: False)
    # 落盘走假写：队列守卫测试不关心 profile 文件内容
    monkeypatch.setattr(sc, "_save_profile", lambda partial: None)
    return svc


# ---------- set_run_queue：运行中守卫 ----------

def test_set_queue_rejected_while_worker_alive(monkeypatch):
    """运行中改队列被拒，且队列状态不被改动。"""
    svc = _svc(monkeypatch, worker=object())  # 非 None 即"worker 槽已占用"
    ok, data, err = svc.set_run_queue({"order": ["speedrush"], "enabled": {"speedrush": True}})
    assert not ok and data is None
    assert "运行中" in err
    assert svc._queue_order == ["treasure", "speedrush"]
    assert svc._queue_enabled == {"treasure": True, "speedrush": False}


def test_set_start_from_rejected_while_worker_alive(monkeypatch):
    svc = _svc(monkeypatch, worker=object())
    ok, _, err = svc.set_start_from({"module_id": "treasure", "stage": "treasure-stage"})
    assert not ok and "运行中" in err
    assert svc._start_from == {}


# ---------- set_run_queue：入列校验 ----------

def test_set_queue_rejects_unknown_module(monkeypatch):
    svc = _svc(monkeypatch)
    ok, _, err = svc.set_run_queue({"order": ["treasure", "ghost"], "enabled": {}})
    assert not ok and "模块不存在" in err
    assert svc._queue_order == ["treasure", "speedrush"]  # 原队列原样保留


def test_set_queue_rejects_duplicate(monkeypatch):
    svc = _svc(monkeypatch)
    ok, _, err = svc.set_run_queue({"order": ["treasure", "treasure"], "enabled": {}})
    assert not ok and "重复" in err


def test_set_queue_persists_snapshot(monkeypatch):
    svc = _svc(monkeypatch)
    saved = {}
    monkeypatch.setattr(sc, "_save_profile", lambda partial: saved.update(partial))
    ok, data, err = svc.set_run_queue({
        "order": ["speedrush", "treasure"],
        "enabled": {"speedrush": True},
    })
    assert ok and err is None
    assert svc._queue_order == ["speedrush", "treasure"]
    assert svc._queue_enabled == {"speedrush": True}
    assert data == {"order": ["speedrush", "treasure"], "enabled": {"speedrush": True}}
    assert saved["run_queue"] == data  # 落盘内容与返回快照一致


def test_set_queue_drops_enabled_entry_of_removed_module(monkeypatch):
    """移出列表的模块连带清掉启用位；再入列时默认不启用，由调用方显式给值。"""
    svc = _svc(monkeypatch)
    svc._queue_enabled["speedrush"] = True
    ok, _, _ = svc.set_run_queue({"order": ["treasure"], "enabled": {"treasure": True, "speedrush": True}})
    assert ok
    assert "speedrush" not in svc._queue_enabled


# ---------- set_run_queue：过期模块须确认 ----------

def test_set_queue_rejects_expired_without_force(monkeypatch):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(sc, "module_expired", lambda mid, now=None: mid == "speedrush")
    ok, _, err = svc.set_run_queue({"order": ["speedrush"], "enabled": {"speedrush": True}})
    assert not ok and "已过期" in err
    # 未勾选的过期模块不拦：不跑的模块没有「使用」发生
    ok2, _, _ = svc.set_run_queue({"order": ["speedrush"], "enabled": {}})
    assert ok2


def test_set_queue_accepts_expired_with_force(monkeypatch):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(sc, "module_expired", lambda mid, now=None: True)
    ok, _, err = svc.set_run_queue({
        "order": ["speedrush"], "enabled": {"speedrush": True}, "force": True,
    })
    assert ok and err is None


# ---------- set_start_from：断点只写内存表、校验阶段合法性 ----------

def test_set_start_from_roundtrip(monkeypatch):
    svc = _svc(monkeypatch)
    ok, data, err = svc.set_start_from({"module_id": "treasure", "stage": "treasure-stage"})
    assert ok and err is None
    assert svc._start_from == {"treasure": "treasure-stage"}
    assert data == {"breakpoints": {"treasure": "treasure-stage"}}


def test_set_start_from_rejects_bad_stage(monkeypatch):
    svc = _svc(monkeypatch)
    ok, _, err = svc.set_start_from({"module_id": "treasure", "stage": "不存在的阶段"})
    assert not ok and "不属于模块" in err
    assert svc._start_from == {}


def test_set_start_from_none_clears(monkeypatch):
    svc = _svc(monkeypatch)
    svc._start_from = {"treasure": "treasure-stage"}
    ok, data, _ = svc.set_start_from({"module_id": "treasure", "stage": None})
    assert ok
    assert svc._start_from == {}
    assert data == {"breakpoints": {}}
