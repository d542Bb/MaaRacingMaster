# -*- coding: utf-8 -*-
"""运行队列执行语义的回归锁：顺序执行、失败/手动停止中止整轮、断点消费即清、退出条件。

判定表（每一项结束后）：
  - 抛异常 / natural=False → 该项 fail，中止整轮；
  - manual_stop=True → 该项 stopped，中止整轮（用户停止）；
  - 内置步骤请求退出本程序 → 该项 ok，剩余项 skip；
  - 自然跑完（含模块到限自动停止）→ ok，继续下一个。
  中止时剩余项标 skip。auto_exit 事件只在整轮全部自然完成且步骤请求退出时发出。

注意不能在启动下一个模块之后才查停止信号——start_module 会清 stop_event（★ 唯一
clear 位置），事后查永远看不到上一轮的停止；实现必须在循环里、调用之前依据上一轮
结局判据决定去留。本文件用假 controller 把四种结局逐个钉住。

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
    not _OK, reason=f"运行队列执行语义测试需要完整运行时依赖：{_ERR}"
)


class _FakeController:
    """按脚本回放各模块结局：outcomes[mid] ∈ 'ok' | 'raise' | 'swallowed' | 'stopped'。"""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls: list[tuple[str, object]] = []
        self.builtin_calls: list[tuple[str, object]] = []
        self.exit_requested = False
        self.last_run_natural = False
        self._manual_stop = False

    def start_module(self, module_id, start_from=None, module_config=None):
        self._manual_stop = False  # 对齐真实现：复位发生在下一次 start_module 开头
        self.last_run_natural = False
        self.calls.append((module_id, start_from, module_config))
        outcome = self.outcomes.get(module_id, "ok")
        if outcome == "raise":
            raise RuntimeError("boom")
        if outcome == "swallowed":
            self.last_run_natural = False  # start_module 内部吞掉模块异常的路径
            return
        if outcome == "stopped":
            self._manual_stop = True  # 模块运行期间用户按下了停止（stop() 置位）
        self.last_run_natural = True

    def run_builtin_step(self, step_id, step_config=None):
        """内置步骤：按脚本回放 ok / raise / swallowed / exit 四种结局。"""
        self.builtin_calls.append((step_id, step_config))
        self._manual_stop = False
        self.last_run_natural = False
        outcome = self.outcomes.get(step_id, "ok")
        if outcome == "raise":
            raise RuntimeError("boom")
        if outcome == "swallowed":
            return False
        if outcome == "stopped":
            self._manual_stop = True   # 步骤执行期间用户按下了停止
            return False
        self.last_run_natural = True
        if outcome == "exit":
            self.exit_requested = True  # 「关闭游戏」步骤里勾了「关闭后退出本程序」
        return True

    @property
    def manual_stop_requested(self):
        return self._manual_stop

    def stop(self):
        self._manual_stop = True


def _svc(controller):
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._controller = controller
    svc._worker = None
    svc._queue_run = None
    svc._round_result = None
    sent = []
    svc.send = sent.append
    return svc, sent


def _spawn(svc, plan):
    """起 worker 并返回线程引用——worker 结束时会把自己的槽位清成 None，
    断言必须持有线程对象本身，不能回头读 svc._worker。"""
    t = threading.Thread(target=svc._worker_main, args=(plan,), daemon=True)
    svc._worker = t
    t.start()
    return t


def _wait(thread):
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_round_runs_enabled_order_and_finishes_naturally():
    """全部自然完成：按序执行、断点与配置按模块传入、结局全 ok、auto_exit 发出。"""
    controller = _FakeController({"close_game": "exit"})
    svc, sent = _svc(controller)
    t = _spawn(svc, [
        {"kind": "module", "id": "a", "start_from": None, "config": None},
        {"kind": "module", "id": "b", "start_from": "b-stage", "config": {"k": 1}},
        {"kind": "builtin", "id": "close_game", "start_from": None, "config": {"exit_app": True}},
    ])
    _wait(t)
    assert [c[0] for c in controller.calls] == ["a", "b"]          # 按计划顺序
    assert controller.calls[1][1] == "b-stage"                     # 断点传入
    assert controller.calls[1][2] == {"k": 1}                      # 配置按模块注入
    assert controller.builtin_calls == [("close_game", {"exit_app": True})]
    assert svc._round_result == {"a": "ok", "b": "ok", "close_game": "ok"}
    assert svc._queue_run is None
    assert any(m.get("event") == "auto_exit" for m in sent)


def test_exception_aborts_round_and_marks_rest_skip():
    """模块抛异常 → 该模块 fail、剩余 skip、不发 auto_exit。"""
    controller = _FakeController({"a": "raise"})
    svc, sent = _svc(controller)
    t = _spawn(svc, [
        {"kind": "module", "id": "a", "start_from": None, "config": None},
        {"kind": "module", "id": "b", "start_from": None, "config": None},
    ])
    _wait(t)
    assert [c[0] for c in controller.calls] == ["a"]               # b 不再启动
    assert svc._round_result == {"a": "fail", "b": "skip"}
    assert not sent


def test_swallowed_failure_aborts_round():
    """start_module 吞掉的模块异常（natural=False）同样 fail + 中止。"""
    controller = _FakeController({"a": "swallowed"})
    svc, sent = _svc(controller)
    t = _spawn(svc, [
        {"kind": "module", "id": "a", "start_from": None, "config": None},
        {"kind": "module", "id": "b", "start_from": None, "config": None},
    ])
    _wait(t)
    assert svc._round_result == {"a": "fail", "b": "skip"}
    assert not sent


def test_manual_stop_aborts_round_and_skips_auto_exit():
    """用户停止 → 当前模块 stopped、剩余 skip、不触发 auto_exit。"""
    controller = _FakeController({"a": "stopped"})
    svc, sent = _svc(controller)
    t = _spawn(svc, [
        {"kind": "module", "id": "a", "start_from": None, "config": None},
        {"kind": "module", "id": "b", "start_from": None, "config": None},
    ])
    _wait(t)
    assert svc._round_result == {"a": "stopped", "b": "skip"}
    assert not sent


def test_step_manual_stop_reports_stopped_not_fail():
    """步骤被手动停止时记「已停止」而不是「失败」：停止先于自然完成判定。"""
    controller = _FakeController({"launch_game": "stopped"})
    svc, sent = _svc(controller)
    t = _spawn(svc, [
        {"kind": "builtin", "id": "launch_game", "start_from": None, "config": None},
        {"kind": "module", "id": "a", "start_from": None, "config": None},
    ])
    _wait(t)
    assert svc._round_result == {"launch_game": "stopped", "a": "skip"}
    assert not sent


def test_start_consumes_breakpoints_and_builds_plan(monkeypatch):
    """start 按启用顺序组计划、断点消费即清；空实现模块让真线程立即跑完。"""
    monkeypatch.setattr(sc, "has_physical_controller", lambda: False)
    monkeypatch.setattr(sc, "check_required_assets", lambda mid: [])
    monkeypatch.setattr(sc, "get_module_info", lambda mid: {
        "id": mid, "name": mid, "stages": [f"{mid}-1", f"{mid}-2"], "expired": False,
    })
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {
        "a": type("A", (), {"ID": "a",
                            "required_capabilities": classmethod(lambda cls, cfg=None: frozenset()),
                            "requires_exclusive_gamepad": classmethod(lambda cls, cfg=None: False)}),
        "b": type("B", (), {"ID": "b",
                            "required_capabilities": classmethod(lambda cls, cfg=None: frozenset()),
                            "requires_exclusive_gamepad": classmethod(lambda cls, cfg=None: False)}),
    })

    def make_svc(order, enabled, start_from):
        svc = object.__new__(SidecarService)
        svc._lock = threading.RLock()
        svc._closed = False
        svc._worker = None
        svc._queue_order = order
        svc._queue_enabled = enabled
        svc._start_from = start_from
        svc._queue_run = None
        svc._round_result = None
        svc._module_config_cache = {"a": {"loops": 3}}
        controller = _FakeController({})  # outcomes 空 = 全部自然完成，真线程立即跑完
        svc._controller = controller
        sent = []
        svc.send = sent.append
        return svc, controller

    # ① 正常路径：计划 = 启用序 + 各自配置槽 + 断点；断点表被消费清空
    svc, controller = make_svc(["a", "b"], {"a": True, "b": True}, {"b": "b-2"})
    ok, _, err = svc.start({})
    assert ok and err is None, err
    assert svc._start_from == {}            # 消费即清（start 受理时即弹出）
    assert svc._round_result is None        # 新一轮清掉上一轮结局
    t = svc._worker
    _wait(t)
    assert controller.calls == [("a", None, {"loops": 3}), ("b", "b-2", None)]
    assert svc._round_result == {"a": "ok", "b": "ok"}

    # ② 队列没有启用模块 → 拒绝
    svc2, _ = make_svc(["a"], {"a": False}, {})
    ok2, _, err2 = svc2.start({})
    assert not ok2 and "没有启用" in err2

    # ③ 断点不属于该模块阶段 → 整批拒绝（计划不成立）
    svc3, _ = make_svc(["a"], {"a": True}, {"a": "bad-stage"})
    ok3, _, err3 = svc3.start({})
    assert not ok3 and "不属于模块" in err3
