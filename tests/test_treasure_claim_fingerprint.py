# -*- coding: utf-8 -*-
"""领取分红「跳过动画重试点击」与「真领取」的指纹判别契约（真机 2026-09-20 卡死回归）。

现象（MaaRM_20260920_204038，20:48:14–20:50:16）：跳动画首次点击后收入迟迟读不出，
settle_skip_retry 每 ~4s 清指纹重发点击；某次重试点击成功（指纹固化为
`(settle_collect_red_btn, auto, 中心, True)`）之后收入才被接受 → 真领取意图与该指纹
完全相同 → `_execute_click` 边沿触发静默拦截，137 个决策帧 0 次提交，卡死 107s
直到游戏侧页面自行变化。旧指纹只带 `clicked_once` 一比特，区分不了「重试点击」和
「真领取」两类动作。

契约：判别位取「收入是否已读出」——重试点击（skip）与真领取（claim）指纹必不同；
真领取恰好重新 arm 一次（成功后边沿触发照常拦截同意图）；物理提交失败不清指纹、
下帧重试；真领取成功后按 CLICK_RETRY_KEYS 惯例 arm 阶段切换重试链（P1-7 交接的
「下一次真领取点击自行 arm」由此真正可达）。
"""
from __future__ import annotations

import pytest


def _load_module():
    try:
        from maaracing_master.plugins.treasure import module as mod
    except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时跳过
        pytest.skip(f"需要完整运行时依赖（maa/…）：{exc}")
    return mod


class _StubClicker:
    need_foreground = False
    last_pos = (2265, 684)
    mode = "real"
    gamepad_bound = False

    def __init__(self, *, submit_ok=True):
        self.submit_ok = submit_ok
        self.submitted = []

    def set_mode(self, mode):
        pass

    def set_intent(self, intent):
        pass

    def is_busy(self):
        return False

    def submit_click(self, x, y, **kw):
        if self.submit_ok:
            self.submitted.append((x, y))
        return self.submit_ok


class _StubCtx:
    click_mode = "real"
    intent_mode = False
    window_foreground = False


def _mod(income=None, *, clicked_once=False, submit_ok=True):
    """只装 _execute_click / _apply_click_success 触碰的最小状态（真身方法）。"""
    mod = _load_module()
    m = mod.TreasureModule.__new__(mod.TreasureModule)
    m.ctx = _StubCtx()
    m._clicker = _StubClicker(submit_ok=submit_ok)
    m._gp_bind_tried = True  # real 模式不触碰手柄绑定，桩直接跳过
    m._current_stage = "领取分红"
    m._bidding_last_decision = None
    m._settle_my_income = income
    m._settle_collect_clicked_once = clicked_once
    m._settle_skip_since_ms = 0
    m._last_click_fingerprint = None
    m._last_click_time = 0.0
    m._click_cooldown_s = 0.0
    m._pending_click = None
    m._frame_counter = 0
    m._trace_writer = None
    m._click_retry_key = None
    m._click_retry_stage = None
    m._click_retry_since_ts = 0.0
    m._click_retry_count = 0
    m._click_retry_frames = 10**9  # 测试窗口内不触发阶段切换超时重试
    m._retry_frames_by_key = {}
    m._click_fail_key = None
    m._click_fail_streak = 0
    m._panel_retry_sig = None
    m._panel_retry_since_ts = 0.0
    m._panel_retry_count = 0
    m._popup_click_cooldown = 0
    m._popup_click_cooldown_frames = 5
    m._session_badge_clicked = False
    return m


def _intent():
    return {"key": "settle_collect_red_btn", "center": (0.794, 0.822)}


def test_real_claim_passes_fingerprint_left_by_retry_click():
    """回归主场景：重试点击成功后收入才读出 → 真领取必须能提交（旧码被静默拦截）。"""
    m = _mod()
    # ① 跳动画首点（income=None）→ 成功消费，指纹固化
    m._execute_click(_intent())
    assert m._pending_click is not None
    m._apply_click_success(m._pending_click)
    m._pending_click = None
    assert m._settle_collect_clicked_once is True
    fp_first = m._last_click_fingerprint
    # ② 跳动画重试点击（settle_skip_retry 效果等价：清指纹后同图重发）→ 成功消费
    m._last_click_fingerprint = None
    m._execute_click(_intent())
    m._apply_click_success(m._pending_click)
    m._pending_click = None
    fp_retry = m._last_click_fingerprint
    assert fp_retry == fp_first, "跳动画类点击（首点/重试）指纹应同类"
    # ③ 收入读出 → 真领取意图：不得被重试点击留下的指纹拦截
    m._settle_my_income = 86574
    m._execute_click(_intent())
    assert m._pending_click is not None, "真领取被指纹锁死 = 2026-09-20 卡死复现"
    fp_claim = m._pending_click["fp"]
    assert fp_claim != fp_retry, "真领取与跳动画点击指纹必须可分"
    # ④ 真领取成功消费后，同意图被边沿触发正常拦截（不连点）
    m._apply_click_success(m._pending_click)
    m._pending_click = None
    m._execute_click(_intent())
    assert m._pending_click is None, "真领取成功后同意图不得重复提交"


def test_real_claim_click_arms_stage_switch_retry_chain():
    """P1-7 交接的后半句：真领取成功自行 arm 阶段切换重试链（页面没切走有兜底）。"""
    m = _mod(income=86574, clicked_once=True)
    m._execute_click(_intent())
    m._apply_click_success(m._pending_click)
    assert m._click_retry_key == "settle_collect_red_btn"
    assert m._click_retry_stage == "领取分红"


def test_failed_submit_keeps_fingerprint_clean_for_retry():
    """提交失败（入队失败/导航失败）→ 指纹不更新，下帧同意图自动重试。"""
    m = _mod(income=86574, clicked_once=True, submit_ok=False)
    m._execute_click(_intent())
    assert m._pending_click is None
    m._clicker.submit_ok = True
    m._execute_click(_intent())
    assert m._pending_click is not None, "提交失败后下帧必须能重试"
