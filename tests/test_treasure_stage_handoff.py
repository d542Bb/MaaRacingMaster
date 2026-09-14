# -*- coding: utf-8 -*-
"""转阶段交接与 PEEP 手柄诊断层的回归锁（真机 2026-09-15 MaaRM_20260915_071523）。

三处证据：
1. 回合切换（第1→2 于 07:17:02、第2→3 于 07:17:23）后各落一条
   `事件: click — 方式=? state=None key=None 归一化=(0.000,0.000)`：在途点击的记录
   已被 `set_stage` 清空，结果却被当成功应用（无主结果必须丢弃）。
2. 同一时刻新回合第一次点击被旧在途导航推迟（07:17:23 换回合、07:17:28 才发出
   本回合第一次点击）→ 回合切换必须中止在途导航。
3. 07:17:34–07:17:58 数字键链路空转 24s，PEEP 叠加层既无手柄光标位也无候选：
   空闲期数据被陈旧闸吞掉，且 `treasure_action` 为空时整层被渲染器早退吞掉。
"""
from __future__ import annotations

import time
import types

import numpy as np
import pytest

from maaracing_master.core.clicker import Clicker


def _load_module_class():
    try:
        from maaracing_master.plugins.treasure.module import TreasureModule
    except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时跳过
        pytest.skip(f"需要完整运行时依赖（maa/…）：{exc}")
    return TreasureModule


def _load_renderer_class():
    try:
        from maaracing_master.plugins.treasure.renderer import TreasureDebugRenderer
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"需要渲染依赖（cv2/…）：{exc}")
    return TreasureDebugRenderer


# ---------------------------------------------------------------------------
# 一、无主点击结果：丢弃，不应用副作用
# ---------------------------------------------------------------------------

class _StubClicker:
    def __init__(self, result=None, *, busy=False, mode="gamepad", bound=True):
        self.mode = mode
        self.gamepad_bound = bound
        self._result = result
        self._busy = busy
        self.cancelled = 0

    def consume_result(self):
        res, self._result = self._result, None
        return res

    def is_busy(self):
        return self._busy

    def cancel(self):
        self.cancelled += 1


class _ClickSelf:
    """只喂 `_consume_click_result` 读到的字段，被测方法为真身。"""

    def __init__(self, clicker, pending):
        mod = _load_module_class()
        self._consume_click_result = mod._consume_click_result.__get__(self)
        self._clicker = clicker
        self._clicker_stub = clicker
        self._pending_click = pending
        self._trace_writer = None
        self._frame_counter = 1
        self._current_stage = "第3回合出价"
        self.applied: list = []

    def _get_clicker(self):
        return self._clicker_stub

    def _apply_click_success(self, pending):
        self.applied.append(("ok", pending))

    def _apply_click_failure(self, res, pending):
        self.applied.append(("fail", pending))


def test_orphan_click_result_is_dropped_without_side_effects():
    """回合切换后回的无主结果：既不应用成功/失败副作用，也不落伪事件。"""
    clicker = _StubClicker(result={"type": "click", "ok": True})
    fake = _ClickSelf(clicker, pending=None)
    fake._consume_click_result()
    assert fake.applied == [], "无主结果被当成成功应用（会产生 方式=?/key=None 伪事件）"
    assert fake._pending_click is None


def test_normal_click_result_still_applies_side_effects():
    """不变式：有主（pending 在）的结果照常走成功副作用。"""
    clicker = _StubClicker(result={"type": "click", "ok": True})
    pending = {"key": "bid_main_red_btn", "state": "S2_bid", "fp": ("k",),
               "center": (0.4, 0.8), "mode_label": "后台手柄+A"}
    fake = _ClickSelf(clicker, pending=pending)
    fake._consume_click_result()
    assert fake.applied == [("ok", pending)]


# ---------------------------------------------------------------------------
# 二、回合切换：中止在途导航（结果交本帧 consume 消化）
# ---------------------------------------------------------------------------

class _AbortSelf:
    def __init__(self, clicker):
        mod = _load_module_class()
        self._abort_inflight_nav = mod._abort_inflight_nav.__get__(self)
        self._clicker = clicker


def test_round_switch_aborts_inflight_gamepad_nav():
    clicker = _StubClicker(busy=True, mode="gamepad")
    _AbortSelf(clicker)._abort_inflight_nav("回合切换")
    assert clicker.cancelled == 1, "回合切换未中止在途导航 → 新回合首点被排队"


def test_abort_is_noop_when_slot_idle_or_real_mode():
    idle = _StubClicker(busy=False)
    _AbortSelf(idle)._abort_inflight_nav("回合切换")
    assert idle.cancelled == 0

    real = _StubClicker(busy=True, mode="real")
    _AbortSelf(real)._abort_inflight_nav("回合切换")
    assert real.cancelled == 0, "real 点击帧内同步完成，不存在跨回合在途导航"

    unbound = _StubClicker(busy=True, bound=False)
    _AbortSelf(unbound)._abort_inflight_nav("回合切换")
    assert unbound.cancelled == 0


# ---------------------------------------------------------------------------
# 三、PEEP 手柄层：空闲保底 + 与点击意图解耦
# ---------------------------------------------------------------------------

class _PeepClicker:
    def __init__(self, *, prog, pos, age=7.5, bound=True):
        self.gamepad_bound = bound
        self._prog = prog
        self._pos = pos
        self._age = age

    def nav_progress(self):
        return dict(self._prog) if self._prog is not None else None

    def gamepad_cursor_pos(self):
        return self._pos

    def gamepad_cursor_age_s(self):
        return self._age


class _PeepSelf:
    def __init__(self, clicker):
        mod = _load_module_class()
        self._gamepad_nav_progress_kwargs = mod._gamepad_nav_progress_kwargs.__get__(self)
        self._clicker = clicker


def test_idle_nav_keeps_last_cursor_with_stale_marker():
    """导航结束（stage=done）后仍给出最后识别位 + stale/age_s，不再整层空白。"""
    clicker = _PeepClicker(
        prog={"seq": 9, "stage": "done", "pos": None, "target": (1100, 600)},
        pos=(1080, 590), age=7.5)
    kw = _PeepSelf(clicker)._gamepad_nav_progress_kwargs()
    assert kw is not None, "空闲期返回 None → 叠加层无光标位可看"
    assert kw["pos"] == (1080, 590)
    assert kw["stale"] is True
    assert kw["age_s"] == 7.5
    assert kw["target"] == (1100, 600)


def test_active_nav_progress_passes_through_unchanged():
    """不变式：导航中的实时快照原样透出（不被 idle 分支改写）。"""
    live = {"seq": 10, "stage": "micro", "pos": (700, 400), "target": (760, 390),
            "dist": 61.0, "ok": None, "done": False}
    kw = _PeepSelf(_PeepClicker(prog=live, pos=(700, 400)))._gamepad_nav_progress_kwargs()
    assert kw == live


def test_idle_without_any_detection_returns_none():
    """从未识别到光标 → None：不编造位置。"""
    clicker = _PeepClicker(prog={"seq": 1, "stage": "done", "pos": None}, pos=None)
    assert _PeepSelf(clicker)._gamepad_nav_progress_kwargs() is None


class _State:
    def __init__(self, **kw):
        self._kw = kw

    def to_kwargs(self):
        return dict(self._kw)


def _blank_frame():
    return np.zeros((180, 320, 3), dtype=np.uint8)


_DIAG_KW = dict(
    treasure_stage="中标结算",
    treasure_click_mode="gamepad",
    treasure_gamepad_cursor={"seq": 3, "stage": "idle", "pos": (120, 90),
                             "dist": None, "stale": True, "age_s": 7.5},
    treasure_cursor_cands={"list": [(120, 90, 0.88, "interactive")], "sel": 0,
                           "stale": True, "age_s": 7.5},
)


def test_peep_draws_gamepad_layers_without_click_intent():
    """`treasure_action` 为空（转场静默分支）时，手柄诊断层照常绘制。"""
    renderer = _load_renderer_class()(None)
    frame = _blank_frame()
    out = renderer.render_peep(frame, _State(treasure_action=None, **_DIAG_KW))
    assert out.sum() > 0, "无点击意图 → 叠加层整层消失（转场期预览只剩原图）"
    assert frame.sum() == 0, "渲染就地改写了输入帧"


def test_peep_draws_gamepad_layers_on_pure_wait():
    """纯等待意图（有 hint、无 center）同样保留手柄诊断层。"""
    renderer = _load_renderer_class()(None)
    out = renderer.render_peep(
        _blank_frame(),
        _State(treasure_action={"key": "bid_waiting", "hint": "等待出价按钮亮起..."},
               **_DIAG_KW))
    assert out.sum() > 0


def test_peep_without_any_content_is_still_blank():
    """对照组：既无意图也无手柄数据 → 仍是原图（不凭空画东西）。"""
    renderer = _load_renderer_class()(None)
    out = renderer.render_peep(_blank_frame(), _State(treasure_action=None,
                                                     treasure_click_mode="gamepad"))
    assert out.sum() == 0


def test_clicker_snapshot_feeds_renderer_stale_flag():
    """端到端：Clicker 超龄快照 → 渲染层确实拿到了 stale 标记（层间契约）。"""
    c = Clicker(hwnd=0, mode="gamepad")
    c._gamepad = types.SimpleNamespace(
        last_cands=((50, 60, 0.9, "interactive"),), last_cand_sel=0,
        last_cands_ts=time.monotonic() - 9.0,
        last_pos=(50, 60), last_pos_ts=time.monotonic() - 9.0)
    snap = c.cursor_candidates()
    renderer = _load_renderer_class()(None)
    out = renderer.render_peep(
        _blank_frame(),
        _State(treasure_action=None, treasure_click_mode="gamepad",
               treasure_cursor_cands=snap,
               treasure_gamepad_cursor={"stage": "idle", "pos": c.gamepad_cursor_pos(),
                                        "stale": True, "age_s": c.gamepad_cursor_age_s()}))
    assert snap["stale"] is True
    assert out.sum() > 0
