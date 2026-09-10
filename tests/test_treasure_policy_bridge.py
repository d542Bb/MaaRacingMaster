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
    """_decision_phase 内部按 resolve → consume → execute 顺序调三件。

    P4c（宪法 §5）：原第四步 shoo 避让看守已从调用序退役——断言方法本体不存在，
    防止反应式躲避被悄悄加回。"""
    m = _module_with_real_decision_phase()
    m._decision_phase()
    m._consume_click_result.assert_called_once_with()
    m._execute_click.assert_called_once_with("INTENT")
    assert not hasattr(TreasureModule, "_maybe_shoo_cursor")
    assert not hasattr(TreasureModule, "_collect_guard_rects")
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
    """入口与开关契约：生产入口 = 起跑汇聚节点（任意 stage 自适应，
    P2b 真机三炸根修——链头线性路径在卡片态变化时永不命中）。"""
    assert TreasureModule._V4_ENTRY == "treasure.__boot.dwell"
    assert TreasureModule._v4_enabled() is True  # P2b 验收后缺省即 v4（应急回退需显式 NAVKIT_SOURCE=v3）


def test_v4_loop_assembles_existing_source_dirs(monkeypatch):
    """_run_v4_loop 装配契约：core/plugin 真源目录必须真实落位
    （回归：插件根曾少推导一级，真机拼出 plugins/resources/nav 而炸）。"""
    from pathlib import Path

    from maaracing_assistant.core import nav_graph as _ng

    captured: dict = {}

    class _StubRunner:
        def __init__(self, ctx, *, pipeline_dirs, image_dirs, bridges):
            captured["built"] = captured.get("built", 0) + 1
            captured["pipeline_dirs"] = [Path(d) for d in pipeline_dirs]
            captured["image_dirs"] = [Path(d) for d in image_dirs]
            captured["bridges"] = dict(bridges)

        def start(self, entry):
            captured["entry"] = entry
            return True

        def poll(self):
            return True

        def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr(_ng, "NavKitV4", _StubRunner)
    m = _stub_module()
    m._V4_ENTRY = TreasureModule._V4_ENTRY
    m._v4_runner = None            # 无驻留 runner → 走构造分支
    m.ctx.lifecycle.running = False  # 健康守护循环立即退出
    m.ctx.click_mode = "keyboard"    # 跳过手柄租约分支
    TreasureModule._run_v4_loop.__get__(m)()

    dirs = captured["pipeline_dirs"]
    assert len(dirs) == 2
    assert (dirs[0] / "global.json").is_file()    # core 骨架真源
    assert (dirs[1] / "treasure.json").is_file()  # 插件模块图真源
    assert (Path(captured["image_dirs"][0]) / ".").is_dir()  # 模板目录存在
    assert captured["entry"] == TreasureModule._V4_ENTRY
    assert PolicyBridge in [type(v) for v in captured["bridges"].values()]
    assert captured["stopped"] is True
    TreasureModule._run_v4_loop.__get__(m)()  # 重启：必须复用驻留 runner
    assert captured["built"] == 1  # C 句柄不二次构造（GC 竞态崩溃防线）
