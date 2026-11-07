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

    def time(self) -> float:
        # 录制影子接线后 _build_control_chain 也读 time.time（run_dir 命名锚）
        return self.t

    def strftime(self, fmt, t=None):
        # _run_dir 产物目录名由此确定（恒定值保测试确定性）
        return "20000101_000000"

    def localtime(self, t=None):
        return t


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
        self.debug = SimpleNamespace(enabled=True)   # debug/ 产物面总闸：默认开


class _FakeGraph:
    """按序列返回锚点命中结果；序列用尽后重复最后一个值。

    驾驶主循环每拍问两类锚点：驾驶页齿轮（结束去抖）与进商店横幅（阶段 1 提前结束）。
    故替身**按节点名分流**——齿轮走 ``seq``、横幅走 ``banner_seq``（默认全程 False，
    即"这一局没弹进商店横幅"，保持既有各用例只考齿轮失配去抖的原语义）。``calls``
    只数齿轮调用（既有断言口径），横幅调用另记 ``banner_calls``。
    """

    def __init__(self, seq: list[bool], banner_seq: list[bool] | None = None) -> None:
        self.seq = list(seq)
        self.banner_seq = list(banner_seq) if banner_seq is not None else [False]
        self.calls = 0
        self.banner_calls = 0

    def run(self, entry: str | None = None, reached: str | None = None,
            *, save: str | None = None) -> bool:
        if entry == sr.STORE_BANNER_NODE:
            v = self.banner_seq[min(self.banner_calls, len(self.banner_seq) - 1)]
            self.banner_calls += 1
            return v
        v = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        return v

    def probe(self, node: str, **kwargs):
        """只读页面查询：驾驶主循环改调它（`run` 仍用于就绪判据与导航）。

        计数口径不变——probe 内部复用 run 的分流逻辑，`calls` / `banner_calls`
        仍是「该节点被问了几次」。
        """
        hit = self.run(node, node)
        return sr.ProbeOutcome.HIT if hit else sr.ProbeOutcome.MISS


@pytest.fixture
def env(monkeypatch, tmp_path):
    """装好受控时钟与产物目录的模块实例工厂。"""
    clock = _Clock()
    monkeypatch.setattr(sr, "time", clock)
    # debug 产物与录制语料同源重定向（录制语料落在 debug/speedrush/）：否则走完整
    # 驾驶循环的用例会把假钟固定名（20000101_000000_p1）写进真实 %APPDATA% 目录
    monkeypatch.setattr(sr, "debug_dir", lambda: tmp_path / "debug")
    ctx = _FakeCtx(clock)
    mod = sr.SpeedRushModule(ctx)  # type: ignore[arg-type]
    mod._running = True  # 默认为"已启动"；需停机的用例自行覆盖
    mod._drive_mode = "depth"   # 本组测深度档循环/门控，不依赖默认档
    return mod, ctx, clock


def _set_graph(mod, seq: list[bool],
               banner_seq: list[bool] | None = None) -> _FakeGraph:
    g = _FakeGraph(seq, banner_seq)
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


def test_ready_confirmed_skips_recognition_entirely(env) -> None:
    """上一步刚用同一锚点确认过落地页 → 入口**一次识别都不跑**，直接进控制链。

    两次之间只有函数返回、没有点击或转场，画面不可能跳走；而起步倒计时期间
    车辆已可接受输入，再查一次就是白扔起步时间。
    """
    mod, _, _ = env
    g = _set_graph(mod, [False])          # 桩恒失配：若还查图，必然判不就绪
    assert mod._wait_drive_ready(1, confirmed=True) is True
    assert g.calls == 0


def test_ready_without_confirm_still_debounces(env) -> None:
    """断点续跑（无前置确认）仍要走完整去抖：两次连续命中才算就绪。"""
    mod, _, _ = env
    g = _set_graph(mod, [True, True])
    assert mod._wait_drive_ready(1) is True
    assert g.calls == sr.DRIVE_READY_HITS


def test_ready_without_confirm_restarts_on_miss(env) -> None:
    """去抖语义不变：中间一次失配就重新计数。"""
    mod, _, _ = env
    g = _set_graph(mod, [True, False, True, True])
    assert mod._wait_drive_ready(1) is True
    assert g.calls == 4


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


# ---------- 进商店横幅提前结束（阶段 1） ----------


def test_loop_ends_early_on_store_banner_phase1(env) -> None:
    """阶段 1 齿轮一直命中（未离场），但弹了进商店横幅 → 提前结束本阶段。

    锁的是"横幅=P1 结束信号"这条新判据：命中即返回 True，不必等齿轮消失后的
    失配去抖（省过场帧）。齿轮全程命中，故只能靠横幅这条路径才会结束。
    """
    mod, _, _ = env
    mod._running = True
    g = _set_graph(mod, [True], banner_seq=[False, True])  # 第一拍无横幅、第二拍命中
    assert mod._drive_loop(1, None) is True
    assert g.banner_calls >= 1  # 确实问过横幅


def test_loop_phase2_never_checks_store_banner(env) -> None:
    """阶段 2 不查进商店横幅：横幅文案专指"即将开始阶段 2"，阶段 2 结束进结算页。

    即便横幅恒命中，阶段 2 也不得据此结束——只走齿轮失配/超时判据（此处齿轮一直
    命中 → 超时返回 False），且一次都没问横幅。
    """
    mod, _, _ = env
    mod._running = True
    g = _set_graph(mod, [True], banner_seq=[True])
    assert mod._drive_loop(2, None) is False
    assert g.banner_calls == 0  # 阶段 2 连查都不查


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
    assert not (sr.debug_dir() / "speedrush").exists()


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

    sessions = sorted((sr.debug_dir() / "speedrush").iterdir())
    assert len(sessions) == 1
    # 命名规则：<运行编号>_manual_p<阶段>（时间戳恒在最前，标签按用途追加）
    assert sessions[0].name.endswith("_manual_p1")
    assert (sessions[0] / "meta.json").is_file()
    assert (sessions[0] / "frames.jsonl").is_file()
    # 场景分层字段由模块注入：阶段号来自调用点、回合号来自 _run_flow 的轮次计数
    meta = json.loads((sessions[0] / "meta.json").read_text(encoding="utf-8"))
    assert meta["phase"] == 1 and meta["round_no"] == 1


def test_record_mode_writes_single_session_dir(env) -> None:
    """人工驾驶阶段只产一个目录，且与程序控制局同一套布局。

    回归锁：录制期同一段画面曾被写两处——``data/speedrush/demos/manual_*``（语料）
    与 ``debug/speedrush/<时刻>_p<N>``（20Hz raw/hud 调试图），单阶段约 300MB。
    现在一个会话目录里就是同一套布局：``raw/`` 原帧 + 叠加图 + 统一索引，
    不再另建控制会话目录、也不往 ``data/`` 写。
    """
    mod, _, _ = env
    mod._record_mode = True
    mod._running = True
    _set_graph(mod, [True, True, True, False, False])

    assert mod._drive(1) is True
    root = sr.debug_dir() / "speedrush"
    sessions = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert len(sessions) == 1 and sessions[0].endswith("_manual_p1")
    session = root / sessions[0]
    # 原帧落 raw/，索引 file 指向会话根下的相对路径（与控制局同一口径）
    assert (session / "raw").is_dir()
    rows = [json.loads(line) for line in
            (session / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all((session / r["file"]).is_file() for r in rows)
    assert all(r["file"].startswith("raw/") for r in rows)
    # HUD 读数与控制局同一落点
    assert (session / "hud" / "hud.jsonl").is_file()
    # 深度录制只写 debug/：data/ 是用户资产与业务数据（宏、DB），训练语料不进那里
    assert not (sr.debug_dir().parent / "data" / "speedrush").exists()


def test_drive_without_record_mode_creates_no_session(env) -> None:
    mod, _, _ = env
    mod._record_mode = False
    # 就绪需连续两次命中；随后两次失配即判离场
    _set_graph(mod, [True, True, False, False])
    assert mod._drive(1) is True
    assert mod._recorder is None
    assert not (sr.debug_dir() / "speedrush").exists()


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
    # 录制语料根=调试目录下的 speedrush（人工语料与调试图同家，只此一处）
    assert state["record_dir"] == str(sr.debug_dir() / "speedrush")


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
        def __init__(self, out_dir, frame_source=None, phase=0, round_no=0,
                     group_id=None, persist=True):
            created.append(None if out_dir is None else Path(out_dir))
            self.running = False

        def start(self):
            self.running = True

        def stop(self, reason="normal"):
            self.stopped = reason

        def latest(self):
            return None   # 读数面契约（Real HudObserver 同名）；桩无样本

    return _Hud


def test_straight_mode_full_run(env, monkeypatch, tmp_path) -> None:
    """直行基线档 _drive_loop 全程：接管手柄零杆满油门、trace 落盘、
    HUD 读数观察线程起停——HUD 是基线金币率的数据源，缺它基线测不了。"""
    mod, ctx, clock = env
    mod._drive_mode = "straight"
    mod._running = True
    _set_graph(mod, [True, False, False])

    pad = _StraightPadLease()
    ctx.gamepad = SimpleNamespace(acquire=lambda: pad)
    monkeypatch.setattr(sr, "time", _FakeTimeFull(clock))
    monkeypatch.setattr(sr, "_run_dir", lambda *a, **k: tmp_path)
    created: list = []
    monkeypatch.setattr(sr, "HudObserver", _hud_stub(created))
    # trace 走流水汇（会话目录形态，日志闸管辖）：绑定本地 sink 验证落点
    from maaracing_master.core.trace_sink import TraceSink, validate_trace_file
    sink = TraceSink("speedrush")
    sink.bind_session(tmp_path, "t_1")
    mod._trace = sink

    assert mod._drive_loop(1, None) is True
    assert mod._hud is None  # 退出后释放引用（_state.hud_recording 不许悬挂为真）
    assert len(created) == 1
    assert created[0] == tmp_path / "hud"  # hud 落 run_dir 子目录（布局收口）
    expected = sr.load_decision().planner.throttle_raw
    assert 0 < expected < 255
    assert pad.updates >= 3 and all(t == expected for t in pad.trig)
    assert all(j == (0, 0) for j in pad.joy)
    # trace：STRAIGHT 行进流水汇 + 阶段边界随行
    sink.flush(2.0)
    p = tmp_path / "trace" / "speedrush.jsonl"
    rows = [json.loads(line) for line in
            p.read_text(encoding="utf-8").splitlines() if line.strip()]
    data = [r for r in rows if "type" not in r]
    assert data and all(r["state"] == "STRAIGHT" for r in data)
    assert any(r["type"] == "stage_begin" for r in rows)
    assert sink.close_session(2.0).ok
    assert validate_trace_file(p) == []


def test_straight_mode_skips_perception(env, monkeypatch, tmp_path) -> None:
    """直行档即使 perception_mode 开着也不跑感知——基线度量的是无感知地板。"""
    mod, ctx, clock = env
    mod._drive_mode = "straight"
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
    monkeypatch.setattr(sr, "_run_dir", lambda *a, **k: tmp_path)
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
    monkeypatch.setattr(sr, "_run_dir", lambda *a, **k: tmp_path)
    created: list = []
    monkeypatch.setattr(sr, "HudObserver", _hud_stub(created))
    monkeypatch.setattr(sr, "load_session", lambda w: None)  # 深度会话桩：不碰 GPU

    assert mod._drive_loop(1, None) is True
    assert len(created) == 1
    assert created[0] == tmp_path / "hud"  # hud 落 run_dir 子目录（布局收口）
    assert mod._hud is None  # 退出释放引用


def test_macro_replay_hud_clock_reads_full_scores(env, monkeypatch) -> None:
    """宏回放要记分：时钟观察器读完整 HUD；宏录制仍只读倒计时。"""
    mod, ctx, clock = env
    seen: list[dict] = []

    class _Hud:
        def __init__(self, out_dir, *, frame_source=None, phase=0, round_no=0,
                     group_id=None, persist=True, regions=None):
            seen.append({"out_dir": out_dir, "persist": persist,
                         "regions": regions})
            self.running = False

        def start(self):
            self.running = True

        def stop(self, reason="normal"):
            self.running = False

        def latest(self):
            return None

    monkeypatch.setattr(sr, "HudObserver", _Hud)
    assert mod._begin_hud_clock(1, 1, with_scores=True) is not None
    assert seen[0]["out_dir"] is None
    assert seen[0]["persist"] is False
    assert seen[0]["regions"] is None          # 全区域：计时 + 比分 + 速度
    assert mod._begin_hud_clock(1, 1) is not None
    assert seen[1]["regions"] and "timer" in seen[1]["regions"]


def test_control_hud_readout_runs_with_debug_off(env, monkeypatch, tmp_path) -> None:
    """debug 关只停落盘，不停读数：战绩库要末分与对手分。"""
    mod, ctx, clock = env
    mod._control_mode = True
    mod._running = True
    _set_graph(mod, [True, False, False])

    ctx.gamepad = SimpleNamespace(acquire=lambda: _StraightPadLease())
    ctx.debug.enabled = False
    monkeypatch.setattr(sr, "time", _FakeTimeFull(clock))
    monkeypatch.setattr(sr, "_run_dir", lambda *a, **k: tmp_path)
    created: list = []
    monkeypatch.setattr(sr, "HudObserver", _hud_stub(created))
    monkeypatch.setattr(sr, "load_session", lambda w: None)  # 深度会话桩：不碰 GPU

    assert mod._drive_loop(1, None) is True
    assert created == [None]  # 只读内存：不建调试目录、不落 hud.jsonl
    assert mod._hud is None


# ---------- 场边界 ----------


def test_view_guard_rearms_at_each_round(env, monkeypatch) -> None:
    """声望归零重置进度会把机位打回追尾：新一场必须重新确认视角。

    同场第二阶段沿用第一阶段的确认（局内相机不重置），不必重复按 RB。
    """
    mod, _, _ = env
    mod._view_ok = True           # 上一场已确认，不能被带进新一场
    monkeypatch.setattr(mod, "_start_preload", lambda: None)
    seen: list[tuple[int, bool]] = []
    stamps: list[str] = []

    def drive(phase, round_no=1, stamp=None, *, ready_confirmed=False):
        seen.append((round_no, mod._view_ok))
        stamps.append(stamp)
        mod._view_ok = True
        if round_no >= 2:
            mod._running = False   # 第二场第一段后收工，避免无限循环
        return True

    monkeypatch.setattr(mod, "_drive", drive)
    _set_graph(mod, [True])
    mod._run_flow(sr.LOOP_START_INDEX)
    assert seen[0] == (1, False)
    assert seen[1] == (1, True)
    assert seen[2] == (2, False)
    # 产物前缀按局划分：同局两阶段共用（p1/p2 能配对），两局之间必须不同，
    # 否则后一局会写进前一局的目录、把图整批覆盖
    assert stamps[0] == stamps[1]
    assert stamps[2] != stamps[0]


# ---------- 步级重试（大阶段之间的 UI 点击 → 落地页确认） ----------


class _StepGraph:
    """按节点名分流：``action_node`` 走动作（点击，恒 ``action_returns``），
    其余 entry 视为落地页确认探针，按 ``confirm_seq`` 返回（用尽后重复末值）。"""

    def __init__(self, action_node: str, confirm_seq: list[bool],
                 action_returns: bool = True) -> None:
        self.action_node = action_node
        self.confirm_seq = list(confirm_seq)
        self.action_returns = action_returns
        self.action_calls = 0
        self.confirm_calls = 0

    def run(self, entry: str | None = None, reached: str | None = None,
            *, save: str | None = None) -> bool:
        if entry == self.action_node:
            self.action_calls += 1
            return self.action_returns
        v = self.confirm_seq[min(self.confirm_calls, len(self.confirm_seq) - 1)]
        self.confirm_calls += 1
        return v


_ACT = "speedrush.点寻找对手"
_STAGE = "寻找对手"


def _tight_step(env, monkeypatch, *, retry_max: int = 1):
    """把步级机收紧到确定性、瞬时：窗 1s、半秒轮询、可控重发上限。

    剂量已进数据面（decision.json step_retry），故打桩 `load_decision` 喂一个只带
    `step_retry.dose` 的替身——_run_step 每步从它取 (max_retries, window_s, poll_s)。
    """
    mod, _, _ = env
    monkeypatch.setattr(sr, "load_decision", lambda: SimpleNamespace(
        step_retry=SimpleNamespace(dose=lambda _stage: (retry_max, 1.0, 0.5))))
    return mod


def test_step_advances_when_confirm_hits(env, monkeypatch) -> None:
    """落地页确认命中 → 一次点击即 DONE 推进，不重发。"""
    mod = _tight_step(env, monkeypatch)
    g = _StepGraph(_ACT, [True])
    mod._graph = g
    assert mod._run_step(_ACT, _STAGE) is True
    assert g.action_calls == 1 and g.confirm_calls == 1


def test_step_retries_same_click_then_fails_loudly(env, monkeypatch) -> None:
    """确认始终未命中 → 按上限重发**同一动作**，达上限后再等一窗显式终止本轮（非静默）。"""
    mod = _tight_step(env, monkeypatch, retry_max=1)
    sink = _LogSink()
    monkeypatch.setattr(sr, "logger", sink)
    g = _StepGraph(_ACT, [False])
    mod._graph = g
    assert mod._run_step(_ACT, _STAGE) is False
    assert g.action_calls == 2          # 首点 + 1 次重发 = STEP_RETRY_MAX + 1
    assert sink.texts("仍未落地")        # 耗尽有显式 WARNING，不得静默卡死


def test_step_hit_after_miss_advances_without_retry(env, monkeypatch) -> None:
    """转场慢：首窗内确认未命中→窗内轮询到命中，仍算同一次尝试成功、**不重发**（不误点第二下）。"""
    mod = _tight_step(env, monkeypatch, retry_max=3)
    g = _StepGraph(_ACT, [False, False, True])  # 前两次探针未命中、第三次命中（窗内）
    mod._graph = g
    assert mod._run_step(_ACT, _STAGE) is True
    assert g.action_calls == 1          # 只在发出首点时点了一次，窗内命中没触发重发
    assert g.confirm_calls == 3


def test_step_exits_on_stop_without_retry_or_fail(env, monkeypatch) -> None:
    """停止 ≠ 失败：中途收到停止立即退出，既不重发、也不记成"耗尽"。"""
    mod, ctx, _ = env
    _tight_step(env, monkeypatch, retry_max=5)
    sink = _LogSink()
    monkeypatch.setattr(sr, "logger", sink)
    g = _StepGraph(_ACT, [False])       # 确认永远未命中（若不因停止退出会一路重发到耗尽）
    mod._graph = g
    orig_run = g.run

    def run_then_stop(entry=None, reached=None, **kwargs):
        r = orig_run(entry, reached, **kwargs)
        if entry != _ACT:               # 首次确认探针后即模拟 stop() 落停止标志
            mod._running = False
        return r

    g.run = run_then_stop
    assert mod._run_step(_ACT, _STAGE) is False
    assert g.action_calls == 1          # 只点了首下，停止即退，没走重发
    assert not sink.texts("仍未落地")    # 停止不是耗尽，不得落"耗尽"日志


def test_every_click_stage_has_confirm_mapping() -> None:
    """防漂移：FLOW 里每个点击步都必须有落地页确认映射（驾驶阶段 node None 除外）。"""
    for stage, node, _phase in sr.FLOW:
        if node is not None:
            assert stage in sr.STEP_CONFIRM, f"点击步「{stage}」缺确认锚点映射"


def test_step_confirm_nodes_exist_in_pipeline() -> None:
    """防漂移：STEP_CONFIRM 指向的确认锚点节点都必须在生成的真源里存在。"""
    graph = json.loads(
        (Path(sr.PIPELINE_DIR) / "speedrush.json").read_text(encoding="utf-8"))
    for stage, dest in sr.STEP_CONFIRM.items():
        assert dest in graph, f"阶段「{stage}」的确认锚点 {dest} 不在真源"


def test_action_nodes_skip_framework_default_pace() -> None:
    """动作节点不得吃框架默认节拍（1s 地板 + 前后各 200ms 隐式等待）。

    2026-11-06 实机日志里 p1→p2 两次点击步分别耗 4s / 2s，按默认值推算其中
    约 2.4s 是这三项固定等待。rate_limit 取 100ms 而不是 0：0 会让「按钮晚出现」
    时框架在 20s 超时内全速截图找图（不失败，但白烧 CPU 与日志）。timeout 未动：
    按官方协议它约束的是 next 列表识别超时，不是等按钮出现。
    """
    graph = json.loads(
        (Path(sr.PIPELINE_DIR) / "speedrush.json").read_text(encoding="utf-8"))
    for stage, node, _phase in sr.FLOW:
        if node is None:
            continue
        data = graph[node]
        assert data.get("rate_limit") == 100, f"「{stage}」吃回框架默认节拍或全速空转"
        assert data.get("pre_delay") == 0, f"「{stage}」带隐式前置等待"
        assert data.get("post_delay") == 0, f"「{stage}」带隐式后置等待"
        assert "timeout" not in data, f"「{stage}」不该自带短超时"


def test_action_settle_spec_is_same_source_as_recognition() -> None:
    """点击后条件等待的判据必须与节点 recognition 同源。

    2026-11-06 实机（nav 帧 894→895）：按钮 ROI 分数 0.814→0.250 与驾驶页齿轮
    0.882 出现在同一帧，检测零滞后；滞后来自点击后固定的 800ms 睡眠让节点挂着。
    条件等待用「源按钮认不出来」当边沿判据，规格直接复制 recognition——阈值若
    两处漂移，判据会在按钮还在时提前收工或按钮已走时白等。
    """
    graph = json.loads(
        (Path(sr.PIPELINE_DIR) / "speedrush.json").read_text(encoding="utf-8"))
    for stage, node, _phase in sr.FLOW:
        if node is None:
            continue
        param = graph[node]["action"]["param"]
        assert param["custom_action"] == "MaaRM_Click", f"「{stage}」不是点击节点"
        reco = graph[node]["recognition"]["param"]["custom_recognition_param"]
        settle = param["custom_action_param"]["settle"]
        for key in ("rect", "templates", "threshold", "colorspace"):
            assert settle[key] == reco[key], f"「{stage}」settle.{key} 与识别规格漂移"
        assert 0.0 < settle["threshold"] <= 1.0


def test_step_retry_dose_not_traded_for_speed() -> None:
    """提速不得靠缩窗口 / 缩轮询 / 减重发上限换：实机最长落地 9s，12s 窗只剩 3s 余量。

    点击变快（固定 800ms → 条件等待最快几十毫秒）只是把时间还给等待窗口，
    不推进 StepMachine 的计数；真正吃次数的是 window_s 变短。这里用同一台机器
    模拟两种点击耗时 × 三种落地耗时，锁住「余量只增不减」。
    """
    from maaracing_master.core.step_machine import Command, StepMachine, Verdict

    policy = json.loads((Path(sr.__file__).parent / "resources" / "policy"
                         / "decision.json").read_text(encoding="utf-8"))
    dose = policy["step_retry"]
    assert dose["max_retries"] >= 3
    assert dose["window_s"] >= 12.0
    assert dose["poll_s"] >= 0.5
    assert dose["by_stage"] == {}, "别为提速给某一步偷偷缩窗"

    def simulate(click_s: float, land_s: float) -> int:
        """返回重发次数；-1 = 判失败。纯假钟，不依赖真实等待。"""
        now = [0.0]
        machine = StepMachine(max_retries=dose["max_retries"],
                              window_s=dose["window_s"],
                              clock=lambda: now[0])
        machine.arm()                       # 点击发出：只重置窗口、不动计数
        now[0] += click_s
        resends = 0
        for _ in range(10000):
            landed = now[0] >= land_s
            cmd = machine.observe(Verdict.SUCCESS if landed else Verdict.PENDING)
            if cmd is Command.DONE:
                return resends
            if cmd is Command.FAIL:
                return -1
            if cmd is Command.RETRY:
                resends += 1
                now[0] += click_s
                machine.arm()
            else:
                now[0] += dose["poll_s"]
        return -2

    for click_s in (0.8, 0.05):           # 旧固定睡 / 条件等待提前返回
        for land_s in (9.0, 12.0):       # 实机最长落地 9s + 窗口边界
            assert simulate(click_s, land_s) == 0, (
                f"点击 {click_s}s、落地 {land_s}s 不该消耗重发次数")
        assert 0 <= simulate(click_s, 15.0) <= 1  # 超窗才允许一次重发


# ---------- 阶段切换：落地确认只复用紧邻的那一次 ----------


def _flow_probe(env, monkeypatch, start_from: str):
    """跑一小段 FLOW，记录驾驶入口拿到的 ready_confirmed。"""
    mod, _, _ = env
    monkeypatch.setattr(sr, "_open_grp", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_start_preload", lambda: None)
    _set_graph(mod, [True])
    seen = []

    def fake_step(node, stage):
        seen.append(("step", stage))
        return True

    def fake_drive(phase, round_no=1, stamp=None, *, ready_confirmed=False):
        seen.append(("drive", phase, ready_confirmed))
        mod._running = False
        return True

    monkeypatch.setattr(mod, "_run_step", fake_step)
    monkeypatch.setattr(mod, "_drive", fake_drive)
    mod._run_flow(sr.resolve_start_index(start_from))
    return seen


def test_flow_reuses_adjacent_drive_confirm(env, monkeypatch) -> None:
    """紧邻上一步的落地确认锚点就是驾驶页锚点 → 驾驶入口复用它。"""
    seen = _flow_probe(env, monkeypatch, "确认上阵2")
    assert ("step", "确认上阵2") in seen
    assert ("drive", 2, True) in seen


def test_flow_starting_at_drive_stage_debounces(env, monkeypatch) -> None:
    """断点续跑直接落在驾驶阶段时没有前置确认 → 仍走完整去抖。"""
    seen = _flow_probe(env, monkeypatch, "驾驶阶段2")
    assert seen and seen[0] == ("drive", 2, False)


# ---------- 路心读数参考系跳变标注（classify_ro_hop，仅记录） ----------

def test_ro_hop_flags_impossible_motion_as_frame_change():
    """0.17s 跳 1.07 道（≈6.3 道/s）远超物理包络 → 换档，不是运动。

    223955 局 s3 25.4s 实拍：换档被离线当成横移真值，车模辨识因此矛盾。"""
    out = sr.classify_ro_hop(10.0, -1.57, "wall", 10.17, -0.50, "wall")
    assert out["hop"] == 1
    assert out["v"] == pytest.approx(6.29, abs=0.01)
    assert out["src_chg"] == 0           # 同源也可以是换档（换箱/换对）


def test_ro_hop_accepts_fast_real_transit_as_motion():
    """0.7s 移动 1.25 道（≈1.8 道/s）在物理包络内 → 运动。"""
    out = sr.classify_ro_hop(26.0, -0.41, "memory", 26.7, 0.84, "wall")
    assert out["hop"] == 0
    assert out["src_chg"] == 1           # 来源变化单独记录，不影响判据


def test_ro_hop_noise_floor_tolerates_single_tick_jitter():
    """一拍 0.05s 跳 0.35 道：隐含 7 道/s，但只超噪声地板内 → 运动。

    短间隔的差分被读数抖动主导（0.25 道地板），不能当换档。"""
    out = sr.classify_ro_hop(26.22, 0.98, "wall", 26.28, 1.33, "wall")
    assert out["hop"] == 0


def test_ro_hop_blackout_when_gap_too_long():
    """间隔 >1s：期间车可能任意移动，真值不可用（hop=2，v=None）。"""
    out = sr.classify_ro_hop(10.0, 0.0, "wall", 11.5, 1.8, "wall")
    assert out["hop"] == 2
    assert out["v"] is None


def test_ro_hop_signed_direction_preserved():
    """隐含速度带方向：向左的大跳也判换档（不能只查绝对值门槛）。"""
    out = sr.classify_ro_hop(10.0, 2.03, "wall", 10.05, 0.15, "wall")
    assert out["hop"] == 1
    assert out["v"] < 0
