# -*- coding: utf-8 -*-
"""手柄光标快照读侧契约：超龄候选**不丢数据**，改为带新鲜度标记。

真机 2026-09-15（MaaRM_20260915_071523）：数字键链路空转 24s（07:17:34–07:17:58），
期间导航线程空闲 → `last_cands` 超 2s 即被判陈旧、`cursor_candidates()` 返回 None
→ PEEP 叠加层既无候选圈也无光标位，用户复盘时无法判断光标停在哪。

契约（用户拍板 2026-09-15「保留最后快照并标陈旧」）：
- 超龄仍返回候选（renderer 淡化 + 标注），只有「从未识别到候选 / 未绑定」才返回 None；
- 年龄由数据主人（Clicker）给出，宿主不再自己比时间。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from maaracing_master.core.clicker import Clicker


def _clicker_with_pad(**pad_kw):
    c = Clicker(hwnd=0, mode="gamepad")
    c._gamepad = SimpleNamespace(**pad_kw)
    return c


def test_stale_candidates_are_returned_with_flag():
    """超龄候选照样返回：list/sel 保真 + stale=True + age_s 可读。"""
    c = _clicker_with_pad(
        last_cands=((10, 20, 0.91, "interactive"), (30, 40, 0.55, "idle")),
        last_cand_sel=0,
        last_cands_ts=time.monotonic() - 5.0,
        last_pos=(10, 20),
        last_pos_ts=time.monotonic() - 5.0,
    )
    snap = c.cursor_candidates(max_age_s=2.0)
    assert snap is not None, "超龄候选被丢弃 → 空闲期叠加层无内容可看"
    assert snap["list"] == [(10, 20, 0.91, "interactive"), (30, 40, 0.55, "idle")]
    assert snap["sel"] == 0
    assert snap["stale"] is True
    assert snap["age_s"] >= 5.0


def test_fresh_candidates_are_not_marked_stale():
    """新鲜候选不得被误标陈旧（实时位与历史位必须可辨）。"""
    c = _clicker_with_pad(
        last_cands=((10, 20, 0.91, "interactive"),),
        last_cand_sel=0,
        last_cands_ts=time.monotonic(),
        last_pos=(10, 20),
        last_pos_ts=time.monotonic(),
    )
    snap = c.cursor_candidates()
    assert snap["stale"] is False and snap["age_s"] < 1.0


def test_never_detected_candidates_return_none():
    """从未识别到候选 → None（不编造内容）。"""
    c = _clicker_with_pad(last_cands=(), last_cand_sel=None, last_cands_ts=0.0)
    assert c.cursor_candidates() is None


def test_cursor_age_reports_staleness_and_inf_when_unknown():
    """光标位年龄：有识别历史给秒数；从未识别给 inf（渲染层据此标「--」）。"""
    fresh = _clicker_with_pad(last_pos=(1, 2), last_pos_ts=time.monotonic() - 3.0)
    assert 2.9 <= fresh.gamepad_cursor_age_s() <= 4.0
    never = _clicker_with_pad(last_pos=None, last_pos_ts=0.0)
    assert never.gamepad_cursor_age_s() == float("inf")


def test_cursor_age_without_bound_navigator_is_inf():
    assert Clicker(hwnd=0, mode="real").gamepad_cursor_age_s() == float("inf")
