# -*- coding: utf-8 -*-
"""帧边界异常可见性（P0-3）：异常必须留痕，fatal 必须真终止。

真机背景（2026-09-16 会话 20260916_215905，领取分红阶段）：决策层判定终止
（`ClickRetryExhaustedError`）后，每帧都在同一处抛出——而且是**在心跳日志与
决策段之前**抛出，于是：

  · `trace.jsonl` 的 `decision` 记录与运行日志心跳整段消失（帧 1800 → 1908
    直跳，约 11 秒）；
  · 框架侧只看到 `policy_loop` 每帧「成功」返回（实测 11 秒内被调 101 次、
    每次 95–110ms），语义上的「终止模块」没有发生；
  · 恢复靠结算动画自己播完，全程零日志、零 ERROR。

契约（本文件锁）：
  1. fatal → ERROR（带帧号 + 阶段 + 原文）→ 请求停止 → 后续帧短路不再执行帧工作；
  2. 非 fatal 异常 → ERROR（带帧号 + 阶段 + traceback）→ **保持**「下帧重试」
     既有语义（不擅自终止模块），并按 FRAME_ERROR_LOG_EVERY 节流；
  3. 帧边界不得让任何异常无声穿出。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from maaracing_master.plugins.treasure.module import (
        ClickRetryExhaustedError,
        TreasureModule,
    )
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"测试需要完整运行时依赖（maa/…）：{_ERR}"
)

_plugin_mod = __import__("maaracing_master.plugins.treasure.module", fromlist=["logger"])


class _RecordingLogger:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO", channel=None):  # noqa: ANN001 —— 与真 logger 同形
        self.lines.append((level, str(msg)))

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lv, m in self.lines if level is None or lv == level]


@pytest.fixture
def recorder(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(_plugin_mod, "logger", rec)
    return rec


class _FrameSelf:
    """帧边界桩：只喂 `_tick_once` 读的字段 + 一个可控抛异常的帧工作体。"""

    FRAME_ERROR_LOG_EVERY = TreasureModule.FRAME_ERROR_LOG_EVERY

    def __init__(self, exc: BaseException | None = None):
        self._frame_abort = False
        self._frame_error_count = 0
        self._frame_counter = 1801
        self._current_stage = "领取分红"
        self.stop_requests: list[bool] = []
        self.body_calls = 0
        self.ctx = SimpleNamespace(
            lifecycle=SimpleNamespace(request_stop=lambda: self.stop_requests.append(True))
        )
        self._exc = exc

    def _tick_frame_body(self):
        self.body_calls += 1
        if self._exc is not None:
            raise self._exc


# ------------------------------------------------------------------
#  1. fatal：记 ERROR → 请求停止 → 短路后续帧
# ------------------------------------------------------------------


def test_fatal_is_logged_with_frame_and_stage_and_stops_module(recorder):
    msg = "面板数字键 bid_numpad_1 连点 4 次输入框仍无变化（目标价无法录入）"
    fake = _FrameSelf(ClickRetryExhaustedError(msg))

    TreasureModule._tick_once(fake)

    errors = recorder.messages("ERROR")
    assert errors, "fatal 必须留痕——静默正是本次缺陷"
    assert "#1801" in errors[0] and "领取分红" in errors[0], "ERROR 须带帧号与阶段"
    assert msg in errors[0], "须带上 fatal 原文（否则不知道终止原因）"
    assert "已请求停止" in errors[0]
    assert fake.stop_requests == [True], "fatal 必须走显式停止路径（不得退化成静默空转）"


def test_fatal_short_circuits_later_frames(recorder):
    """终止判定后不再执行帧工作：既不重复打日志，也不再点击。"""
    fake = _FrameSelf(ClickRetryExhaustedError("终止"))
    TreasureModule._tick_once(fake)
    assert fake.body_calls == 1

    for _ in range(100):                     # 模拟真机那 11 秒里的 100 次重入
        TreasureModule._tick_once(fake)

    assert fake.body_calls == 1, "短路后不得再执行帧工作"
    assert len(recorder.messages("ERROR")) == 1, "不得每帧重复记录同一 fatal"
    assert fake.stop_requests == [True], "停止请求只发一次"


def test_non_fatal_exception_is_visible_but_does_not_kill_module(recorder):
    """非 fatal 异常：留痕（带 traceback）但**保持**下帧重试语义。"""
    fake = _FrameSelf(ValueError("boom"))

    TreasureModule._tick_once(fake)

    errors = recorder.messages("ERROR")
    assert errors and "#1801" in errors[0] and "领取分红" in errors[0]
    assert "ValueError" in errors[0] and "Traceback" in errors[0], "须带异常类型与回溯"
    assert fake.stop_requests == [], "非 fatal 不得擅自终止模块"
    assert fake._frame_abort is False, "非 fatal 不得置终止闸"


def test_non_fatal_exception_logging_is_throttled(recorder):
    """同类非 fatal 异常按帧节流：首个全量记录，此后每 N 帧一条（不刷屏）。"""
    fake = _FrameSelf(ValueError("boom"))
    for _ in range(fake.FRAME_ERROR_LOG_EVERY * 2):
        TreasureModule._tick_once(fake)

    n = len(recorder.messages("ERROR"))
    assert fake.body_calls == fake.FRAME_ERROR_LOG_EVERY * 2, "非 fatal 必须继续尝试帧工作"
    assert n == 3, f"节流后应为 1 + 2 条（每 {fake.FRAME_ERROR_LOG_EVERY} 帧一条），实际 {n}"


def test_no_exception_no_noise(recorder):
    """正常帧：零告警、零停止请求（帧边界闸不得干扰正常路径）。"""
    fake = _FrameSelf(None)
    TreasureModule._tick_once(fake)
    assert fake.body_calls == 1
    assert recorder.messages() == []
    assert fake.stop_requests == []


def test_snapshot_path_does_not_run_decision():
    """P0-4：快照字段只读上一帧决策结果，不得自己触发决策（含 fatal 副作用）。

    这是上面那条「11 秒静默」的结构性成因：快照在心跳与决策段**之前**执行，
    若它调用决策入口，fatal 就抛在帧边界之外（无心跳、无 decision trace，
    也不终止）。用源码级守卫锁住这个方向（决策副作用只许从决策段走）。
    """
    import inspect
    src = inspect.getsource(TreasureModule._treasure_kwargs)
    assert "_resolve_action_target()" not in src, \
        "快照路径不得调用决策入口 _resolve_action_target()（决策副作用须只在决策段）"
    assert "_last_intent" in src, "快照应读决策段发布的只读缓存 _last_intent"

    # 唯一真实决策调用点必须是决策段
    decision_src = inspect.getsource(TreasureModule._decision_phase)
    assert "_resolve_action_target()" in decision_src, "决策段必须仍是决策入口的调用者"
    assert "_last_intent = intent" in decision_src, "决策段须发布 _last_intent 供快照只读"