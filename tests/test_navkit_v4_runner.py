# -*- coding: utf-8 -*-
"""P2a-Q4：WgcapController（帧注入/新鲜度守卫）与 NavKitV4 装配契约。

注意：maa binding 的 C 句柄对象（CustomController/Tasker/Resource）不能在
pytest 进程内反复构造/回收（GC 顺序触发 C 层悬空回调 → 0xC0000005 崩溃），
本文件全部经 monkeypatch 把 maa 工厂替换为纯 Python 桩，只测 Python 逻辑。
"""
from __future__ import annotations

import json
import math
from unittest.mock import MagicMock

import numpy as np
import pytest

from maaracing_assistant.core import nav_graph as ng
from maaracing_assistant.core.nav_graph import STALE_FRAME_MS, FrameStaleError


@pytest.fixture()
def pure_maa(monkeypatch):
    """把 nav_graph 命名空间里的 maa 工厂替换为无 C 层的桩。"""
    class _NoInit:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ng, "CustomController", _NoInit)
    monkeypatch.setattr(ng, "CustomAction", _NoInit)
    monkeypatch.setattr(ng, "CustomRecognition", _NoInit)
    monkeypatch.setattr(ng, "PipelineLogger", _NoInit)
    monkeypatch.setattr(ng, "Resource", lambda: MagicMock())  # 需 register_* 方法
    monkeypatch.setattr(ng, "Tasker", lambda: MagicMock())


class _FakeCapture:
    """CaptureAdapter 形态的桩：可控帧/年龄。"""

    def __init__(self):
        self.frame: np.ndarray | None = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.fid = 7
        self.age = 0.0

    def frame_with_age(self):
        return (self.frame, self.fid, 0, self.age)


def test_screencap_injects_rgb_as_bgr(pure_maa):
    cap = _FakeCapture()
    cap.frame[:, :] = (10, 20, 30)  # RGB 通道序
    ctrl = ng.WgcapController(cap)
    out = ctrl.screencap()
    assert out.shape == (720, 1280, 3)
    assert out[0, 0, 0] == 30 and out[0, 0, 2] == 10  # BGR 反转
    assert ctrl.last_frame_id == 7


def test_stale_frame_raises(pure_maa):
    cap = _FakeCapture()
    ctrl = ng.WgcapController(cap, stale_ms=100.0)
    cap.age = 100.1
    with pytest.raises(FrameStaleError):
        ctrl.screencap()
    cap.age = 99.9
    assert ctrl.screencap() is not None  # 阈值内正常


def test_missing_frame_raises(pure_maa):
    cap = _FakeCapture()
    cap.frame = None
    cap.age = math.inf
    with pytest.raises(FrameStaleError):
        ng.WgcapController(cap).screencap()


def test_default_threshold_matches_plan():
    assert STALE_FRAME_MS == 500.0  # plan §4.2 帧新鲜度守卫


def test_navkit_v4_assembles_dirs_and_idempotent_start(pure_maa, tmp_path):
    """装配契约：真源目录持有、模板目录进桥宿主搜索根、start 幂等、poll 判健康。"""
    nav = tmp_path / "nav"
    nav.mkdir()
    (nav / "t.json").write_text(
        json.dumps({"t": {"recognition": "DirectHit", "action": "DoNothing"}}),
        encoding="utf-8")
    (tmp_path / "img").mkdir()
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = ng.NavKitV4(ctx, pipeline_dirs=[nav], image_dirs=[tmp_path / "img"])
    v4._resource.post_pipeline.return_value.wait.return_value.failed = False
    v4._tasker.post_task.return_value.status.done = True  # 起跑后立即"完成"（桩）
    assert [str(d) for d in v4._pipeline_dirs] == [str(nav)]
    assert str(tmp_path / "img") in [str(d) for d in v4._graph.image_dirs]
    assert v4.poll() is False  # 未起跑
    assert v4.load() is True  # 桩 Resource：post_pipeline 直接成功
    assert v4.load() is True  # 幂等
    assert v4.start("t") is True
    assert v4.start("t") is True  # 起跑幂等
    v4.stop()
    v4.shutdown()
