# -*- coding: utf-8 -*-
"""speedrush 驾驶门控与模块配置的回归锁。

重点锁住两件容易错的事：
1. **就绪去抖**——中间一次失配就必须重新计数（否则起步动画期间的瞬时命中会
   被当成"已可驾驶"）；
2. **结束去抖**——单次锚点丢失不算离场（否则过场动画的瞬时遮挡会提前结束阶段）。

用受控时钟驱动，不依赖真实等待；用假 graph 控制锚点命中序列。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
    """受控单调时钟：sleep 推进它，使超时判据在测试中可控且瞬时。

    perf_counter 委托受控值（2026-10-06 起 pacing 全线改细钟，驱动环环拍
    记账与 rest 计算也读 perf_counter——假钟必须同名供给，否则驱动环
    AttributeError；两钟同值保持确定性）。"""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def perf_counter(self) -> float:
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


# ---------- 门控取证的帧锚点 ----------


class _LogSink:
    """接住模块的日志行，用来断言"日志里到底写了什么"。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO", channel=None) -> None:
        self.lines.append((level, msg))

    def texts(self, needle: str) -> list[str]:
        return [m for _, m in self.lines if needle in m]


def test_frame_note_format() -> None:
    assert sr._frame_note(1234, 12.4) == "frame=1234 age=12ms"
    assert sr._frame_note(0, float("inf")) == "frame=0 age=infms"


def test_gating_logs_carry_frame_anchor(env, monkeypatch) -> None:
    """门控判定的日志必须带帧号：没有它，门控判早了还是判晚了无从复查。

    日志时间戳只到秒且是墙钟，录制器是单调时钟——两个时基对不上，帧号是唯一的共同坐标。
    """
    mod, _, _ = env
    sink = _LogSink()
    monkeypatch.setattr(sr, "logger", sink)
    _set_graph(mod, [True, True, False, False])

    assert mod._drive(1) is True
    ready = sink.texts("已进入驾驶页")
    left = sink.texts("已离开对局")
    assert ready and "frame=" in ready[0]
    assert left and "frame=" in left[0]


def test_miss_sequence_is_logged_per_occurrence(env, monkeypatch) -> None:
    """每次锚点失配都留一行：连续失配序列才是"过场动画"与"真离场"的区别所在。"""
    mod, _, _ = env
    sink = _LogSink()
    monkeypatch.setattr(sr, "logger", sink)
    _set_graph(mod, [True, False, False])

    assert mod._drive_loop(1, None) is True
    misses = sink.texts("锚点失配")
    assert len(misses) == 2  # 容差是 2：两条都要留下，只留最后一条看不出序列
    assert all("frame=" in m for m in misses)


def test_frame_is_read_even_without_recorder(env) -> None:
    """未录制也每 tick 取帧（取证锚点的来源），且不落任何盘。"""
    mod, ctx, _ = env
    mod._record_mode = False
    _set_graph(mod, [True, True, False, False])

    assert mod._drive(1) is True
    assert ctx.capture.calls > 0
    assert not (sr.data_dir() / "speedrush" / "demos").exists()


# ---------- 感知接线（perception_mode） ----------


def test_drive_loop_with_perception_mode_runs(env) -> None:
    """感知模式开启时主循环的回归锁。

    2026-09-21 审查发现：调用点曾把 phase 多传给了 _ensure_perception()（无参方法），
    perception_mode 开启后每个 tick 都抛 TypeError——既有测试只覆盖了默认关闭的路径。
    预置 _perception 绕开模型加载，锁的是调用点与消费路径本身。
    """
    mod, _, _ = env
    calls: list[int] = []

    def _detect(frame, frame_id=0, ts_ns=0):
        calls.append(frame_id)
        # wait_ms/run_ms：等锁/run 分段记账列（2026-10-06 起）同样进节拍报告
        return SimpleNamespace(infer_ms=1.0, wait_ms=0.0, run_ms=1.0)

    mod._perception_mode = True
    mod._perception = SimpleNamespace(detect=_detect)
    _set_graph(mod, [True, False, False])

    assert mod._drive_loop(1, None) is True
    assert len(calls) >= 3  # 有帧的每个 tick 都应推理一次
    assert mod._last_perception is not None
    assert mod._infer_times  # 感知耗时进了节拍报告的记账
    assert mod._wait_times and mod._run_times


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
    # 场景分层字段由模块注入：阶段号来自调用点、回合号来自 _run_flow 的轮次计数
    meta = json.loads((sessions[0] / "meta.json").read_text(encoding="utf-8"))
    assert meta["phase"] == 1 and meta["round_no"] == 1


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


# ---------- 启动约束（按本次配置求值）----------


def test_record_mode_does_not_require_gamepad_capability() -> None:
    """录制模式不操纵车辆 → 不需要手柄能力；机器上没装 ViGEmBus 也该能录。"""
    m = sr.SpeedRushModule
    assert "gamepad" in m.required_capabilities({"record_mode": False})
    assert "gamepad" not in m.required_capabilities({"record_mode": True})
    # 不给配置 = 完整模式，按类属性声明
    assert m.required_capabilities(None) == m.REQUIRES


def test_record_mode_does_not_require_exclusive_gamepad() -> None:
    """录制演示数据正是要用物理手柄驾驶 → 不能要求断开手柄。"""
    m = sr.SpeedRushModule
    assert m.requires_exclusive_gamepad({"record_mode": False}) is True
    assert m.requires_exclusive_gamepad({"record_mode": True}) is False
    assert m.requires_exclusive_gamepad(None) is True


def test_startup_constraints_tolerate_truthy_config() -> None:
    """配置值来自 GUI/缓存，只认真值不认字面 True（别写成 `is True`）。"""
    m = sr.SpeedRushModule
    assert "gamepad" not in m.required_capabilities({"record_mode": 1})
    assert m.requires_exclusive_gamepad({"record_mode": 1}) is False
    assert "gamepad" in m.required_capabilities({})


def test_base_default_follows_class_attributes() -> None:
    """未覆盖这两个查询的模块行为不变：默认就是类属性的直接投影。"""
    from maaracing_master.core.base import ActivityModule

    assert ActivityModule.required_capabilities(None) == ActivityModule.REQUIRES
    assert ActivityModule.requires_exclusive_gamepad(None) == \
        ActivityModule.REQUIRES_GAMEPAD_EXCLUSIVE


# ---------- 直行基线档：全程接线（手柄下发 + trace + HUD 读数） ----------


class _FakeTimeFull:
    """time 模块的最小桩：monotonic 委托受控时钟，time/strftime 恒定
    （trace 与 hud 目录名由此确定）。"""

    def __init__(self, clock) -> None:
        self._clock = clock

    def time(self) -> float:
        return 1_000_000.0

    def monotonic(self) -> float:
        return self._clock.t

    def perf_counter(self) -> float:
        return self._clock.t

    def strftime(self, fmt, t=None):
        return "20261003_190000"

    def localtime(self, t):
        return t


class _StraightPadLease:
    """直行档手柄租约桩：记录下发，供断言。"""

    def __init__(self) -> None:
        self.joy: list = []
        self.trig: list = []
        self.updates = 0

    def left_joystick(self, x_value=0, y_value=0):
        self.joy.append((x_value, y_value))

    def right_trigger(self, value=0):
        self.trig.append(value)

    def update(self):
        self.updates += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _hud_stub(created: list):
    class _Hud:
        def __init__(self, out_dir, frame_source=None, phase=0, round_no=0):
            created.append(Path(out_dir))
            self.running = False

        def start(self):
            self.running = True

        def stop(self, reason="normal"):
            self.stopped = reason

    return _Hud


def test_straight_mode_full_run(env, monkeypatch, tmp_path) -> None:
    """直行基线档 _drive_loop 全程：接管手柄零杆满油门、trace 落盘、
    HUD 读数观察线程起停——HUD 是基线金币率的数据源，缺它基线测不了。"""
    mod, ctx, clock = env
    mod._straight_mode = True
    mod._running = True
    _set_graph(mod, [True, False, False])

    pad = _StraightPadLease()
    ctx.gamepad = SimpleNamespace(acquire=lambda: pad)
    monkeypatch.setattr(sr, "time", _FakeTimeFull(clock))
    monkeypatch.setattr(sr, "_control_trace_root", lambda: tmp_path)
    created: list = []
    monkeypatch.setattr(sr, "HudObserver", _hud_stub(created))

    assert mod._drive_loop(1, None) is True
    assert mod._hud is None  # 退出后释放引用（_state.hud_recording 不许悬挂为真）
    assert len(created) == 1
    assert created[0].name == "hud_20261003_190000_p1"  # 与 trace 同时刻命名
    assert pad.updates >= 3 and all(t == 255 for t in pad.trig)
    assert all(j == (0, 0) for j in pad.joy)
    # trace：STRAIGHT 行落盘
    files = list(tmp_path.glob("trace_20261003_190000_p1.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in
            files[0].read_text(encoding="utf-8").splitlines()]
    assert rows and all(r["state"] == "STRAIGHT" for r in rows)


def test_straight_mode_skips_perception(env, monkeypatch, tmp_path) -> None:
    """直行档即使 perception_mode 开着也不跑感知——基线度量的是无感知地板。"""
    mod, ctx, clock = env
    mod._straight_mode = True
    mod._perception_mode = True
    mod._running = True
    _set_graph(mod, [True, False, False])

    ctx.gamepad = SimpleNamespace(acquire=lambda: _StraightPadLease())
    calls: list = []

    def _detect(frame, frame_id=0, ts_ns=0):
        calls.append(frame_id)
        return SimpleNamespace(infer_ms=1.0)

    mod._perception = SimpleNamespace(detect=_detect)
    monkeypatch.setattr(sr, "time", _FakeTimeFull(clock))
    monkeypatch.setattr(sr, "_control_trace_root", lambda: tmp_path)
    monkeypatch.setattr(sr, "HudObserver", _hud_stub([]))

    assert mod._drive_loop(1, None) is True
    assert calls == []  # 一次推理都没跑
    assert mod._last_perception is None


def test_control_mode_also_records_hud(env, monkeypatch, tmp_path) -> None:
    """智能驾驶档（control_mode、非直行）也起 HUD 读数——验收②要智能 vs
    直行基线的分数流对比，两档的数据面必须对称。"""
    mod, ctx, clock = env
    mod._control_mode = True
    mod._running = True
    _set_graph(mod, [True, False, False])

    ctx.gamepad = SimpleNamespace(acquire=lambda: _StraightPadLease())
    monkeypatch.setattr(sr, "time", _FakeTimeFull(clock))
    monkeypatch.setattr(sr, "_control_trace_root", lambda: tmp_path)
    created: list = []
    monkeypatch.setattr(sr, "HudObserver", _hud_stub(created))
    monkeypatch.setattr(sr, "load_session", lambda w: None)  # 深度会话桩：不碰 GPU

    assert mod._drive_loop(1, None) is True
    assert len(created) == 1
    assert created[0].name == "hud_20261003_190000_p1"
    assert mod._hud is None  # 退出释放引用