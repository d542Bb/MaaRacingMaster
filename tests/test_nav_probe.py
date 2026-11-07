# -*- coding: utf-8 -*-
"""A5：`NavGraph.probe()` 的只读契约、结果提交闸门与墓碑生命周期。

设计依据（A0 实测，见 `tools/maafw_audit/probe_a0/`）：
  - `stopping=True` 时框架**忽略**新任务 → `probe()` 必须在入口直接拒绝；
  - 桥异常时 `job.succeeded` 仍为 True → 结果只能以容器记录为准；
  - 只标记 stale 不够 → 提交侧必须检查记录仍为 `pending`。

maafw 的 C 句柄对象不能在 pytest 进程内反复构造，本文件沿用
`test_nav_graph_runner.py` 的口径：把 maa 工厂换成纯 Python 桩，只测 Python 逻辑。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

try:
    from maaracing_master.core import nav_graph as ng

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)

MODULE_SRC = Path(__file__).resolve().parents[1] / "maaracing_master/plugins/speedrush/module.py"


@pytest.fixture()
def pure_maa(monkeypatch):
    class _NoInit:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ng, "CustomController", _NoInit)
    monkeypatch.setattr(ng, "CustomAction", _NoInit)
    monkeypatch.setattr(ng, "CustomRecognition", _NoInit)
    monkeypatch.setattr(ng, "PipelineLogger", _NoInit)
    monkeypatch.setattr(ng, "Resource", lambda: MagicMock())
    monkeypatch.setattr(ng, "Tasker", lambda: MagicMock())


class _Capture:
    """RGB 帧源桩：左上角像素固定为 (10, 20, 30)，便于验通道序。"""

    def __init__(self) -> None:
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.frame[:, :] = (10, 20, 30)

    def frame_with_age(self):
        return (self.frame, 42, 123456789, 0.0)


class _Job:
    """任务桩：`done` 由测试控制，`succeeded` 恒真（模拟桥异常时的真实行为）。"""

    def __init__(self, done_after: float | None = None):
        self.succeeded = True
        self._until = None if done_after is None else time.monotonic() + done_after

    @property
    def done(self) -> bool:
        return self._until is not None and time.monotonic() >= self._until

    def finish_now(self) -> None:
        self._until = 0.0

    def wait(self):
        return self


def _graph(pure_maa, tmp_path, job, *, stopping: bool = False):
    nav = tmp_path / "nav"
    nav.mkdir()
    (nav / "t.json").write_text(
        json.dumps({"t": {"recognition": "DirectHit", "action": "DoNothing"}}),
        encoding="utf-8")
    (tmp_path / "img").mkdir()
    ctx = MagicMock()
    ctx.capture.frame_with_age = _Capture().frame_with_age
    graph = ng.NavGraph(ctx)
    graph.add_plugin(nav, tmp_path / "img")
    graph._resource.post_pipeline.return_value.wait.return_value.failed = False
    graph._tasker.stopping = stopping
    graph._tasker.post_task.return_value = job
    return graph


# ---------- 入口硬门控 ----------


def test_probe_refuses_when_stopping_without_side_effects(pure_maa, tmp_path):
    """stopping 时直接 NOT_READY：不建记录、不提交任务。

    A0 已证框架在 stopping 期间忽略新任务；若仍走「建记录→提交→等待→超时」，
    只是把已知的框架行为包装成一次无意义的任务尝试。
    """
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0), stopping=True)
    assert graph.probe("speedrush.驾驶页锚点") is ng.ProbeOutcome.NOT_READY
    graph._tasker.post_task.assert_not_called()
    assert graph._probe_records == {}


# ---------- 结果提交 ----------


def test_probe_hit_returns_hit(pure_maa, tmp_path):
    """桥提交命中 → HIT，且记录在返回前被清掉（不残留）。"""
    job = _Job(done_after=0.0)
    graph = _graph(pure_maa, tmp_path, job)
    seen: dict[str, str] = {}

    def post(entry, pipeline_override=None):
        pid = pipeline_override[ng.PROBE_ENTRY]["action"]["param"][
            "custom_action_param"]["probe_id"]
        seen["pid"] = pid
        assert graph._commit_probe(pid, hit=True) is True
        return job

    graph._tasker.post_task.side_effect = post
    assert graph.probe("speedrush.驾驶页锚点") is ng.ProbeOutcome.HIT
    assert seen["pid"] not in graph._probe_records


def test_probe_miss_when_bridge_silent(pure_maa, tmp_path):
    """任务结束但桥没提交（任务失败/节点没跑到）→ MISS，不是 NOT_READY。"""
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0))
    assert graph.probe("speedrush.驾驶页锚点") is ng.ProbeOutcome.MISS


def test_probe_bridge_error_is_miss_not_hit(pure_maa, tmp_path):
    """桥异常写 error：即使 job.succeeded 为真，也只能判 MISS。"""
    job = _Job(done_after=0.0)
    graph = _graph(pure_maa, tmp_path, job)

    def post(entry, pipeline_override=None):
        pid = pipeline_override[ng.PROBE_ENTRY]["action"]["param"][
            "custom_action_param"]["probe_id"]
        graph._commit_probe(pid, error="RuntimeError: boom")
        return job

    graph._tasker.post_task.side_effect = post
    assert graph.probe("speedrush.驾驶页锚点") is ng.ProbeOutcome.MISS


def test_commit_rejects_stale_and_unknown_ids(pure_maa, tmp_path):
    """提交闸门：stale 与不存在的 id 一律拒绝，绝不重建、绝不覆盖。"""
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0))
    with graph._probe_lock:
        rec = ng._ProbeRecord()
        rec.status = "stale"
        graph._probe_records["p-stale"] = rec
    assert graph._commit_probe("p-stale", hit=True) is False
    assert graph._probe_records["p-stale"].status == "stale"
    assert graph._probe_records["p-stale"].hit is False
    assert graph._commit_probe("p-missing", hit=True) is False
    assert "p-missing" not in graph._probe_records


# ---------- 超时与墓碑 ----------


def test_probe_timeout_settled_returns_miss_and_drops_record(pure_maa, tmp_path, monkeypatch):
    """超时但收摊确认 → MISS，记录删除；迟到回写此时无记录可写。"""
    monkeypatch.setattr(ng, "PROBE_BUDGET_S", 0.05)
    monkeypatch.setattr(ng, "PROBE_SETTLE_TIMEOUT_S", 0.1)
    job = _Job(done_after=None)
    graph = _graph(pure_maa, tmp_path, job)
    seen: dict[str, str] = {}

    def post(entry, pipeline_override=None):
        seen["pid"] = pipeline_override[ng.PROBE_ENTRY]["action"]["param"][
            "custom_action_param"]["probe_id"]
        return job

    graph._tasker.post_task.side_effect = post
    # 停止请求让任务句柄结束（模拟框架收摊成功）
    graph._tasker.post_stop.side_effect = lambda: job.finish_now() or _Job(done_after=0.0)

    assert graph.probe("speedrush.驾驶页锚点") is ng.ProbeOutcome.MISS
    assert seen["pid"] not in graph._probe_records


def test_probe_timeout_unsettled_returns_not_ready_and_keeps_tombstone(
        pure_maa, tmp_path, monkeypatch):
    """收摊没确认 → NOT_READY（不是 MISS），墓碑保留；迟到回写被丢弃。"""
    monkeypatch.setattr(ng, "PROBE_BUDGET_S", 0.05)
    monkeypatch.setattr(ng, "PROBE_SETTLE_TIMEOUT_S", 0.1)
    job = _Job(done_after=None)  # 永不结束：收摊必失败
    graph = _graph(pure_maa, tmp_path, job)
    seen: dict[str, str] = {}

    def post(entry, pipeline_override=None):
        seen["pid"] = pipeline_override[ng.PROBE_ENTRY]["action"]["param"][
            "custom_action_param"]["probe_id"]
        return job

    graph._tasker.post_task.side_effect = post
    graph._tasker.post_stop.side_effect = lambda: _Job(done_after=None)

    assert graph.probe("speedrush.驾驶页锚点") is ng.ProbeOutcome.NOT_READY
    rec = graph._probe_records[seen["pid"]]
    assert rec.status == "stale"
    # 迟到回写：不能复活、不能覆盖
    assert graph._commit_probe(seen["pid"], hit=True) is False
    assert graph._probe_records[seen["pid"]].status == "stale"
    assert graph._probe_records[seen["pid"]].hit is False


def test_tombstone_swept_only_when_framework_idle(pure_maa, tmp_path):
    """确认空闲后的下一次 probe 会清墓碑；stopping 时不清也不提交。"""
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0))
    with graph._probe_lock:
        rec = ng._ProbeRecord()
        rec.status = "stale"
        graph._probe_records["p-old"] = rec

    graph._tasker.stopping = True
    assert graph.probe("x") is ng.ProbeOutcome.NOT_READY
    assert "p-old" in graph._probe_records          # stopping 时不清墓碑
    graph._tasker.post_task.assert_not_called()

    graph._tasker.stopping = False
    graph.probe("x")
    assert "p-old" not in graph._probe_records      # 空闲后清掉


# ---------- 取帧通道 ----------


def test_probe_frame_converts_rgb_to_bgr(pure_maa, tmp_path):
    """capture 返回 RGB，框架契约要 BGR：桥必须先转，否则二次错色。"""
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0))
    frame = graph.probe_frame()
    assert frame is not None
    assert tuple(frame[0, 0]) == (30, 20, 10)


def test_probe_frame_returns_none_when_capture_missing(pure_maa, tmp_path):
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0))
    graph.ctx.capture.frame_with_age = lambda: (None, 0, 0, float("inf"))
    assert graph.probe_frame() is None


def test_probe_entry_override_zeroes_framework_pace(pure_maa, tmp_path):
    """入口 override 必须**显式**零掉 pre_delay / post_delay / rate_limit。

    不写就吃框架默认（各 200ms），一次「只读看一眼」白等 400ms。2026-11-07
    实机因此把驾驶主循环从 20Hz 压到 1.1Hz（每拍两次查询 = 800ms），深度帧
    100% 超龄、视角确认失败、阶段中止。这是回归锁，不是风格要求。
    """
    graph = _graph(pure_maa, tmp_path, _Job(done_after=0.0))
    captured: dict = {}

    def post(entry, pipeline_override=None):
        captured.update(pipeline_override[ng.PROBE_ENTRY])
        return _Job(done_after=0.0)

    graph._tasker.post_task.side_effect = post
    graph.probe("speedrush.驾驶页锚点")

    assert captured.get("pre_delay") == 0, "漏写 pre_delay 会吃框架默认 200ms"
    assert captured.get("post_delay") == 0, "漏写 post_delay 会吃框架默认 200ms"
    assert captured.get("rate_limit") == 0
    assert captured["recognition"] == {"type": "DirectHit"}
    # 参数必须是 dict（传字符串会被二次编码）
    param = captured["action"]["param"]["custom_action_param"]
    assert isinstance(param, dict)


# ---------- 结构约束（只验本次代码形态，不声称证明同实例串行） ----------


def test_only_two_drive_loop_call_sites_use_probe():
    """只有驾驶主循环那两个已过 A0-2 的调用点改调 probe。

    注意 `_wait_drive_ready` 里仍保留 `run(DRIVE_STAGE_NODE)`——那是**有意**的，
    不在本次替换范围（A0-2 没验过就绪判据用的节点），所以断言限定在 `_drive` 体内。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    probe_calls = re.findall(r"self\._graph\.probe\(", src)
    assert len(probe_calls) == 2, f"probe 调用点应为 2 个，实际 {len(probe_calls)}"
    assert "self._graph.probe(DRIVE_STAGE_NODE)" in src
    assert "STORE_BANNER_NODE" in src

    start = src.index("def _drive(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert not re.search(
        r"self\._graph\.run\(\s*(DRIVE_STAGE_NODE|STORE_BANNER_NODE)", body), \
        "驾驶主循环里这两个锚点不得再走导航动作路径"


def test_wait_drive_ready_still_uses_run():
    """就绪判据不在替换范围：A0-2 没验过它用的节点。"""
    src = MODULE_SRC.read_text(encoding="utf-8")
    start = src.index("def _wait_drive_ready")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "self._graph.run(" in body
    assert "self._graph.probe(" not in body


def test_ui_nav_segments_declare_save_drive_does_not():
    """UI 导航段显式声明落图；驾驶控制链不声明。

    `DEBUG_SAVE` 是**运行项级**的（整局一个值，控制器开 scope 时读一次），
    做不到「一半落一半不落」。真正的分界靠接口：`run` 开段且可声明 save，
    `probe` 不开段——所以驾驶期间识别再频繁也进不了调试流。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    for name in ("def _run_step", "def _wait_drive_ready"):
        start = src.index(name)
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        calls = re.findall(r"self\._graph\.run\([^)]*\)", body)
        assert calls, f"{name} 里应有 run 调用"
        for call in calls:
            assert "save=NAV_SAVE" in call, f"{name} 的 run 未声明落图：{call}"

    start = src.index("def _drive_loop")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "save=" not in body, "驾驶控制链不应声明存盘策略（probe 本就不开段）"


def test_drive_loop_introduces_no_thread_or_async():
    """两个调用点仍在原有单线程驾驶流程内，未引入线程/异步调度。

    只验本次代码形态；同实例串行仍是调用方契约，不由本测试证明。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    start = src.index("def _drive(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "threading.Thread" not in body
    assert "asyncio" not in body
    assert "concurrent.futures" not in body
