# -*- coding: utf-8 -*-
"""决策流水（trace）与运行日志共用「日志记录」开关的契约测试。

不变式：
- 开关未开（`logger.session_dir` 为 None）→ 不建写手，两者都不落盘；
- 开关开着 → trace.jsonl 落进同一个会话目录 `logs/<ts>/`（与 MaaRM_<ts>.log 同放）；
- 中途关 → 收掉写手；再开（新会话目录）→ 跟到新目录；
- 只认自己开的写手：外部注入的 writer（调试/其它测试）不归本开关管。

module 顶层 import maa.toolkit，CI 轻依赖环境下收集期 ERROR 会中断整个会话；
按仓内既有口径整文件优雅跳过（见 tests/test_treasure_policy_bridge.py）。
"""
from __future__ import annotations

import json

import pytest

from maaracing_master.core.logger import Logger

try:
    from maaracing_master.plugins.treasure import module as mod
    from maaracing_master.plugins.treasure.module import TreasureModule
    from maaracing_master.core.navkit import TraceWriter  # noqa: F401

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)


class _FakeWriter:
    """外部注入的写手替身：只有 write/close，用于验证开关不越权。"""

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, payload):
        self.written.append(payload)

    def close(self):
        self.closed = True


@pytest.fixture
def host(tmp_path, monkeypatch):
    """返回 (裸模块实例, 临时 Logger)：只挂写手管理所需状态，不跑 __init__（会拉 MAA/采集）。"""
    log = Logger(tmp_path)
    monkeypatch.setattr(mod, "logger", log)
    m = TreasureModule.__new__(TreasureModule)
    m._trace_writer = None
    m._trace_sink = None
    return m, log


def test_no_writer_until_log_switch_on(host):
    m, log = host
    m._ensure_trace_sink()
    assert m._trace_writer is None          # 开关未开：不落盘
    assert log.session_dir is None
    assert list(log._log_dir.iterdir()) == []   # 连会话目录都不建


def test_writer_lands_in_log_session_dir(host):
    m, log = host
    log.set_file_logging(True)
    m._ensure_trace_sink()
    assert m._trace_sink == log.session_dir
    assert m._trace_writer is not None
    m._trace_writer.write({"frame": 1, "event": "unit"})
    trace = log.session_dir / "trace.jsonl"
    assert json.loads(trace.read_text(encoding="utf-8").strip())["event"] == "unit"
    # 与运行日志同处一个会话目录（取证时整个目录打包即可）
    assert log.log_file.parent == log.session_dir
    m._close_trace_writer()


def test_turn_off_stops_and_turn_on_restarts(host):
    m, log = host
    log.set_file_logging(True)
    m._ensure_trace_sink()
    assert m._trace_sink == log.session_dir
    log.set_file_logging(False)             # 中途关开关 → 停写
    m._ensure_trace_sink()
    assert m._trace_writer is None and m._trace_sink is None
    log.set_file_logging(True)              # 再开 → 重新建写手，落回当前会话目录
    m._ensure_trace_sink()
    assert m._trace_writer is not None
    assert m._trace_sink == log.session_dir
    m._close_trace_writer()


def test_writer_follows_changed_session_dir(host):
    """落点已不是当前会话目录（跨秒重开/换目录）→ 换到新目录重开写手。"""
    m, log = host
    log.set_file_logging(True)
    m._ensure_trace_sink()
    stale = log.session_dir.parent / "19990101_000000"
    m._trace_sink = stale                  # 伪造一个过期落点
    m._ensure_trace_sink()
    assert m._trace_sink == log.session_dir
    assert m._trace_sink != stale
    m._close_trace_writer()


def test_externally_injected_writer_not_governed(host):
    m, log = host
    fake = _FakeWriter()
    m._trace_writer = fake
    m._ensure_trace_sink()                  # 开关未开：不碰外部写手
    assert m._trace_writer is fake and fake.closed is False
    log.set_file_logging(True)
    m._ensure_trace_sink()                  # 开关开着：也不替换外部写手
    assert m._trace_writer is fake and fake.closed is False


def test_ensure_is_idempotent(host):
    m, log = host
    log.set_file_logging(True)
    m._ensure_trace_sink()
    writer = m._trace_writer
    m._ensure_trace_sink()
    assert m._trace_writer is writer         # 同会话目录不重开写手
    m._close_trace_writer()
    assert m._trace_writer is None and m._trace_sink is None
    m._close_trace_writer()                  # 幂等
