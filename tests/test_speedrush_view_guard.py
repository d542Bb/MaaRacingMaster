# -*- coding: utf-8 -*-
"""开局视角守卫回归锁：正常带确认、坏视角切换、无几何切换、三视角失败、截止。"""

from __future__ import annotations

from maaracing_master.plugins.speedrush.view_guard import (
    ViewGuard, ViewGuardConfig)

CFG = ViewGuardConfig(enabled=True, sample_s=0.7, settle_s=0.25,
                      max_views=3, min_samples=2, deadline_s=2.9)


def test_correct_view_confirms_after_window():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, 1.33, True) == ViewGuard.WAIT
    assert g.update(0.50, 1.35, True) == ViewGuard.WAIT
    assert g.update(0.85, None, False) == ViewGuard.OK
    assert g.median == 1.34
    assert g.views == 1


def test_wrong_view_switches_then_confirms():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, 1.90, True) == ViewGuard.WAIT
    assert g.update(0.85, 1.92, True) == ViewGuard.SWITCH
    assert g.views == 2
    assert g.update(0.95, None, False) == ViewGuard.WAIT      # settle
    assert g.update(1.15, 1.35, True) == ViewGuard.WAIT       # 新窗口开始
    assert g.update(1.60, 1.37, True) == ViewGuard.WAIT
    assert g.update(1.90, None, False) == ViewGuard.OK


def test_no_geometry_is_bad_view():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, None, True) == ViewGuard.WAIT
    assert g.update(0.50, None, True) == ViewGuard.WAIT
    assert g.update(0.85, None, True) == ViewGuard.WAIT   # 宽限一次
    assert g.update(1.25, None, True) == ViewGuard.SWITCH
    assert g.median is None


def test_single_sample_is_not_enough():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, 1.35, True) == ViewGuard.WAIT
    assert g.update(0.85, None, False) == ViewGuard.WAIT  # 样本不足，宽限
    assert g.update(1.25, None, False) == ViewGuard.SWITCH


def test_three_bad_views_fail():
    g = ViewGuard(ViewGuardConfig(enabled=True, sample_s=0.7, settle_s=0.25,
                                  max_views=3, min_samples=2,
                                  deadline_s=10.0), t0=0.0)
    t = 0.10
    for expected_views in (2, 3):
        assert g.update(t, 1.90, True) == ViewGuard.WAIT
        assert g.update(t + 0.75, 1.90, True) == ViewGuard.SWITCH
        assert g.views == expected_views
        t += 0.80 + 0.25
    assert g.update(t, 1.90, True) == ViewGuard.WAIT
    assert g.update(t + 0.75, 1.90, True) == ViewGuard.FAIL


def test_deadline_fails_without_samples():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(2.95, None, False) == ViewGuard.FAIL


def test_three_samples_required_by_production_config():
    from maaracing_master.plugins.speedrush.config import load_decision
    cfg = load_decision().view_guard
    assert cfg.enabled is True
    assert cfg.min_samples == 3
    g = ViewGuard(cfg, t0=0.0)
    assert g.update(0.10, 1.33, True) == ViewGuard.WAIT
    assert g.update(0.50, 1.35, True) == ViewGuard.WAIT
    assert g.update(0.80, None, False) == ViewGuard.WAIT   # 2 个样本：宽限
    assert g.update(0.90, 1.34, True) == ViewGuard.OK      # 第 3 个样本到位


def test_press_view_button_always_releases(monkeypatch):
    import maaracing_master.core.vgamepad_lazy as vgl
    import maaracing_master.plugins.speedrush.module as smod

    class _Btn:
        XUSB_GAMEPAD_RIGHT_SHOULDER = "RB"

    class _Vg:
        XUSB_BUTTON = _Btn

    monkeypatch.setattr(vgl, "vg", _Vg())

    class Pad:
        def __init__(self):
            self.calls = []

        def press_button(self, b):
            self.calls.append(("press", b))

        def release_button(self, b):
            self.calls.append(("release", b))

        def update(self):
            self.calls.append(("update",))

    pad = Pad()
    smod.SpeedRushModule(None)._press_view_button(pad)
    kinds = [c[0] for c in pad.calls]
    assert kinds == ["press", "update", "release", "update"]
    assert pad.calls[0][1] == "RB"
