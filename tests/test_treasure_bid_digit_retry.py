# -*- coding: utf-8 -*-
"""面板内数字键「点了但输入框没反应」的兜底契约（真机 2026-09-15 24s 空转回归）。

现象（MaaRM_20260915_071523，07:17:34–07:17:58）：程序把光标导航到数字键「2」并按了 A，
输入框读数 24 秒始终为空 → `_bid_input_progress` 不推进 → 指纹（含该锚点）不变 →
边沿触发不再重发 → 光标本该在动却原地不动；同面板「智能出价」「✖ 清空」按下均生效。

口径（沿用「按钮点击重试规范」第②/③层）：成功信号 = 面板读回变化（已编码在指纹里，
故「指纹不变」就是「无响应」）→ 超时清指纹重发、封顶后终止模块（不得静默）。
"""
from __future__ import annotations

import time

import pytest


def _load_module():
    """取 treasure.module 模块对象（类 + 模块级异常都要用；缺运行时依赖则跳过）。"""
    try:
        from maaracing_master.plugins.treasure import module as mod
    except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时跳过
        pytest.skip(f"需要完整运行时依赖（maa/…）：{exc}")
    return mod


class _StubSelf:
    """只喂 `_maybe_retry_panel_no_response` 读到的字段；被测方法为真身。"""

    def __init__(self, *, count=0, elapsed_s=0.0, sig=(("bid_numpad_2", "S3_edit_type", 0.63, 0.54))):
        cls = _load_module().TreasureModule
        self._maybe_retry_panel_no_response = cls._maybe_retry_panel_no_response.__get__(self)
        self.BID_DIGIT_RETRY_MS = cls.BID_DIGIT_RETRY_MS
        self.BID_DIGIT_RETRY_MAX = cls.BID_DIGIT_RETRY_MAX
        self._panel_retry_sig = sig
        self._panel_retry_since_ts = time.monotonic() - elapsed_s
        self._panel_retry_count = count
        self._last_click_fingerprint = sig


_KEY = "bid_numpad_2"
_FP = ("bid_numpad_2", "S3_edit_type", 0.63, 0.54)


def test_new_fingerprint_resets_baseline_without_retry():
    """换了新意图（输入框推进/换数字）→ 计时归零、计数清零、不动指纹。"""
    fake = _StubSelf(sig=("旧指纹",), elapsed_s=99.0, count=2)
    fake._last_click_fingerprint = "已点过的旧指纹"
    fake._maybe_retry_panel_no_response(_KEY, _FP)
    assert fake._panel_retry_sig == _FP
    assert fake._panel_retry_count == 0
    assert fake._panel_retry_since_ts > time.monotonic() - 1
    assert fake._last_click_fingerprint == "已点过的旧指纹", "换意图不该重发"


def test_within_timeout_waits():
    """未到判定时限 → 不重发（一次性导航+按键+OCR 的耗时不能算成无响应）。"""
    fake = _StubSelf(count=0, elapsed_s=1.0, sig=_FP)
    fake._maybe_retry_panel_no_response(_KEY, _FP)
    assert fake._panel_retry_count == 0
    assert fake._last_click_fingerprint == _FP, "未超时就把指纹清了"


def test_timeout_rearms_fingerprint_and_counts():
    """超时无推进 → 清指纹重新 arm（下帧同意图可重发）+ 计数 +1。"""
    fake = _StubSelf(count=0, elapsed_s=99.0, sig=_FP)
    fake._maybe_retry_panel_no_response(_KEY, _FP)
    assert fake._panel_retry_count == 1
    assert fake._last_click_fingerprint is None, "未清指纹 → 边沿触发仍不会重发"
    assert fake._panel_retry_since_ts > time.monotonic() - 1, "重发后计时未重置"


def test_retry_is_capped_and_fails_loudly():
    """封顶后不得静默：抛 ClickRetryExhaustedError 终止模块（用户可见、可干预）。"""
    mod = _load_module()
    fake = _StubSelf(count=mod.TreasureModule.BID_DIGIT_RETRY_MAX, elapsed_s=99.0, sig=_FP)
    with pytest.raises(mod.ClickRetryExhaustedError) as ei:
        fake._maybe_retry_panel_no_response(_KEY, _FP)
    assert _KEY in str(ei.value)
    assert fake._last_click_fingerprint == _FP, "终止路径不该再动指纹"


def test_clear_key_is_covered_too():
    """✖ 清空键同属面板内数据类（成功信号 = 读数归零），同样纳入兜底。"""
    fake = _StubSelf(count=0, elapsed_s=99.0,
                     sig=("bid_numpad_clear", "S3_edit_clear", 0.33, 0.63))
    fake._maybe_retry_panel_no_response(
        "bid_numpad_clear", ("bid_numpad_clear", "S3_edit_clear", 0.33, 0.63))
    assert fake._panel_retry_count == 1
    assert fake._last_click_fingerprint is None


def test_other_keys_are_not_touched():
    """阶段切换类/其它 key 不归本兜底管（它们走 _maybe_retry_stage_click）。"""
    fake = _StubSelf(count=0, elapsed_s=99.0, sig=(("bid_main_red_btn",),))
    fake._last_click_fingerprint = ("bid_main_red_btn",)
    fake._maybe_retry_panel_no_response("bid_main_red_btn", ("bid_main_red_btn",))
    assert fake._panel_retry_count == 0
    assert fake._last_click_fingerprint == ("bid_main_red_btn",)


class _StubClicker:
    def __init__(self):
        self.mode = "gamepad"
        self.submits = 0

    @property
    def need_foreground(self):
        return False

    def set_mode(self, _mode):
        pass

    def set_intent(self, _intent):
        pass

    def is_busy(self):
        return False

    def submit_click(self, *_a, **_kw):
        self.submits += 1
        return True


class _ExecSelf:
    """`_execute_click` 真身 + 最小桩：验证「指纹命中 → 走无响应兜底」这条接线。"""

    def __init__(self):
        cls = _load_module().TreasureModule
        self._execute_click = cls._execute_click.__get__(self)
        self.BID_DIGIT_RETRY_MS = cls.BID_DIGIT_RETRY_MS
        self.BID_DIGIT_RETRY_MAX = cls.BID_DIGIT_RETRY_MAX
        self.CLICK_MODE_LABELS = cls.CLICK_MODE_LABELS
        self.CLICK_DOWN_UP_GAP_MS = cls.CLICK_DOWN_UP_GAP_MS
        self.CLICK_MOVE_PAUSE_S = cls.CLICK_MOVE_PAUSE_S
        self._maybe_retry_stage_click = lambda _key: None   # 本用例只管面板内兜底这条线
        self._maybe_retry_panel_no_response = cls._maybe_retry_panel_no_response.__get__(self)
        self._clicker_stub = _StubClicker()
        self._clicker = self._clicker_stub
        self.ctx = type("Ctx", (), {"click_mode": "gamepad", "intent_mode": False})()
        self._current_stage = "待机"
        self._bidding_last_decision = None
        self._bid_input_progress = 0
        self._settle_collect_clicked_once = False
        self._settle_my_income = None
        self._last_click_fingerprint = None
        self._last_click_time = 0.0
        self._click_cooldown_s = 0.0
        self._pending_click = None
        self._trace_writer = None
        self._frame_counter = 1
        self._panel_retry_sig = None
        self._panel_retry_since_ts = 0.0
        self._panel_retry_count = 0

    def _get_clicker(self):
        return self._clicker_stub

    def _ensure_gamepad_bound(self):
        pass


def test_execute_click_routes_matching_fingerprint_into_no_response_retry():
    """接线：同一数字指纹被指纹锁拦下时，必须进无响应兜底（否则就是 24s 空转复现）。"""
    fake = _ExecSelf()
    target = {"key": _KEY, "center": (0.63, 0.54), "state": "S3_edit_type"}
    fake._execute_click(target)                      # 首次提交
    assert fake._clicker_stub.submits == 1
    assert fake._panel_retry_sig is not None, "提交时未记录兜底计时基准"

    fake._last_click_fingerprint = fake._panel_retry_sig   # 模拟点击成功写入指纹
    fake._panel_retry_since_ts = time.monotonic() - 99.0   # 超时仍无读回推进
    fake._execute_click(target)
    assert fake._clicker_stub.submits == 1, "本帧应只重发一次"
    assert fake._last_click_fingerprint is None, "指纹未清 → 下帧仍不会重发"
    assert fake._panel_retry_count == 1
