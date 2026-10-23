# -*- coding: utf-8 -*-
"""模型预加载回归锁：只构造一次、软失败可重试、线程只起一次。"""

from __future__ import annotations

import threading
import time

import maaracing_master.plugins.speedrush.module as smod


def _module():
    return smod.SpeedRushModule(None)


def test_ensure_perception_single_construction_under_threads(monkeypatch):
    mod = _module()
    calls = {"n": 0}

    class FakePerc:
        def __init__(self, path):
            calls["n"] += 1
            time.sleep(0.05)

    monkeypatch.setattr(smod, "StreetPerception", FakePerc)
    ths = [threading.Thread(target=mod._ensure_perception) for _ in range(4)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert calls["n"] == 1
    assert mod._perception is not None


def test_soft_failure_does_not_poison_lazy_retry(monkeypatch):
    mod = _module()
    calls = {"n": 0}

    class Flaky:
        def __init__(self, path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cold start")

    monkeypatch.setattr(smod, "StreetPerception", Flaky)
    assert mod._ensure_perception(soft=True) is None
    assert mod._perception_failed is False
    assert mod._ensure_perception() is not None
    assert calls["n"] == 2


def test_start_preload_only_once(monkeypatch):
    mod = _module()
    mod._control_mode = True
    calls = {"d": 0, "p": 0}
    monkeypatch.setattr(smod.SpeedRushModule, "PRELOAD_DELAY_S", 0.0)
    monkeypatch.setattr(smod.SpeedRushModule, "PRELOAD_GAP_S", 0.0)
    monkeypatch.setattr(mod, "_preload_wanted", lambda: True)
    monkeypatch.setattr(mod, "_ensure_depth_session",
                        lambda soft=False: calls.__setitem__(
                            "d", calls["d"] + 1))
    monkeypatch.setattr(mod, "_ensure_perception",
                        lambda soft=False: calls.__setitem__(
                            "p", calls["p"] + 1))
    mod._start_preload()
    mod._start_preload()
    assert mod._preload_done.wait(2.0)
    assert calls == {"d": 1, "p": 1}
    assert mod._preload_thread is None


def test_straight_mode_never_preloads():
    mod = _module()
    mod._straight_mode = True
    mod._control_mode = True
    assert mod._preload_wanted() is False


def test_record_perception_skips_depth_model(monkeypatch):
    mod = _module()
    mod._control_mode = False
    mod._perception_mode = True
    calls = {"d": 0, "p": 0}
    monkeypatch.setattr(smod.SpeedRushModule, "PRELOAD_DELAY_S", 0.0)
    monkeypatch.setattr(smod.SpeedRushModule, "PRELOAD_GAP_S", 0.0)
    monkeypatch.setattr(mod, "_ensure_depth_session",
                        lambda soft=False: calls.__setitem__(
                            "d", calls["d"] + 1))
    monkeypatch.setattr(mod, "_ensure_perception",
                        lambda soft=False: calls.__setitem__(
                            "p", calls["p"] + 1))
    mod._start_preload()
    assert mod._preload_done.wait(2.0)
    assert calls == {"d": 0, "p": 1}
