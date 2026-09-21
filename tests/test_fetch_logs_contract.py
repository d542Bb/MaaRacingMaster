# -*- coding: utf-8 -*-
"""fetch_logs 双格式过渡契约（DESIGN_log_api v2 §6 / §8 第 4-5 步）。

锁三件事：
1. events 结构化通道——log 按 INFO+ 过滤、组事件不受级别约束；
2. lines 字节兼容——与 get_lines_since 同写入序列输出一致（旧前端零感知）；
3. 游标单次推进 + session_id/schema_version/gap 常在场。
"""
from collections import deque
import threading

import pytest

try:
    import maaracing_master.core.sidecar as sidecar_mod
    _SIDECAR_OK, _SIDECAR_ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _SIDECAR_OK, _SIDECAR_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _SIDECAR_OK, reason=f"需要 core.sidecar 运行时依赖：{_SIDECAR_ERR}")

from maaracing_master.core.logger import Logger  # noqa: E402


def _svc():
    svc = sidecar_mod.SidecarService.__new__(sidecar_mod.SidecarService)  # 绕开真实 controller 装配
    svc._lock = threading.Lock()
    svc._last_log_seq = 0
    return svc


@pytest.fixture
def fresh_logger(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(sidecar_mod, "logger", lg)
    return lg


def test_events_channel_shape(fresh_logger):
    fresh_logger.log("plain")
    g = fresh_logger.group("phase", kind="phase")
    g.log("debug-detail", "DEBUG")   # 级别过滤：不进 events
    g.log("warn", "WARNING")
    g.end()
    ok, payload, err = _svc().fetch_logs({})
    assert ok and err is None
    assert payload["schema_version"] == Logger.SCHEMA_VERSION
    assert payload["session_id"] == fresh_logger.session_id
    types = [e["event_type"] for e in payload["events"]]
    assert types == ["log", "group_start", "log", "group_end"]  # DEBUG 滤除、组事件保留
    end = payload["events"][-1]
    assert end["outcome"] == "warning" and end["group_id"] == g.id


def test_lines_byte_compat_with_legacy(fresh_logger):
    fresh_logger.log("a")
    fresh_logger.log("b", "DEBUG")
    g = fresh_logger.group("g")
    g.log("c")
    g.end()
    expect, _seq, _tr = fresh_logger.get_lines_since(0, "INFO")
    _ok, payload, _ = _svc().fetch_logs({})
    assert payload["lines"] == expect  # 结构事件不混入 legacy 文本行


def test_cursor_advances_once(fresh_logger):
    fresh_logger.log("x")
    svc = _svc()   # 游标挂在 service 实例上（生产为单例轮询），必须同一实例连拉
    _ok, p1, _ = svc.fetch_logs({})
    _ok, p2, _ = svc.fetch_logs({})
    assert len(p1["events"]) == 1 and p2["events"] == []
    assert p2["next_seq"] == p1["next_seq"]


def test_gap_explicit_on_truncation(fresh_logger):
    fresh_logger._lines = deque(fresh_logger._lines, maxlen=2)  # 缩容模拟落后
    for i in range(5):
        fresh_logger.log(f"l{i}")
    _ok, payload, _ = _svc().fetch_logs({})
    assert payload["truncated"] is True
    assert payload["gap"] == {"from_seq": 1, "to_seq": 3}
    assert payload["lines"][0].startswith("[!!]")  # legacy 提示文本保留
