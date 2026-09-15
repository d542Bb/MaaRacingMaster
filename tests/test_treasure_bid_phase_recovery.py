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

    def _match_bid_pass_confirm(self, frame_rgb):
        return None

    def _read_bid_main_btn_label(self, frame_rgb):
        return self._label

    def _run_bidding_execute(self, frame_rgb, s_score):
        self.executed += 1


def _choice(fake):
    TreasureModule._run_bidding_choice(fake, _FRAME)
    return fake


def test_fake_fallback_requires_elapsed_buffer():
    """wait_result 缓冲未满（monotonic 经过时长口径）不回退；超 1.5s 才判假下降沿。"""
    fake = _FakeSelf(phase="wait_result")
    fake._wait_result_entered_ts = time.monotonic() - 0.2
    _choice(fake)
    assert fake._bid_phase == "wait_result", "缓冲期内不得回退（旧帧数口径在 v4 节奏下秒级稀释）"
    assert fake._bidding_last_decision["state"] == "S4_wait_result"

    fake._wait_result_entered_ts = time.monotonic() - 2.0
    _choice(fake)
    assert fake._bid_phase == "wait_first"
    assert fake._bidding_last_decision["state"] == "S4_fake_fallback"


def test_submitted_label_recovers_wait_result():
    """wait_first 中按钮读到「已出价」→ 自愈回 wait_result 并重置计时。"""
    fake = _FakeSelf(phase="wait_first", label="已出价:242,100")
    _choice(fake)
    assert fake._bid_phase == "wait_result"
    assert fake._bidding_last_decision["state"] == "S4_wait_result"
    assert time.monotonic() - fake._wait_result_entered_ts < 1.0, "自愈须重置缓冲计时"


def test_submitted_label_never_triggers_s2():
    """「已出价」不触发 S2_bid：wait_next 下走保守等待，R1 首轮（epoch=0）亮「出价」正常 S2。"""
    fake = _FakeSelf(phase="wait_next", label="已出价:242,100")
    _choice(fake)
    assert fake._bidding_last_decision["state"] == "S1_waiting"
    assert fake._bidding_last_decision["center"] is None

    fake = _FakeSelf(phase="wait_first", epoch=0, label="出价")
    _choice(fake)
    assert fake._bidding_last_decision["state"] == "S2_bid"


# --------------------------------------------------------------------
#  输入子状态机：OCR 瞬空读锚点推进（2026-09-11 实机振荡回归）
# --------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from maaracing_master.plugins.treasure.strategy import (  # noqa: E402
    BALANCE_UNKNOWN, DECISION_TARGET_SECOND, BidContext, BidDecision)


class _StubStrategy:
    def decide(self, ctx):
        return BidDecision(
            price=81401, decision=DECISION_TARGET_SECOND, vhat=120960.0,
            max_win_bid=None, opponent_max=89500, trigger_bid=None,
            reason="R5 target_second(出到买入线): 81401")


class _ExecSelf:
    """_run_bidding_execute 最小状态桩：R5 target_second 目标 81401（实机场景）。"""

    BID_CONFIRM_STABLE_FRAMES = getattr(TreasureModule, "BID_CONFIRM_STABLE_FRAMES", 3)
    BID_ZERO_STABLE_MS = getattr(TreasureModule, "BID_ZERO_STABLE_MS", 1500.0)

    def __init__(self, *, progress=0, latest=8):
        self._bid_input_progress = progress
        self._bid_input_latest = latest
        self._bid_confirm_streak = 0
        self._bid_zero_since_ts = None
        self._bidding_last_decision = None
        self._action_centers = {
            "bid_numpad_8": (0.633, 0.718), "bid_numpad_1": (0.582, 0.538),
            "bid_numpad_4": (0.583, 0.628), "bid_numpad_0": (0.583, 0.805),
            "bid_numpad_clear": (0.329, 0.626), "bid_confirm_red_btn": (0.463, 0.805),
        }
        self._strategy = _StubStrategy()

    def _build_bid_context(self):
        return BidContext(round_no=5, h_seen=(89600,), last_round=None,
                          balance=BALANCE_UNKNOWN)


def test_blink_zero_read_advances_by_anchor_not_restart():
    """OCR 瞬空读（B=0、锚点=1）→ 按锚点继续指第 2 位，不回头重输第 1 位。

    实机振荡（2026-09-11 R5）：输 8 后 OCR 瞬时读输入框为空 → 旧代码直接
    重输首位 8（指纹带 progress 挡不住）→ B=88 → ✖清空 → 重输，一回合
    19 秒在清空/重输间循环，光标在面板来回跳（用户视角=「不点出价」）。
    """
    fake = _ExecSelf(progress=1, latest=0)
    TreasureModule._run_bidding_execute(fake, _FRAME, 1.0)
    d = fake._bidding_last_decision
    assert d["key"] == "bid_numpad_1", f"应按锚点推进到第 2 位，实际 {d['key']}"
    assert fake._bid_input_progress == 1, "瞬空读不得回退/重置锚点"


def test_stable_zero_read_resets_anchor_and_restarts():
    """B==0 持续超 BID_ZERO_STABLE_MS（真清空）→ 锚点归零，从首位重输。"""
    fake = _ExecSelf(progress=1, latest=0)
    fake._bid_zero_since_ts = time.monotonic() - 2.0
    TreasureModule._run_bidding_execute(fake, _FRAME, 1.0)
    d = fake._bidding_last_decision
    assert d["key"] == "bid_numpad_8"
    assert fake._bid_input_progress == 0
    assert fake._bid_zero_since_ts is None


def test_nonzero_read_clears_zero_timer():
    """读到非零值 → 空读计时清零（下一轮瞬空读重新获得完整防抖窗口）。"""
    fake = _ExecSelf(progress=1, latest=8)   # B=8 前缀匹配 → 输第 2 位
    fake._bid_zero_since_ts = time.monotonic() - 2.0
    TreasureModule._run_bidding_execute(fake, _FRAME, 1.0)
    assert fake._bid_zero_since_ts is None
    assert fake._bidding_last_decision["key"] == "bid_numpad_1"


# --------------------------------------------------------------------
#  出价主按钮：OCR 读数光标遮挡剔除 + S2 精确判定（2026-09-11 实机）
# --------------------------------------------------------------------

_LABEL_ROI = (0.4313876651982379, 0.805286343612335,
              0.523953744493392, 0.8575624082232012)


def test_cursor_over_label_roi_detected():
    """光标盘压在按钮文字 ROI 内 → 判遮挡；移开/无光标（real 模式）→ 不判。"""
    fake = _FakeSelf(phase="wait_next", label="出价")
    # 盘中心 (610,596)：ROI 像素 x[551,670] y[579,617] 之内
    fake._clicker = SimpleNamespace(gamepad_cursor_pos=lambda: (610, 596))
    assert TreasureModule._cursor_hits_rect(fake, _LABEL_ROI, _FRAME) is True
    # 移远 → 不相交
    fake._clicker = SimpleNamespace(gamepad_cursor_pos=lambda: (100, 100))
    assert TreasureModule._cursor_hits_rect(fake, _LABEL_ROI, _FRAME) is False
    # 无光标（real 模式/未绑定/从未识别）→ 视为无光标，与模板侧 mask_cursor 同语义
    fake._clicker = None
    assert TreasureModule._cursor_hits_rect(fake, _LABEL_ROI, _FRAME) is False


def test_s2_requires_exact_cn_label():
    """S2 只认剥掉非中文噪声后恰为「出价」的读数。

    实机误判（2026-09-11）：「等得出价」（等待出价的 OCR 误读）含"出价"不含
    "等待"，旧子串判定连点灰按钮并触发 3 轮无效重试；光标盘数字混入的
    「出价.39,5」剥离后仍须正常触发。
    """
    fake = _FakeSelf(phase="wait_next", label="等得出价")
    _choice(fake)
    assert fake._bidding_last_decision["state"] == "S1_waiting", \
        "「等得出价」是等待态误读，不得触发 S2 点灰按钮"

    fake = _FakeSelf(phase="wait_next", label="出价.39,5")
    _choice(fake)
    assert fake._bidding_last_decision["state"] == "S2_bid", \
        "光标盘数字噪声剥离后应正常识别已亮"


# --------------------------------------------------------------------
#  光标驻留看守 auto_shoo（core 层避让判定语义，2026-09-11 重新接线）
# --------------------------------------------------------------------

_SHOO_ROI = ("bid_main_btn_label", (0.4, 0.75, 0.55, 0.9))


class _NavStub:
    def __init__(self, pos, miss=0):
        self.last_pos = pos
        self.miss_streak = miss


def _shoo_clicker(pos, *, busy=False, miss=0):
    from maaracing_master.core.clicker import Clicker
    c = Clicker(hwnd=123, mode="gamepad")
    c._gamepad = _NavStub(pos, miss)
    c.is_busy = lambda: busy
    moves: list[tuple[float, float]] = []
    c.submit_move = lambda cx, cy, **kw: (moves.append((cx, cy)), True)[1]
    return c, moves


def test_auto_shoo_moves_cursor_off_guard_roi():
    """光标压 guard ROI 且下一意图中心也在脏区 → 避让导航（不点击）并返回命中 key。"""
    c, moves = _shoo_clicker((610, 596))   # (0.477,0.828) 在 ROI 内
    r = c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720),
                    next_center=(0.477, 0.829))
    assert r is not None and r["key"] == "bid_main_btn_label"
    assert len(moves) == 1, "避让须走 submit_move（只导航不点击）"
    x, y = r["point"]
    rx, ry = 30.0 / 1280, 30.0 / 720
    assert not (0.4 - rx <= x <= 0.55 + rx and 0.75 - ry <= y <= 0.9 + ry), \
        "避让点必须移出识别区（含遮挡半径余量）"


def test_auto_shoo_skips_when_next_intent_clean():
    """下一点击意图中心在干净区 → 点击导航自然带离，不专门避让。"""
    c, moves = _shoo_clicker((610, 596))
    r = c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720),
                    next_center=(0.9, 0.5))
    assert r is None and not moves


def test_auto_shoo_gates_busy_miss_streak_cooldown():
    """任务槽忙（点击优先）/冷却窗内 → 跳过；光标连续未识别 → 1s 节流探测
    （不再永久跳过——失踪计数清零依赖导航执行，永久封死即自锁死局）。"""
    c, moves = _shoo_clicker((610, 596), busy=True)
    assert c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720)) is None
    assert not moves

    c, moves = _shoo_clicker((610, 596), miss=3)
    # 失踪计数高不再永久封死（自锁死局修复）：首帧放行探测，节流窗内再跳过
    assert c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720)) is not None
    assert len(moves) == 1
    assert c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720)) is None, \
        "探测节流窗（1s）内不得重复提交"
    assert len(moves) == 1

    c, moves = _shoo_clicker((610, 596))
    assert c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720)) is not None
    assert c.auto_shoo([_SHOO_ROI], radius_px=30.0, frame_size=(1280, 720)) is None, \
        "冷却窗内不得连续避让"
    assert len(moves) == 1
