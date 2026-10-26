# -*- coding: utf-8 -*-
"""跑图器收尾契约：销毁前必须确认框架收摊，销毁时机与顺序由我们定。

回归背景（真机崩溃，近七天 6 次）：GUI 点停止后界面永远停在「停止中...」，
游戏动作确实停了；系统事件日志显示后台进程在收尾阶段崩在 MaaFramework.dll
（0xC0000005）。最小实验定案：**框架 worker 仍在活动时销毁 C 句柄 → 必崩**，
停止完成后再销毁则稳定。故这里锁三件事：

1. stop() 同时等「停止请求」与「任务句柄」，缺一都可能留下仍在收摊的 worker；
2. shutdown() 在销毁句柄之前先 stop，且销毁是显式置空（不等垃圾回收定时机）；
3. 两者幂等——停止路径可能被模块 stop 与 cleanup 各调一次。

注意：maa binding 的 C 句柄对象不能在 pytest 进程内反复构造/回收
（GC 顺序触发 C 层悬空回调 → 0xC0000005），本文件全部用纯 Python 桩。
"""
from __future__ import annotations

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
    """把 nav_graph 命名空间里的 maa 工厂替换为无 C 层的桩。"""

    class _NoInit:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(ng, "CustomController", _NoInit)
    monkeypatch.setattr(ng, "CustomAction", _NoInit)
    monkeypatch.setattr(ng, "CustomRecognition", _NoInit)
    monkeypatch.setattr(ng, "PipelineLogger", _NoInit)
    monkeypatch.setattr(ng, "Resource", lambda: MagicMock())
    monkeypatch.setattr(ng, "Tasker", lambda: MagicMock())


class _FakeJob:
    """只回答「结束了吗」的任务句柄桩：记录被探问次数。

    done_after：前 N 次探问回答「还在跑」，第 N+1 次起回答「结束了」——
    用来模拟真实收摊过程，同时避免测试等满真实超时。
    """

    def __init__(self, done: bool = True, done_after: int | None = None):
        self._done = done
        self._done_after = done_after
        self.probes = 0

    @property
    def done(self):
        self.probes += 1
        if self._done_after is not None:
            return self.probes > self._done_after
        return self._done


class _Ctx:
    """NavGraph/NavKitV4 只用到 ctx 的少量属性。"""

    def __init__(self):
        self.capture = MagicMock()
        self.lifecycle = MagicMock()
        self.click_mode = "real"


def test_navgraph_stop_waits_both_jobs(pure_maa):
    """任务在跑时：停止请求与任务句柄都要等，只等其一会留下仍在收摊的 worker。"""
    graph = ng.NavGraph(_Ctx())
    stop_job = _FakeJob()
    graph._tasker.post_stop.return_value = stop_job
    task_job = _FakeJob(done_after=1)   # 免发检查说「在跑」，等待时说「结束了」
    graph._job = task_job

    graph.stop()

    assert stop_job.probes > 0 and task_job.probes > 1


def test_navgraph_skips_stop_request_when_task_finished(pure_maa):
    """任务已结束就不投递停止请求：没有可停的东西，投出去的句柄没人消费。"""
    graph = ng.NavGraph(_Ctx())
    graph._job = _FakeJob(done=True)

    graph.stop()

    graph._tasker.post_stop.assert_not_called()


def test_navgraph_stop_request_sent_once_per_run(pure_maa):
    """停止会从模块停止路径与跑图线程两处触发，只能投递一次。"""
    graph = ng.NavGraph(_Ctx())
    graph._tasker.post_stop.return_value = _FakeJob()
    graph._job = _FakeJob(done_after=1)

    graph.stop()
    graph.stop()

    assert graph._tasker.post_stop.call_count == 1


def test_navgraph_shutdown_waits_before_destroying(pure_maa):
    """销毁必须发生在收摊之后，且句柄是显式置空（不留给垃圾回收）。"""
    graph = ng.NavGraph(_Ctx())
    tasker = graph._tasker
    resource = graph._resource
    stop_job = _FakeJob()
    tasker.post_stop.return_value = stop_job
    task_job = _FakeJob(done_after=1)
    graph._job = task_job

    graph.shutdown()

    assert stop_job.probes > 0 and task_job.probes > 1
    assert graph._tasker is None
    assert graph._resource is None
    del tasker, resource  # 桩对象不参与真实析构，仅表明不再被持有


def test_navgraph_shutdown_idempotent(pure_maa):
    """stop 与 cleanup 各调一次是正常路径，第二次不能炸。"""
    graph = ng.NavGraph(_Ctx())
    graph.shutdown()
    graph.shutdown()
    assert graph._tasker is None and graph._resource is None


def test_navkitv4_shutdown_destroys_handles(pure_maa, tmp_path):
    """常驻图通路：任务执行器 / 帧注入控制器 / 资源都要显式销毁。"""
    nav = ng.NavKitV4(_Ctx(), pipeline_dirs=[tmp_path])
    tasker = nav._tasker
    stop_job = _FakeJob()
    tasker.post_stop.return_value = stop_job
    nav._job = _FakeJob()

    nav.shutdown()

    assert stop_job.probes > 0
    assert nav._tasker is None
    assert nav._controller is None
    assert nav._resource is None


def test_stop_does_not_hang_when_framework_never_settles(pure_maa, monkeypatch):
    """框架卡住时按期限放弃等待——否则「停止中」永不结束（本次现象的另一来源）。"""
    monkeypatch.setattr(ng, "STOP_SETTLE_TIMEOUT_S", 0.05)
    graph = ng.NavGraph(_Ctx())
    graph._tasker.post_stop.return_value = _FakeJob(done=False)
    graph._job = _FakeJob(done=False)   # 一直「在跑」：两个等待都要靠期限退出

    started = time.monotonic()
    graph.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0            # 有界：没有无限等
    assert graph._tasker is not None  # 超时不影响停止路径本身


def test_navkitv4_shutdown_idempotent(pure_maa, tmp_path):
    nav = ng.NavKitV4(_Ctx(), pipeline_dirs=[tmp_path])
    nav.shutdown()
    nav.shutdown()
    assert nav._tasker is None
