# -*- coding: utf-8 -*-
"""speedrush 驾驶门控与模块配置的回归锁。

重点锁住两件容易错的事：
1. **就绪去抖**——中间一次失配就必须重新计数（否则起步动画期间的瞬时命中会
   被当成"已可驾驶"）；
2. **结束去抖**——单次锚点丢失不算离场（否则过场动画的瞬时遮挡会提前结束阶段）。

用受控时钟驱动，不依赖真实等待；用假 graph 控制锚点命中序列。
"""

from __future__ import annotations

import numpy as np
import pytest

try:
    from maaracing_master.plugins.speedrush import module as sr
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺重依赖的机器上整文件 SKIP
    # 模块的继承链要 maa（ActivityModule），而 CI 只装轻量依赖集（pytest/numpy/
    # opencv-headless），故在 CI 上整文件跳过；本机 .venv 全依赖照常执行。
    _OK, _ERR = False, str(exc)
    sr = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


class _Clock:
    """受控单调时钟：sleep 推进它，使超时判据在测试中可控且瞬时。"""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t


class _FakeLifecycle:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.running = True

    def sleep(self, seconds: float) -> bool:
        self._clock.t += seconds
        return self.running


class _FakeCapture:
    """实现 CaptureCapability 的两个入口；帧标识单调递增以便断言落到 jsonl。"""

    def __init__(self) -> None:
        self.calls = 0
        self._fid = 0

    def screenshot(self):
        self.calls += 1
        return np.zeros((8, 8, 3), dtype=np.uint8)

    def frame_with_age(self):
        self.calls += 1
        self._fid += 1
        return (np.zeros((8, 8, 3), dtype=np.uint8), self._fid, 1000 * self._fid, 1.5)


class _FakeCtx:
    def __init__(self, clock: _Clock) -> None:
        self.lifecycle = _FakeLifecycle(clock)
        self.capture = _FakeCapture()


class _FakeGraph:
    """按序列返回锚点命中结果；序列用尽后重复最后一个值。"""

    def __init__(self, seq: list[bool]) -> None:
        self.seq = list(seq)
        self.calls = 0

    def run(self, entry: str, reached: str | None = None) -> bool:
        v = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        return v


@pytest.fixture
def env(monkeypatch, tmp_path):
    """装好受控时钟与数据目录的模块实例工厂。"""
    clock = _Clock()
    monkeypatch.setattr(sr, "time", clock)
    monkeypatch.setattr(sr, "data_dir", lambda: tmp_path / "data")
    ctx = _FakeCtx(clock)
    mod = sr.SpeedRushModule(ctx)  # type: ignore[arg-type]
    mod._running = True  # 默认为"已启动"；需停机的用例自行覆盖
    return mod, ctx, clock


def _set_graph(mod, seq: list[bool]) -> _FakeGraph:
    g = _FakeGraph(seq)
    mod._graph = g
    return g


# ---------- 就绪判据 ----------


def test_ready_debounces_on_single_miss(env) -> None:
    """命中-失配-命中-命中：失配那一次必须清零重计。"""
    mod, _, _ = env
    g = _set_graph(mod, [True, False, True, True])
    assert mod._wait_drive_ready(1) is True
    assert g.calls == 4  # 若未去抖，第 2 次就会误判就绪


def test_ready_requires_consecutive_hits(env) -> None:
    mod, _, _ = env
    _set_graph(mod, [True, False, True, False, True, True])
    assert mod._wait_drive_ready(1) is True


def test_ready_times_out_without_anchor(env) -> None:
    mod, _, _ = env
    g = _set_graph(mod, [False])
    assert mod._wait_drive_ready(1) is False
    # 超时靠受控时钟推进，调用次数应为有限值而非自旋
    assert g.calls > 0


def test_ready_returns_false_when_stopped(env) -> None:
    mod, ctx, _ = env
    _set_graph(mod, [False])
    mod._running = False
    assert mod._wait_drive_ready(1) is False


# ---------- 结束判据 ----------


def test_loop_ends_after_consecutive_misses(env) -> None:
    mod, _, _ = env
    mod._running = True
    g = _set_graph(mod, [True, False, False])
    assert mod._drive_loop(1, None) is True
    assert g.calls == 3


def test_loop_tolerates_single_miss(env) -> None:
    """单次锚点丢失不得结束阶段——这是过场动画误判的防线。"""
    mod, _, _ = env
    mod._running = True
    g = _set_graph(mod, [True, False, True, False, False])
    assert mod._drive_loop(1, None) is True
    assert g.calls == 5  # 若不设容差，第 3 次就结束了


def test_loop_returns_false_on_stop(env) -> None:
    mod, ctx, _ = env
    _set_graph(mod, [True])

    class _StopAfterOne(_FakeCapture):
        def frame_with_age(self):
            mod._running = False  # 模拟 stop() 置停止标志
            return (np.zeros((4, 4, 3), dtype=np.uint8), 1, 1000, 0.0)

    ctx.capture = _StopAfterOne()
    assert mod._drive_loop(1, None) is False


def test_loop_times_out(env) -> None:
    mod, _, _ = env
    mod._running = True
    _set_graph(mod, [True])  # 一直命中 → 永不结束
    assert mod._drive_loop(1, None) is False


# ---------- 录制接入 ----------


def test_drive_starts_and_stops_recorder_in_record_mode(env) -> None:
    mod, _, _ = env
    mod._record_mode = True
    mod._running = True
    _set_graph(mod, [True, True, True, False, False])

    assert mod._drive(1) is True
    # 退出后必须释放引用（否则 HUD 的 _state.recording 会永远为真）
    assert mod._recorder is None

    sessions = sorted((sr.data_dir() / "speedrush" / "demos").iterdir())
    assert len(sessions) == 1
    assert sessions[0].name.endswith("_p1")
    assert (sessions[0] / "meta.json").is_file()
    assert (sessions[0] / "frames.jsonl").is_file()


def test_drive_without_record_mode_creates_no_session(env) -> None:
    mod, _, _ = env
    mod._record_mode = False
    # 就绪需连续两次命中；随后两次失配即判离场
    _set_graph(mod, [True, True, False, False])
    assert mod._drive(1) is True
    assert mod._recorder is None
    assert not (sr.data_dir() / "speedrush" / "demos").exists()


def test_drive_returns_false_when_never_ready(env) -> None:
    mod, _, _ = env
    mod._running = True
    _set_graph(mod, [False])
    assert mod._drive(1) is False


# ---------- 模块配置 ----------


def test_config_roundtrip_and_state_shape(env) -> None:
    mod, _, _ = env
    cfg = mod.set_module_config({"record_mode": True})
    assert cfg["record_mode"] is True
    assert mod.get_module_config()["record_mode"] is True

    state = mod.get_module_config()["_state"]
    assert state["recording"] is False
    assert state["frames"] == 0
    assert state["demos_dir"].endswith("speedrush\\demos") or state["demos_dir"].endswith(
        "speedrush/demos")


def test_config_ignores_unknown_and_coerces_bool(env) -> None:
    mod, _, _ = env
    mod.set_module_config({"unknown_key": 1})
    assert mod.get_module_config()["record_mode"] is False
    mod.set_module_config({"record_mode": 1})
    assert mod.get_module_config()["record_mode"] is True


def test_config_tolerates_non_dict(env) -> None:
    mod, _, _ = env
    assert mod.set_module_config("not-a-dict")["record_mode"] is False