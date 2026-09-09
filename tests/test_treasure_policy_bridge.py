# -*- coding: utf-8 -*-
"""P2a-Q3b/Q4：PolicyBridge 与 module 决策段契约测试。"""
from __future__ import annotations

from unittest.mock import MagicMock

from maaracing_assistant.plugins.treasure.module import TreasureModule
from maaracing_assistant.plugins.treasure.policy_bridge import (
    POLICY_ACTION_NAME,
    PolicyBridge,
)


def _stub_module() -> MagicMock:
    m = MagicMock()
    m._policy_snapshot = None
    m._trace_writer = None
    return m


def test_bridge_delegates_full_tick():
    """v4 模式：桥 run() = 一帧完整工作（_tick_once，Tasker 线程单线程无竞态）。"""
    module = _stub_module()
    bridge = PolicyBridge(module)
    assert bridge.run(None, MagicMock()) is True
    module._tick_once.assert_called_once_with()


def test_bridge_registers_under_policy_action_name():
    bridge = PolicyBridge(_stub_module())
    res = MagicMock()
    bridge.register(res)
    res.register_custom_action.assert_called_once_with(POLICY_ACTION_NAME, bridge)


def _module_with_real_decision_phase() -> MagicMock:
    """mock 状态 + 真绑定 _decision_phase：验内部调用序与落盘行为。"""
    m = _stub_module()
    m._resolve_action_target = MagicMock(return_value="INTENT")
    m._decision_phase = TreasureModule._decision_phase.__get__(m)
    return m


def test_decision_phase_call_sequence_on_module():
    """_decision_phase 内部按 resolve → consume → execute → shoo 顺序调三件。"""
    m = _module_with_real_decision_phase()
    m._decision_phase()
    m._consume_click_result.assert_called_once_with()
    m._execute_click.assert_called_once_with("INTENT")
    m._maybe_shoo_cursor.assert_called_once_with("INTENT")
    names = [c[0] for c in m.method_calls]
    assert names.index("_resolve_action_target") < names.index("_consume_click_result")
    assert names.index("_consume_click_result") < names.index("_execute_click")


def test_decision_phase_flushes_trace_snapshot():
    m = _module_with_real_decision_phase()
    writer = MagicMock()
    m._policy_snapshot = {"facts_projection": {}, "decision": {"key": "x"}}
    m._trace_writer = writer
    m._decision_phase()
    writer.write.assert_called_once()
    row = writer.write.call_args[0][0]
    assert row["event"] == "decision" and row["decision_snapshot"]["decision"]["key"] == "x"
    assert m._policy_snapshot is None  # 落盘后清空（v3 主循环与桥一致）


def test_v4_entry_and_flag_contract():
    """入口链头常量与开关函数契约。"""
    assert TreasureModule._V4_ENTRY == \
        "global.hall_peak_appraise_card.rhall_to_treasure.0"
    assert TreasureModule._v4_enabled() is False  # 缺省 NAVKIT_SOURCE=v3
