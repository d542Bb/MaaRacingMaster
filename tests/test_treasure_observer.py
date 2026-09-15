# -*- coding: utf-8 -*-
"""观察通路（帧供给与决策段解耦）行为锁定。

契约出处：docs/plan/observe-split-plan.md。要钉死的是四件事——
  I1 观察线程不产生第二个大脑：不得调 `_treasure_kwargs()`（内含决策入口
     `_resolve_action_target()`），也不碰检测/OCR/状态机。
  I2 帧号只有一个主人 = 观察线程；决策段 `_tick_once` 只发布 HUD 快照引用。
  A3/A4/A5 无快照也出图、队列满丢帧不抛、debug+peep 全关不干活。
"""
from __future__ import annotations

import threading
from collections import deque
from queue import Queue
from types import SimpleNamespace

import numpy as np
import pytest

# module 经 core.window_utils 顶层 import maa.toolkit，CI 轻依赖环境下收集期 ERROR
# 会中断整个 pytest 会话；按仓内既有口径整文件优雅跳过。
try:
    from maaracing_master.plugins.treasure.module import TreasureModule

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)
    TreasureModule = None

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)

_FRAME = np.zeros((720, 1280, 3), dtype=np.uint8)


class _FakeSelf:
    """最小观察态桩：只喂观察通路读到的字段。"""

    STAGE_ORDER = getattr(TreasureModule, "STAGE_ORDER", ("游戏大厅",))
    OBSERVE_INTERVAL_MS = getattr(TreasureModule, "OBSERVE_INTERVAL_MS", 150.0)
    PEEP_ONLY_INTERVAL_MS = getattr(TreasureModule, "PEEP_ONLY_INTERVAL_MS", 50.0)
    STAGE_JUDGE_INTERVAL_MS = getattr(TreasureModule, "STAGE_JUDGE_INTERVAL_MS", 300.0)
    IO_QUEUE_MAX = getattr(TreasureModule, "IO_QUEUE_MAX", 8)

    def __init__(self, *, debug_on=True, peep_on=False, saving=True):
        self.ctx = SimpleNamespace(
            debug=SimpleNamespace(enabled=debug_on, peep_enabled=peep_on),
            capture=SimpleNamespace(screenshot=None),
            click_mode="gamepad",
        )
        self._session_dir = "session" if saving else None
        self._raw_dir = "raw" if saving else None
        self._io_queue = Queue(maxsize=self.IO_QUEUE_MAX)
        self._saved_frames = 0
        self._debug_saved = 0
        self._last_debug_kwargs = None
        self._current_stage = "游戏大厅"
        self._round_no = None
        self._note = ""
        self._detector = None                    # 桩不支持真检测：判定分支应在 detector 为 None 时直接跳过
        self._obs_slot: tuple | None = None      # 观察线程阶段判定槽（C3，整体替换引用）
        self._last_judge_at = 0.0                # 判定节拍门控（monotonic 秒水位）
        # 性能仪表字段：`_io_submit` 会写 io 三项，`_tick_once` 会写 tick 两项。
        # 桩里必须给齐，否则仪表一加、这批契约测试就红——正是仪表想暴露的"静默"的反面。
        self._io_enqueued = 0
        self._io_dropped = 0
        self._io_queue_peak = 0
        self._last_tick_at = None
        self._tick_gap_ms = deque(maxlen=getattr(TreasureModule, "PERF_WINDOW", 200))
        self._cpu_pct_win = deque(maxlen=getattr(TreasureModule, "PERF_WINDOW", 200))
        self._cpu_last = None
        self.screenshot_calls = 0
        # 决策流水写手：落点由「日志记录」开关决定（测试里未开 → 不落盘）
        self._trace_writer = None
        self._trace_sink = None

        def _shot():
            self.screenshot_calls += 1
            return _FRAME

        self.ctx.capture.screenshot = _shot
        # 观察通路的两个方法跑真身（本测试锁的就是它们的行为）
        self._io_submit = TreasureModule._io_submit.__get__(self)
        self._observe_kwargs = TreasureModule._observe_kwargs.__get__(self)
        # CPU 采样也是真身：`_tick_once` 每帧都会调它，桩不给就会炸在这里
        self._sample_cpu = TreasureModule._sample_cpu.__get__(self)
        # 写手落点管理也是真身：_tick_once 每帧按「日志记录」开关决定 trace 是否落盘
        self._ensure_trace_sink = TreasureModule._ensure_trace_sink.__get__(self)

    def _treasure_kwargs(self, **_kw):
        raise AssertionError("观察线程不得调用决策段的快照构造（不变量 I1）")


def test_observer_frames_flow_without_any_decision_tick():
    """厅类阶段一帧决策都没有，观察通路仍按节拍出图并自增帧号。"""
    fake = _FakeSelf()
    for _ in range(3):
        TreasureModule._observe_tick_once(fake)
    assert fake._saved_frames == 3
    assert fake._debug_saved == 3
    tasks = [fake._io_queue.get_nowait() for _ in range(3)]
    assert [t[0] for t in tasks] == ["frame"] * 3
    assert [t[2] for t in tasks] == [1, 2, 3]      # raw 帧号连续
    assert [t[3] for t in tasks] == [1, 2, 3]      # rendered 帧号连续


def test_decision_tick_publishes_snapshot_but_owns_no_frame_number():
    """_tick_once 只发布快照引用：不再分配帧号、不再入队（帧号单主人）。"""
    fake = _FakeSelf()
    marker = {"treasure_stage": "第1回合出价"}
    published = []

    def _kwargs(**kw):
        published.append(kw)
        return marker

    fake._trace_writer = SimpleNamespace(write=lambda *_a: None)
    fake._frame_counter = 7                # 跳过首帧尺寸校验分支
    fake.DEBUG_LOG_INTERVAL = 999          # 跳过心跳段（它读一整套决策字段）
    fake._round_elapsed = 0
    fake._last_detection_result = None
    fake._daily_loop_limit_reached = lambda: False
    fake._detect_change = lambda _f: True
    fake._treasure_kwargs = _kwargs
    for name in ("_consume_stage_slot", "_run_appraiser_choice", "_run_session_choice",
                 "_run_bidding_choice", "_run_ocr", "_decision_phase"):
        assert hasattr(TreasureModule, name), name   # 决策段步骤改名 → 本锁必须失效提醒
        setattr(fake, name, lambda *_a, **_k: None)

    TreasureModule._tick_once(fake)

    assert published and published[0]["extra_note"] == "画面变化"
    assert fake._last_debug_kwargs is marker
    assert fake._saved_frames == 0 and fake._debug_saved == 0
    assert fake._io_queue.empty()


def test_observer_overrides_frame_index_and_survives_missing_snapshot():
    """有快照时只换帧号；厅内无快照时给最小快照，两者帧号都必须与自增值一致。"""
    fake = _FakeSelf()
    fake._last_debug_kwargs = {"treasure_stage": "第1回合出价",
                               "treasure_frame_index": 999,
                               "treasure_debug_index": 999,
                               "treasure_player_bids": {"玩家1": [1, 2]}}
    TreasureModule._observe_tick_once(fake)
    _kind, _img, idx, didx, _label, kwargs = fake._io_queue.get_nowait()
    assert kwargs["treasure_frame_index"] == idx == 1
    assert kwargs["treasure_debug_index"] == didx == 1
    assert kwargs["treasure_player_bids"] == {"玩家1": [1, 2]}
    assert fake._last_debug_kwargs["treasure_frame_index"] == 999, "发布件不得被改写"

    bare = _FakeSelf()
    TreasureModule._observe_tick_once(bare)
    _kind, _img, idx, didx, _label, kwargs = bare._io_queue.get_nowait()
    assert kwargs["treasure_frame_index"] == idx == 1
    assert kwargs["treasure_stage"] == "游戏大厅"


def test_observer_drops_frames_when_queue_full():
    """队列满 → 丢帧不抛（观测降密度），产帧方永不被阻塞；且**丢帧必须留下计数**。

    计数是本测试的后半段意义：曾经 `except Full: pass` 完全静默，一场跑完只能拿
    声称帧号减盘上文件数倒推丢了多少（真机 824 产 / 712 落盘 = 13.6%）。
    """
    fake = _FakeSelf()
    fake._io_queue = Queue(maxsize=1)
    TreasureModule._observe_tick_once(fake)
    TreasureModule._observe_tick_once(fake)      # 第二次入队时队列已占满？worker 未取 → 丢
    assert fake._saved_frames == 2
    assert fake._io_queue.qsize() == 1
    assert (fake._io_enqueued, fake._io_dropped) == (1, 1)
    assert fake._io_queue_peak == 1


def test_observer_idle_when_both_sinks_off():
    """debug 与 peep 全关：存图/预览两个出口休眠，但阶段判定消费者恒在（C6）——

    不再返回 None，改为判定档（STAGE_JUDGE_INTERVAL_MS）；仍截帧做判定但
    不 copy、不入队。这是修厅类阶段冻结的前提（GUI 阶段条 / trace 恒在消费者）。
    """
    fake = _FakeSelf(debug_on=False, peep_on=False, saving=False)
    assert TreasureModule._observe_interval_s(fake) == pytest.approx(
        TreasureModule.STAGE_JUDGE_INTERVAL_MS / 1000.0)
    TreasureModule._observe_tick_once(fake)
    assert fake.screenshot_calls >= 1            # 仍取帧（判定消费者）
    assert fake._io_queue.empty()                # 但不入队（存图/peep 出口都关）
    assert fake._saved_frames == 0


def test_observer_judge_writes_slot_not_state_machine():
    """观察线程判定只写槽，不改状态机；决策段消费槽 → 变更驱动 set_stage 恰一次。"""
    fake = _FakeSelf()
    calls = []

    def _set_stage(stage, reason="", raw_round=None):
        calls.append((stage, reason, raw_round))
        fake._current_stage = stage   # 与真身 set_stage 一致：切换后状态机推进 → 幂等成立

    fake.set_stage = _set_stage
    # 模拟观察线程判定结果写槽（_judge_stage_into_slot 的产物）
    fake._obs_slot = ("待机", None, 1)
    # 观察侧：GUI 读槽即得新阶段
    assert TreasureModule.current_stage.fget(fake) == "待机"
    # 决策段尚未消费 → _current_stage 未动
    assert fake._current_stage == "游戏大厅"
    assert calls == []
    # 决策段消费一次 → set_stage 变更驱动触发
    TreasureModule._consume_stage_slot(fake, _FRAME)
    assert calls == [("待机", "观察判定", None)]
    # 同槽再消费（决策段每帧都读槽）→ 幂等，不再触发
    TreasureModule._consume_stage_slot(fake, _FRAME)
    assert calls == [("待机", "观察判定", None)]


def test_peep_only_mode_uses_fast_interval_and_no_frame_number():
    """仅预览：走 20fps 挡、kind=peep、不占用落盘帧号。"""
    fake = _FakeSelf(debug_on=False, peep_on=True, saving=False)
    assert TreasureModule._observe_interval_s(fake) == pytest.approx(
        TreasureModule.PEEP_ONLY_INTERVAL_MS / 1000.0)
    TreasureModule._observe_tick_once(fake)
    kind, _img, idx, didx, _label, _kw = fake._io_queue.get_nowait()
    assert kind == "peep"
    assert (idx, didx) == (0, 0)
    assert fake._saved_frames == 0


# ---------- 启动判据（真机 2026-09-15 两场会话暴露的缺口） ----------
#
# 上面那批契约测试全是**方法级**：直接调 _observe_tick_once / _observe_interval_s，
# 因此"观察线程到底有没有被启动"从未被钉住。两场真机日志暴露了两处判据过期：
#   173823：不开 PEEP/Debug 跑会话 → 观察线程根本没起 → 阶段判定无帧可判；
#   173229：会话途中打开 PEEP → 无第二个启动点 → 全程无预览。


def test_session_workers_always_start_observer_even_when_all_sinks_off():
    """两个出口全关，观察线程也必须起：它是阶段判定的唯一生产者。

    观察线程是 `_obs_slot` 的唯一写者，决策段 `_consume_stage_slot` 读不到槽就直接
    返回 → 阶段永不推进，整个流程停摆。旧判据把它绑在 debug/peep 开关上，
    全关会话的阶段判定从未跑起来过（与 _observe_interval_s 声明的 C6 直接冲突）。
    """
    fake = _FakeSelf(debug_on=False, peep_on=False, saving=False)
    started = []
    fake._start_io_worker = lambda: started.append("io")
    fake._start_observer = lambda: started.append("observer")

    TreasureModule._start_session_workers(fake)

    assert started == ["observer"], "全关时观察线程仍须启动（阶段判定恒在）"


def test_session_workers_start_io_worker_only_when_sink_on():
    """IO worker 仍按出口开关（全关不养空转线程），且先于观察线程起。"""
    for debug_on, peep_on, saving in ((True, False, True), (False, True, False)):
        fake = _FakeSelf(debug_on=debug_on, peep_on=peep_on, saving=saving)
        started = []
        fake._start_io_worker = lambda: started.append("io")
        fake._start_observer = lambda: started.append("observer")

        TreasureModule._start_session_workers(fake)

        assert started == ["io", "observer"], (debug_on, peep_on)


def test_ensure_io_worker_backfills_when_sink_opened_mid_session():
    """会话途中打开 Debug/PEEP：观察循环补启 IO worker，不必重开会话。

    真机 173229：第一次会话途中开 PEEP 全程无预览，重开会话（启动时已开）才出图。
    """
    fake = _FakeSelf(debug_on=False, peep_on=False, saving=False)
    calls = []
    fake._start_io_worker = lambda: calls.append("io")

    TreasureModule._ensure_io_worker(fake)
    assert calls == [], "出口全关：不养空转线程"

    fake.ctx.debug.peep_enabled = True
    TreasureModule._ensure_io_worker(fake)
    assert calls == ["io"], "打开出口：补启 IO worker"

    fake.ctx.debug.enabled = True
    TreasureModule._ensure_io_worker(fake)
    assert calls == ["io", "io"], "每 tick 复查一次（幂等由 _start_io_worker 内部保证）"


def test_observer_loop_backfills_io_worker():
    """补启的真实调用点在观察循环里（每 tick 复查），不是只在启动路径。"""
    fake = _FakeSelf(debug_on=False, peep_on=True, saving=False)
    fake._observe_stop = threading.Event()
    fake._observe_interval_s = lambda: 0.01
    fake._ensure_io_worker = TreasureModule._ensure_io_worker.__get__(fake)
    calls = []
    fake._start_io_worker = lambda: calls.append("io")

    def _tick_once():
        fake._observe_stop.set()   # 跑一轮即收工，避免测试卡在真循环里

    fake._observe_tick_once = _tick_once

    TreasureModule._observer_loop(fake)

    assert calls == ["io"]
