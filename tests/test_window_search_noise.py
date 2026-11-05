# -*- coding: utf-8 -*-
"""窗口搜索的日志噪声：轮询重试必须静默，首次诊断才打印窗口列表。"""
from __future__ import annotations

import pytest

wu = pytest.importorskip("maaracing_master.core.window_utils")


class _Toolkit:
    @staticmethod
    def init_option(path):
        pass

    @staticmethod
    def find_desktop_windows():
        return []


def test_quiet_search_does_not_log(monkeypatch, tmp_path):
    logs = []
    monkeypatch.setattr(wu, "Toolkit", _Toolkit)
    monkeypatch.setattr(wu, "framework_dir", lambda: tmp_path)
    monkeypatch.setattr(wu.logger, "log",
                        lambda msg, level="INFO", **kw: logs.append((level, msg)))

    assert wu.find_game_hwnd(verbose=False) == 0
    assert logs == [], "轮询重试不得逐次打印窗口列表"

    # 首次诊断保留完整窗口列表（排错要靠它）
    assert wu.find_game_hwnd() == 0
    assert logs and logs[0][0] == "ERROR"
    assert any("可用窗口前10个" in msg for _lv, msg in logs)
