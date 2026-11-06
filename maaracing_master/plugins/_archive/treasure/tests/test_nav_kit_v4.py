# -*- coding: utf-8 -*-
"""归档鉴宝的 v4 常驻图宿主契约（随插件下沉，默认不跑）。

原在 `tests/test_navkit_v4_runner.py` 与 `test_nav_graph_teardown.py`，与现役的
WgcapController / NavGraph 测试混在一起。宿主下沉后测试随迁；现役部分改用
NavGraph 直接构造，不再依赖归档类。

注意：maa binding 的 C 句柄对象不能在 pytest 进程内反复构造/回收，本文件经
monkeypatch 把宿主模块里的 maa 工厂替换为纯 Python 桩，只测 Python 逻辑。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

try:
    from maaracing_master.plugins._archive.treasure.core import nav_kit_v4 as nk

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺重依赖的机器上整文件 SKIP
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)
    nk = None  # type: ignore[assignment]

pytestmark = [
    pytest.mark.retired,
    pytest.mark.skipif(not _RUNTIME_OK,
                       reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"),
]


@pytest.fixture()
def pure_maa(monkeypatch):
    """把宿主模块命名空间里的 maa 工厂替换为无 C 层的桩。"""
    class _NoInit:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(nk, "CustomAction", _NoInit)
    monkeypatch.setattr(nk, "PipelineLogger", _NoInit)
    monkeypatch.setattr(nk, "Resource", lambda: MagicMock())
    monkeypatch.setattr(nk, "Tasker", lambda: MagicMock())


class _FakeCapture:
    """CaptureAdapter 形态的桩：可控帧/年龄。"""

    def __init__(self):
        self.frame: np.ndarray | None = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.fid = 7
        self.age = 0.0

    def frame_with_age(self):
        return (self.frame, self.fid, 0, self.age)


class _FakeJob:
    """只回答「结束了吗」的任务句柄桩：记录被探问次数。"""

    def __init__(self, done: bool = True):
        self._done = done
        self.probes = 0

    @property
    def done(self):
        self.probes += 1
        return self._done


class _Ctx:
    """宿主只用到 ctx 的少量属性。"""

    def __init__(self):
        self.capture = MagicMock()
        self.lifecycle = MagicMock()
        self.click_mode = "real"


def _make_nav_dir(root, name, nodes):
    d = root / name
    d.mkdir()
    (d / f"{name}.json").write_text(json.dumps(nodes, ensure_ascii=False), encoding="utf-8")
    return d


def _stub_post_recording(calls: list):
    """post 桩：记录每次 post 的目标目录内文件名，并返回成功 job。"""

    def _post(path):
        calls.append(sorted(p.name for p in Path(path).iterdir()))
        job = MagicMock()
        job.failed = False
        job.wait = MagicMock(return_value=job)
        return job

    return _post


# ----------------------------------------------------------------------
# 装配契约
# ----------------------------------------------------------------------


def test_navkit_v4_assembles_dirs_and_idempotent_start(pure_maa, tmp_path):
    """装配契约：真源目录持有、模板目录进桥宿主搜索根、start 幂等、poll 判健康。"""
    nav = _make_nav_dir(tmp_path, "nav", {"t": {"recognition": "DirectHit",
                                                 "action": "DoNothing"}})
    (tmp_path / "img").mkdir()
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = nk.NavKitV4(ctx, pipeline_dirs=[nav], image_dirs=[tmp_path / "img"])
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


def test_navkit_v4_merges_crossref_dirs_into_single_post(pure_maa, tmp_path):
    """互指真源（core 骨架 ↔ 模块图）必须合并为一次 post：框架按 post 校验
    节点引用存在性，分次 post 时先加载者必然失败（P2b 真机首炸根因）。"""
    d1 = _make_nav_dir(tmp_path, "core", {"a.hall": {"next": "t.anchor"}})
    d2 = _make_nav_dir(tmp_path, "plugin", {"t.anchor": {"next": "a.hall"}})
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = nk.NavKitV4(ctx, pipeline_dirs=[d1, d2], image_dirs=[])
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
    v4 = nk.NavKitV4(ctx, pipeline_dirs=[d1, d2], image_dirs=[])
    v4._resource.post_pipeline.side_effect = AssertionError("冲突应拒绝 post")
    assert v4.load() is False


def test_navkit_v4_rejects_missing_pipeline_dir(pure_maa, tmp_path):
    """真源目录不存在直接拒绝（可诊断），不进 post。"""
    ctx = MagicMock()
    ctx.capture.frame_with_age = _FakeCapture().frame_with_age
    v4 = nk.NavKitV4(ctx, pipeline_dirs=[tmp_path / "nope"], image_dirs=[])
    v4._resource.post_pipeline.side_effect = AssertionError("缺失应拒绝 post")
    assert v4.load() is False


# ----------------------------------------------------------------------
# 收尾契约
# ----------------------------------------------------------------------


def test_navkitv4_shutdown_destroys_handles(pure_maa, tmp_path):
    """常驻图通路：任务执行器 / 帧注入控制器 / 资源都要显式销毁。"""
    nav = nk.NavKitV4(_Ctx(), pipeline_dirs=[tmp_path])
    tasker = nav._tasker
    stop_job = _FakeJob()
    tasker.post_stop.return_value = stop_job
    nav._job = _FakeJob()

    nav.shutdown()

    assert stop_job.probes > 0
    assert nav._tasker is None
    assert nav._controller is None
    assert nav._resource is None


def test_navkitv4_shutdown_idempotent(pure_maa, tmp_path):
    nav = nk.NavKitV4(_Ctx(), pipeline_dirs=[tmp_path])
    nav.shutdown()
    nav.shutdown()
    assert nav._tasker is None
