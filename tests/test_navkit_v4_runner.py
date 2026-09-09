# -*- coding: utf-8 -*-
"""P2a-Q4：WgcapController（帧注入/新鲜度守卫）与 NavKitV4 装配契约。

注意：maa binding 的 C 句柄对象（CustomController/Tasker/Resource）不能在
pytest 进程内反复构造/回收（GC 顺序触发 C 层悬空回调 → 0xC0000005 崩溃），
本文件全部经 monkeypatch 把 maa 工厂替换为纯 Python 桩，只测 Python 逻辑。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
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


def _stub_post_recording(calls: list):
    """post 桩：记录每次 post 的目标目录内文件名，并返回成功 job。"""

    def _post(path):
        calls.append(sorted(p.name for p in Path(path).iterdir()))
        job = MagicMock()
        job.failed = False
        job.wait = MagicMock(return_value=job)
        return job

    return _post


def _make_nav_dir(root, name, nodes):
    d = root / name
    d.mkdir()
    (d / f"{name}.json").write_text(json.dumps(nodes, ensure_ascii=False), encoding="utf-8")
    return d


def test_navkit_v4_merges_crossref_dirs_into_single_post(pure_maa, tmp_path):
    """互指真源（core 骨架 ↔ 模块图）必须合并为一次 post：框架按 post 校验
    节点引用存在性，分次 post 时先加载者必然失败（P2b 真机首炸根因）。"""
    d1 = _make_nav_dir(tmp_path, "core", {"a.hall": {"next": "t.anchor"}})
    d2 = _make_nav_dir(tmp_path, "plugin", {"t.anchor": {"next": "a.hall"}})
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = ng.NavKitV4(ctx, pipeline_dirs=[d1, d2], image_dirs=[])
    calls: list = []
    v4._resource.post_pipeline.side_effect = _stub_post_recording(calls)
    assert v4.load() is True
    assert len(calls) == 1  # 一次 post
    assert calls[0] == ["core.json", "plugin.json"]  # 两目录内容合并到同一树


def test_navkit_v4_rejects_filename_conflict(pure_maa, tmp_path):
    """合并目录遇同名真源文件时拒绝加载（静默覆盖会悄悄换图）。"""
    d1 = _make_nav_dir(tmp_path, "x", {"n1": {}})
    d2 = _make_nav_dir(tmp_path, "y", {"n2": {}})
    (d2 / "x.json").write_text("{}", encoding="utf-8")  # 与 d1 同名
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = ng.NavKitV4(ctx, pipeline_dirs=[d1, d2], image_dirs=[])
    v4._resource.post_pipeline.side_effect = AssertionError("冲突应拒绝 post")
    assert v4.load() is False


def test_navkit_v4_rejects_missing_pipeline_dir(pure_maa, tmp_path):
    """真源目录不存在直接拒绝（可诊断），不进 post。"""
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = ng.NavKitV4(ctx, pipeline_dirs=[tmp_path / "nope"], image_dirs=[])
    v4._resource.post_pipeline.side_effect = AssertionError("缺失应拒绝 post")
    assert v4.load() is False


def test_click_action_uses_xywh_rect_contract(pure_maa):
    """MaaFW rect 契约 = (x, y, w, h)：ClickAction 落点 = rect 中心。

    真机六炸根修——analyze 曾返回 (x1,y1,x2,y2) 混填 w/h 槽，框架按帧边界
    clip 后中心恒错位到 (0.5,0.5) 附近（卡片 (999,591,1103,633) → 被裁成
    (999,591,281,129) → 点击落屏幕中心）。
    """
    graph = MagicMock()
    graph.frame_size.return_value = (1280, 720)
    graph.click.return_value = True
    act = ng.ClickAction(graph)
    argv = MagicMock()
    argv.custom_action_param = json.dumps({})
    argv.node_name = "t.card"
    argv.box = (999, 591, 104, 42)
    assert act.run(None, argv) is True
    cx, cy, box_norm = graph.click.call_args[0]
    assert graph.click.call_args[1]["timeout_s"] == 20.0
    assert abs(cx - (999 + 52) / 1280) < 1e-9
    assert abs(cy - (591 + 21) / 720) < 1e-9
    assert box_norm == (104 / 1280, 42 / 720)
