# -*- coding: utf-8 -*-
"""出价相位机：假下降沿时间口径 + 「已出价」自愈回归（v4 桥接节奏修复锁定）。

行为契约（2026-09-09 20:13 实机 log 定罪）：
  1. 假下降沿缓冲以**时间**（SUBMIT_ANIMATION_BUFFER_MS）判定，与帧率解耦——
     v4 节奏下帧数口径被稀释，会在对手未齐报价时误退 wait_first。
  2. wait_first 中主按钮 OCR 读到「已出价」= 提交成功铁证 → 自愈回
     wait_result（恢复 4 槽 OCR 投递，公开报价窗口不再错过）。
  3. 「已出价:金额」不得触发 S2_bid（点灰按钮无效）。
"""
from __future__ import annotations

import time

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
    """最小状态桩：只喂 _run_bidding_choice 读到的字段，方法按需打桩。"""

    SWITCH_CONFIRM_FRAMES = getattr(TreasureModule, "SWITCH_CONFIRM_FRAMES", None)
    PANEL_OPEN_MIN_STABLE_FRAMES = getattr(
        TreasureModule, "PANEL_OPEN_MIN_STABLE_FRAMES", None
    )
    SUBMIT_ANIMATION_BUFFER_MS = getattr(
        TreasureModule, "SUBMIT_ANIMATION_BUFFER_MS", None
    )
    _BID_MAIN_BTN_KEY = getattr(TreasureModule, "_BID_MAIN_BTN_KEY", None)

    def __init__(self, *, phase, smart=None, label="", epoch=1):
        self._current_stage = "第1回合出价"
        self._round_no = 1
        self._round_elapsed = 99
        self._match = smart
        self._label = label
        self._panel_stable_frames = 0
        self._panel_open = False
        self._bid_epoch = epoch
        self._bid_phase = phase
        self._bid_epoch_seen = epoch
        self._current_h = 242_100
        self._bid_input_latest = 242_100
        self._bid_input_progress = 0
        self._bid_confirm_streak = 0
        self._wait_result_frames = 0
        self._wait_result_entered_ts = 0.0
        self._my_rank = 1
        self._bid_player_submitted = {1: False, 2: False, 3: False, 4: False}
        self._bid_slots = {
            pid: {"val": -1, "stable": 0, "locked": False, "miss": 0,
                  "consumed": 5, "output": 3, "hits": 0}
            for pid in (1, 2, 3, 4)
        }
        self._action_centers = {"bid_main_red_btn": (0.477, 0.829)}
        self._bidding_last_decision = None
        self.executed = 0

    def _match_bid_smart_btn(self, frame_rgb):
        return self._match

    def _read_bid_main_btn_label(self, frame_rgb):
        return self._label

    def _run_bidding_execute(self, frame_rgb, s_score):
        self.executed += 1


def _choice(fake):
    TreasureModule._run_bidding_choice(fake, _FRAME)
    return fake


def test_fake_fallback_requires_wall_clock_buffer():
    """wait_result 缓冲未满（时间口径）不回退；超 1.5s 才判假下降沿。"""
    fake = _FakeSelf(phase="wait_result")
    fake._wait_result_entered_ts = time.time() - 0.2
    _choice(fake)
    assert fake._bid_phase == "wait_result", "缓冲期内不得回退（旧帧数口径在 v4 节奏下秒级稀释）"
    assert fake._bidding_last_decision["state"] == "S4_wait_result"

    fake._wait_result_entered_ts = time.time() - 2.0
    _choice(fake)
    assert fake._bid_phase == "wait_first"
    assert fake._bidding_last_decision["state"] == "S4_fake_fallback"


def test_submitted_label_recovers_wait_result():
    """wait_first 中按钮读到「已出价」→ 自愈回 wait_result 并重置计时。"""
    fake = _FakeSelf(phase="wait_first", label="已出价:242,100")
    _choice(fake)
    assert fake._bid_phase == "wait_result"
    assert fake._bidding_last_decision["state"] == "S4_wait_result"
    assert time.time() - fake._wait_result_entered_ts < 1.0, "自愈须重置缓冲计时"


def test_submitted_label_never_triggers_s2():
    """「已出价」不触发 S2_bid：wait_next 下走保守等待，R1 首轮（epoch=0）亮「出价」正常 S2。"""
    fake = _FakeSelf(phase="wait_next", label="已出价:242,100")
    _choice(fake)
    assert fake._bidding_last_decision["state"] == "S1_waiting"
    assert fake._bidding_last_decision["center"] is None

    fake = _FakeSelf(phase="wait_first", epoch=0, label="出价")
    _choice(fake)
    assert fake._bidding_last_decision["state"] == "S2_bid"
