# -*- coding: utf-8 -*-
"""fatal 通道与总时限（ADR-0018 §错误传播）。

协议事实：MaaFW 里「识别命中但动作失败」会进入该节点的 `on_error`，与「视觉没命中」
走同一条路。所以基础设施故障不能靠 on_error 传播——桥内记 fatal，由轮询循环与
run 收尾各查一次。这里锁三件事：

1. **job 成功后仍要复查 fatal**：动作桥可能在最后一刻记录故障，不能被成功状态覆盖；
2. 总时限到期先停框架再返回，结局是 deadline；
3. **只有「完全没有帧」才算基础设施故障**：WGC 对静止画面不出新帧（加载页/后台
   暂停），帧龄增长是正常状态，继续复用最后一帧等待。
"""
from __future__ import annotations

import math
import time
from unittest.mock import MagicMock

import pytest

try:
    from maaracing_master.core import nav_graph as ng

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)


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
    # 收摊等待有 10s 上限；桩里不需要真的等满
    monkeypatch.setattr(ng, "_wait_job_settled", lambda job, timeout_s=None: True)


class _Ctx:
    def __init__(self, frame_age_ms: float = 5.0):
        self.capture = MagicMock()
        self.capture.frame_with_age.return_value = (object(), 1, 0.0, frame_age_ms)
        self.lifecycle = MagicMock()
        self.lifecycle.running = True
        self.click_mode = "real"


class _Job:
    def __init__(self, done: bool, succeeded: bool = True):
        self.status = MagicMock()
        self.status.done = done
        self.succeeded = succeeded

    @property
    def done(self):  # 真 TaskJob 同时暴露 .done 与 .status.done
        return self.status.done


def _graph(ctx: _Ctx, job: _Job) -> ng.NavGraph:
    g = ng.NavGraph(ctx)
    g._loaded = True  # 跳过真加载：本文件只测 run 的结局判定
    g._tasker = MagicMock()
    g._tasker.post_task.return_value = job
    g._tasker.get_latest_node.return_value = MagicMock(completed=True)
    return g


def test_fatal_recorded_during_run_wins_over_job_success(pure_maa):
    ctx = _Ctx()
    g = _graph(ctx, _Job(done=True, succeeded=True))
    g._tasker.post_task.side_effect = lambda entry: (g.report_fatal("截图链路挂了"), g._tasker.post_task.return_value)[1]

    assert g.run("startup.boot", reached="startup.done") is False
    assert g.last_outcome == "task_failed", "job 成功不得覆盖已记录的 fatal"


def test_fatal_from_missing_frame_terminates_run(pure_maa):
    ctx = _Ctx()
    ctx.capture.frame_with_age.return_value = (None, 0, 0, math.inf)
    g = _graph(ctx, _Job(done=False))

    assert g.run("startup.boot", deadline_s=2.0) is False
    assert g.last_outcome == "task_failed"


def test_stale_frame_is_not_fatal(pure_maa):
    """WGC 静止画面不出新帧：帧龄增长只告警、继续等，不判采集故障。"""
    ctx = _Ctx(frame_age_ms=ng.FRAME_STATIC_WARN_MS + 1000)
    g = _graph(ctx, _Job(done=False))

    assert g.run("startup.boot", deadline_s=0.8) is False
    assert g.last_outcome == "deadline"


def test_deadline_stops_framework_and_reports(pure_maa):
    g = _graph(_Ctx(), _Job(done=False))
    t0 = time.perf_counter()
    assert g.run("startup.boot", deadline_s=0.05) is False
    assert g.last_outcome == "deadline"
    assert time.perf_counter() - t0 < 1.5, "到期必须尽快返回，而不是等满收摊上限"
    g._tasker.post_stop.assert_called_once()


def test_reached_requires_terminal_node_completed(pure_maa):
    g = _graph(_Ctx(), _Job(done=True, succeeded=True))
    g._tasker.get_latest_node.return_value = MagicMock(completed=False)
    assert g.run("startup.boot", reached="startup.done") is False
    assert g.last_outcome == "task_failed"

    g2 = _graph(_Ctx(), _Job(done=True, succeeded=True))
    assert g2.run("startup.boot", reached="startup.done") is True
    assert g2.last_outcome == "reached"


# ---------------- 动作桥的致命/可重试分类 ----------------

class _Argv:
    node_name = "startup.login.engine_start"
    custom_action_param = "{}"
    box = (100, 100, 20, 20)


class _BridgeGraph:
    """只实现 ClickAction 用到的几个入口。"""

    def __init__(self, ok: bool, reason: str):
        self._ok = ok
        self.last_click_error = reason
        self.fatal: list[str] = []
        self.lines: list[tuple[str, str]] = []
        self.ctx = MagicMock()

    def frame_size(self):
        return (1280, 720)

    def frame(self):
        return None

    def click(self, *a, **k):
        return self._ok

    def report_fatal(self, reason):
        self.fatal.append(reason)

    def _publish_intent(self, *args, **kwargs):
        pass

    def _log(self, msg, level="INFO"):
        self.lines.append((level, msg))


def test_unbound_gamepad_is_fatal():
    g = _BridgeGraph(ok=False, reason="gamepad_unbound")
    assert ng.ClickAction(g).run(None, _Argv()) is False
    assert g.fatal, "手柄未绑定是基础设施故障，重试没有意义"


def test_foreground_and_navigate_failures_are_retryable():
    for reason in ("not_foreground", "navigate", "submit"):
        g = _BridgeGraph(ok=False, reason=reason)
        assert ng.ClickAction(g).run(None, _Argv()) is False
        assert g.fatal == [], f"{reason} 是可重试条件，不能判致命"
