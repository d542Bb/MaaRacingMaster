# -*- coding: utf-8 -*-
"""聚合奖励弹窗数蛋的「飞入动画窗内轮询择优」契约（真机 2026-09-20 失录回归）。

现象（MaaRM_20260920_221833，22:32:50）：彩蛋收尾链点开聚合奖励弹窗后**单帧识别**，
撞上蛋卡/medal 卡逐帧飞入动画（完整帧仅约 1 帧，2026-08-18 结算链同款时序）读出
「红0 黄0 蓝0 银币+0 积分+0」→ 照常点屏幕中心关弹窗 → 游戏侧实得红2/蓝/金/银币
全部失录（看板 领取彩蛋 0/0/0）。

契约（`_egg_recognize_stable`）：
- 窗内轮询：counts 按「命中蛋种数 → 蛋总数」择优绝不降级；银币/积分独立取窗内最大
  （count-up 同为单调）；
- 连续 STABLE_POLLS 拍持平且已过最短观察窗才提前收——纯零结果同样要过最短窗，
  防动画早期空帧假稳定；
- 识别器整个窗口不可用时 counts=None，金额仍返回窗内最大读数（照常入账）；
- 窗口受剩余预算 clamp（预算耗尽 → 不轮询，返回 (None, 0, 0)）。

测试用假时钟确定性驱动：monkeypatch module 命名空间的 `time`（仅本模块可见），
`lifecycle.sleep` 推进假钟，无真实等待。
"""
from __future__ import annotations

import pytest


def _load():
    try:
        from maaracing_master.plugins.treasure import module as mod
    except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时跳过
        pytest.skip(f"需要完整运行时依赖（maa/…）：{exc}")
    return mod


ZERO = {"red": 0, "yellow": 0, "blue": 0}
FULL = {"red": 2, "yellow": 0, "blue": 1}


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


class _StubLifecycle:
    def __init__(self, clock):
        self.clock = clock
        self.running = True
        self.sleeps = 0

    def sleep(self, _s):
        self.sleeps += 1
        self.clock.t += 0.25  # EGG_CHAIN_POLL_S


class _StubCtx:
    def __init__(self, capture, lifecycle):
        self.capture = capture
        self.lifecycle = lifecycle


class _StubRecognizer:
    """按「弹窗轮次」索引的识别序列（末项无限延续）。"""

    def __init__(self, seq, state):
        self.seq = seq
        self.state = state

    def recognize(self, _frame):
        idx = min(self.state["poll"] - 1, len(self.seq) - 1)
        return {"counts": dict(self.seq[idx])}


def _mod(monkeypatch, recog_seq, *, amounts=None, recognizer=True, budget=None):
    """装配只喂 _egg_recognize_stable 的最小状态；amounts 按 medal 名给金额序列。"""
    mod = _load()
    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", clock)  # 仅替换本模块命名空间的 time
    state = {"poll": 0}

    class _Capture:
        def screenshot(self):
            state["poll"] += 1
            return f"frame{state['poll']}"

    m = mod.TreasureModule.__new__(mod.TreasureModule)
    m.ctx = _StubCtx(_Capture(), _StubLifecycle(clock))
    m._egg_recognizer = _StubRecognizer(recog_seq, state) if recognizer else None

    amt = amounts or {"claim_coin_medal": [0], "claim_score_medal": [0]}

    def _read_amount(_specs, name, _frame):
        seq = amt[name]
        idx = min(state["poll"] - 1, len(seq) - 1)
        return seq[idx]

    m._egg_chain_read_amount = _read_amount
    return m, clock, state


def test_flyin_animation_yields_best_counts(monkeypatch):
    """飞入动画：前几拍空窗全 0、卡片落位后转正 → 取落位后的 counts 与金额。"""
    m, _clock, state = _mod(
        monkeypatch,
        [ZERO, ZERO, ZERO, FULL],
        amounts={"claim_coin_medal": [0, 0, 0, 25000],
                 "claim_score_medal": [0, 0, 0, 86574]},
    )
    counts, coin, score = m._egg_recognize_stable({}, budget_deadline=None)
    assert counts == FULL
    assert (coin, score) == (25000, 86574)
    assert state["poll"] >= 4, "动画未播完不得提前收"


def test_zero_egg_day_waits_out_min_window(monkeypatch):
    """纯零结果同样要过最短观察窗才收（防动画早期空帧假稳定）。"""
    m, clock, _state = _mod(monkeypatch, [ZERO],
                            amounts={"claim_coin_medal": [0],
                                     "claim_score_medal": [0]})
    counts, coin, score = m._egg_recognize_stable({}, budget_deadline=None)
    assert counts == ZERO and (coin, score) == (0, 0)
    assert clock.t >= 2.0, "最短观察窗未到就收 = 空帧假稳定复现"
    assert clock.t < 6.0, "稳定即收：纯零日不应耗满整个识别窗"


def test_mid_animation_glitch_never_downgrades(monkeypatch):
    """中途一拍识别回落（闪烁/遮挡）→ 历史最优不被降级。"""
    m, _clock, _state = _mod(
        monkeypatch,
        [ZERO, FULL, ZERO, FULL],
        amounts={"claim_coin_medal": [0, 25000, 0, 25000],
                 "claim_score_medal": [0]},
    )
    counts, coin, score = m._egg_recognize_stable({}, budget_deadline=None)
    assert counts == FULL
    assert (coin, score) == (25000, 0)


def test_recognizer_missing_keeps_amounts(monkeypatch):
    """识别器整个窗口不可用 → counts=None，金额照常返回（照常入账路径）。"""
    m, _clock, _state = _mod(monkeypatch, [ZERO],
                             amounts={"claim_coin_medal": [25000],
                                      "claim_score_medal": [0]},
                             recognizer=False)
    counts, coin, score = m._egg_recognize_stable({}, budget_deadline=None)
    assert counts is None
    assert (coin, score) == (25000, 0)


def test_exhausted_budget_polls_nothing(monkeypatch):
    """剩余预算已耗尽 → 窗口 clamp 为 0，一次都不轮询。"""
    m, clock, state = _mod(monkeypatch, [FULL], amounts=[25000])
    counts, coin, score = m._egg_recognize_stable({}, budget_deadline=clock.t)
    assert (counts, coin, score) == (None, 0, 0)
    assert state["poll"] == 0


def test_dismiss_point_clears_reward_card_band():
    """关闭弹窗点位必须在卡片带下方（medal/蛋卡搜索区 y 0.28~0.60、详情卡下缘 ~0.72）。

    旧点位 (0.50, 0.55) 落在卡片带正中，点开的是卡片详情而不是推进页面
    （真机 2026-09-20 红色彩蛋详情卡被误点开）。
    """
    mod = _load()
    x, y = mod.TreasureModule.EGG_CHAIN_DISMISS_NORM
    assert y >= 0.75, "点位落回卡片带 = 误点卡片详情复现"
    assert 0.0 < x < 1.0
