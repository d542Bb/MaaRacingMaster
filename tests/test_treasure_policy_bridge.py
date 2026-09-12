# -*- coding: utf-8 -*-
"""P2a-Q3b/Q4：PolicyBridge 与 module 决策段契约测试。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# module 经 core.window_utils 顶层 import maa.toolkit，CI 轻依赖环境下收集期 ERROR
# 会中断整个 pytest 会话；按仓内既有口径整文件优雅跳过。
try:
    from maaracing_master.plugins.treasure.module import TreasureModule
    from maaracing_master.plugins.treasure.policy_bridge import (
        MIN_FRAME_INTERVAL_MS,
        POLICY_ACTION_NAME,
        PolicyBridge,
    )

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
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
    """_decision_phase 内部按 resolve → consume → execute → shoo 顺序调四件。

    P4c 曾以 mask_cursor 替代全部 shoo 并锁死调用序；2026-09-11 实机定罪该替代
    有缺口——mask_cursor 只装在图内模板节点，OCR 通路（出价按钮文字/输入框）无
    遮挡处理被读脏（「出价.39,5」、瞬空读振荡）。光标驻留看守按新证据重新接线：
    shoo 在 execute 之后（auto_shoo 的 is_busy 闸保证点击优先，避让只走
    submit_move 不产生点击）。"""
    m = _module_with_real_decision_phase()
    m._decision_phase()
    m._consume_click_result.assert_called_once_with()
    m._execute_click.assert_called_once_with("INTENT")
    m._maybe_shoo_cursor.assert_called_once_with("INTENT")
    names = [c[0] for c in m.method_calls]
    assert names.index("_resolve_action_target") < names.index("_consume_click_result")
    assert names.index("_consume_click_result") < names.index("_execute_click")
    assert names.index("_execute_click") < names.index("_maybe_shoo_cursor")


def test_decision_phase_flushes_trace_snapshot():
    m = _module_with_real_decision_phase()
    writer = MagicMock()
    m._policy_snapshot = {"facts_projection": {}, "decision": {"key": "x"}}
    m._trace_writer = writer
    m._decision_phase()
    writer.write.assert_called_once()
    row = writer.write.call_args[0][0]
    assert row["event"] == "decision" and row["decision_snapshot"]["decision"]["key"] == "x"
    assert m._policy_snapshot is None  # 落盘后清空，每帧只写一份决策契约


def test_v4_entry_and_single_path_contract():
    """入口契约：生产入口 = 起跑汇聚节点（任意 stage 自适应，
    P2b 真机三炸根修——链头线性路径在卡片态变化时永不命中）；
    执行通路唯一，运行模式开关不得回加。"""
    assert TreasureModule._V4_ENTRY == "treasure.__boot.dwell"
    assert not hasattr(TreasureModule, "_v4_enabled")


def test_v4_loop_assembles_existing_source_dirs(monkeypatch):
    """_run_v4_loop 装配契约：core/plugin 真源目录必须真实落位
    （回归：插件根曾少推导一级，真机拼出 plugins/resources/nav 而炸）。"""
    from pathlib import Path

    from maaracing_master.core import nav_graph as _ng

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
    # core 侧无跨模块共用链真源 → 整层不加载（目录按存在性+非空纳入）
    assert len(dirs) == 1
    assert (dirs[0] / "treasure.json").is_file()       # 插件对局图真源
    assert (dirs[0] / "treasure.entry.json").is_file()  # 插件大厅入口链真源
    assert (Path(captured["image_dirs"][0]) / ".").is_dir()  # 模板目录存在
    assert captured["entry"] == TreasureModule._V4_ENTRY
    assert PolicyBridge in [type(v) for v in captured["bridges"].values()]
    assert captured["stopped"] is True
    TreasureModule._run_v4_loop.__get__(m)()  # 重启：必须复用驻留 runner
    assert captured["built"] == 1  # C 句柄不二次构造（GC 竞态崩溃防线）


# ==================== 决策帧自节流闸门 ====================
# 依据 5.12.3 实测（tools/experiments/v4-frame-pacing/）：命中 jump_back 兜底位后，
# 父 dwell 的 rate_limit / pre_delay / post_delay 全部旁路，间隔 = 动作自身耗时。
# 闸门只在这一层能拦住，故用可注入时钟做确定性验证（不靠真实 sleep 计时）。

class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _paced_bridge(*, work_s: float, floor_ms: float = 100.0, running: bool = True):
    """桥 + 假时钟 + 假决策段：_tick_once 恒耗时 work_s。

    sleeper 必须**推进假时钟**——_sleep 的退出条件靠时钟前进，
    只记录不推进会让等待循环永不结束（首版踩过的坑）。
    """
    clock = _FakeClock()
    slept: list[float] = []

    def _advance(seconds: float) -> None:
        clock.advance(seconds)
        slept.append(seconds)

    module = _stub_module()
    module.ctx.lifecycle.running = running
    module._tick_once.side_effect = lambda: clock.advance(work_s)
    bridge = PolicyBridge(module, min_interval_ms=floor_ms, clock=clock,
                          sleeper=_advance)
    return bridge, clock, slept


def test_pacer_gates_light_decision_frames():
    """决策段轻于下限时补足间隔：轻帧 10ms + 下限 100ms → 决策起点严格 100ms 步进。"""
    bridge, clock, slept = _paced_bridge(work_s=0.010)
    starts: list[float] = []
    for _ in range(4):
        assert bridge.run(None, MagicMock()) is True
        starts.append(bridge._last_run_at)  # 过闸后的决策起点，不是入口时刻
    assert starts == pytest.approx([0.0, 0.1, 0.2, 0.3])
    assert sum(slept) == pytest.approx(0.27)  # 首帧不等待，后三帧各补 90ms
    assert clock.t == pytest.approx(0.31)     # 末帧起点 0.3 + 该帧工作 10ms


def test_pacer_silent_when_work_exceeds_floor():
    """决策段自身够重（真机约 125ms > 100ms 下限）时闸门完全不介入，不加延迟。"""
    bridge, clock, slept = _paced_bridge(work_s=0.125)
    for _ in range(3):
        bridge.run(None, MagicMock())
    assert slept == []
    assert clock.t == pytest.approx(0.375)


def test_pacer_returns_fast_on_stop_signal():
    """睡眠途中宿主停止 → 立即返回，不把 Tasker 线程睡在闸门里。"""
    bridge, clock, slept = _paced_bridge(work_s=0.010, running=False)
    bridge.run(None, MagicMock())   # 首帧建立基准时刻
    slept.clear()
    bridge.run(None, MagicMock())   # 本应补 90ms，被停止信号短路
    assert slept == []
    assert clock.t == pytest.approx(0.020)  # 未等待，两帧只有工作耗时 10+10


def test_pacer_floor_configurable_and_disablable():
    """下限可配：0 = 关闭闸门（供实测/压测复现自旋上限）。"""
    assert MIN_FRAME_INTERVAL_MS == 100.0
    bridge, clock, slept = _paced_bridge(work_s=0.010, floor_ms=0.0)
    for _ in range(5):
        bridge.run(None, MagicMock())
    assert slept == []
    assert clock.t == pytest.approx(0.05)
