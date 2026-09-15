# -*- coding: utf-8 -*-
"""场次选择「先选场次，再开始匹配」闸门的回归锁。

真机症状（01:08:26 倒序事故）：「开始匹配」按钮模板在进大厅当帧即命中（详情卡默认
打开、任意场次都带该按钮），旧逻辑据此直接点开始匹配——目标场次根本没点过；
点完按钮消失、阶段未切的窗口内又回退点场次标签，序整个倒过来。

修复口径（本锁所验）：单次进入场次选择阶段内，点中目标场次 badge 成功
（_apply_click_success 置位 _session_badge_clicked）之前只给 badge 意图；
置位之后只给「开始匹配」/纯等待，badge 不再出现。闸门在阶段切走/新一场清零。
"""
from __future__ import annotations

import pytest

STAGE = "鉴宝大厅(选择场次)"
# 命中模板的返回形态：[(priority, key, score, cxn, cyn)]
HIT = [(0, "session_start_match_btn", 0.95, 0.85, 0.80)]


def _load_module_class():
    try:
        from maaracing_master.plugins.treasure.module import TreasureModule
    except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时跳过
        pytest.skip(f"需要完整运行时依赖（maa/…）：{exc}")
    return TreasureModule


class _SessionSelf:
    """只喂 _run_session_choice / _apply_click_success 读到的字段，被测方法为真身。

    _match_session_panel 以 hits 列表直接控制（不跑模板匹配）。
    """

    def __init__(self, *, stage=STAGE, hits=(), gate=False, panel_configured=True):
        mod = _load_module_class()
        self._run_session_choice = mod._run_session_choice.__get__(self)
        self._apply_click_success = mod._apply_click_success.__get__(self)
        self._current_stage = stage
        self._session_badge_clicked = gate
        self._target_session = "master"
        self._session_panel = [("tpl",)] if panel_configured else []
        self._hits = list(hits)
        self._action_centers = {
            "session_start_match_btn": (0.85, 0.80),
            "session_master_badge": (0.60, 0.45),
            "session_expert_badge": (0.40, 0.35),
            "session_intern_badge": (0.20, 0.30),
        }
        self._session_last_decision: dict | None = None
        # _apply_click_success 副作用所需最小状态（badge key 不进重试/冷却分支）
        self._record_click = lambda *a, **k: None
        self._last_click_fingerprint = None
        self._last_click_time = 0.0
        self._popup_click_cooldown = 0
        self._popup_click_cooldown_frames = 5
        self._click_retry_key = None
        self._click_retry_stage = None
        self._click_retry_since_ts = 0.0
        self._click_retry_count = 0
        self._settle_my_income = None
        self._settle_collect_clicked_once = False
        self.POPUP_HIGH_CONTINUE_KEY = mod.POPUP_HIGH_CONTINUE_KEY
        self.POPUP_REWARD_CONTINUE_KEY = mod.POPUP_REWARD_CONTINUE_KEY
        self.CLICK_RETRY_KEYS = mod.CLICK_RETRY_KEYS

    def _match_session_panel(self, frame_rgb):  # noqa: ARG002
        return list(self._hits)

    def _daily_loop_limit_reached(self):
        return False


class TestSessionBadgeGate:
    def test_badge_before_start_match_even_if_btn_visible(self):
        """闸门未过：即使「开始匹配」按钮已命中，也只给场次标签意图。"""
        s = _SessionSelf(hits=HIT)
        s._run_session_choice(None)
        assert s._session_last_decision["key"] == "session_master_badge"
        assert s._session_badge_clicked is False

    def test_click_success_opens_gate(self):
        """点中目标场次 badge（场次选择阶段内）→ 闸门开启；其它阶段同名 key 不开闸。"""
        s = _SessionSelf()
        s._apply_click_success({"key": "session_master_badge", "fp": ("f",),
                                "center": (0.60, 0.45)})
        assert s._session_badge_clicked is True
        s2 = _SessionSelf(stage="游戏大厅")
        s2._apply_click_success({"key": "session_master_badge", "fp": ("f",),
                                 "center": (0.60, 0.45)})
        assert s2._session_badge_clicked is False

    def test_gate_open_btn_hit_emits_start_match(self):
        """闸门已过 + 按钮命中 → 点「开始匹配」（actions 段静态中心）。"""
        s = _SessionSelf(hits=HIT, gate=True)
        s._run_session_choice(None)
        dec = s._session_last_decision
        assert dec["key"] == "session_start_match_btn"
        assert dec["center"] == (0.85, 0.80)

    def test_no_badge_fallback_while_transitioning(self):
        """闸门已过 + 按钮未命中（点完匹配转场中）→ 纯等待，10 帧内不出现 badge 意图。"""
        s = _SessionSelf(hits=HIT, gate=True)
        s._hits = []
        for _ in range(10):
            s._run_session_choice(None)
            dec = s._session_last_decision
            assert dec["key"] == "session_waiting"
            assert dec.get("center") is None

    def test_stage_leave_resets_gate(self):
        """离开场次选择阶段 → 闸门复位，下次进大厅重新选场次。"""
        s = _SessionSelf(stage="匹配中", gate=True)
        s._run_session_choice(None)
        assert s._session_badge_clicked is False
        s._current_stage = STAGE
        s._run_session_choice(None)
        assert s._session_last_decision["key"] == "session_master_badge"

    def test_degrade_mode_gate_open_clicks_btn_pos(self):
        """降级模式（模板未配置）：闸门已过 → 直接点开始匹配位置（无命中信号可等）。"""
        s = _SessionSelf(panel_configured=False, gate=True)
        s._run_session_choice(None)
        assert s._session_last_decision["key"] == "session_start_match_btn"
