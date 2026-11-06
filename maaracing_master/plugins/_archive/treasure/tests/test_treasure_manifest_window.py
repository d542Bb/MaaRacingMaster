# -*- coding: utf-8 -*-
"""treasure manifest 活动窗口契约锁（随活动退役）。"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from maaracing_master.core.module_validity import GAME_TZ, parse_endpoint

pytestmark = pytest.mark.retired

_MANIFEST = Path(__file__).resolve().parents[1] / "manifest.py"

# treasure 声明的活动窗口（游戏服时间 UTC+8）：2026-08-06 05:00 — 2026-09-23 04:59
_WINDOW_START = datetime(2026, 8, 6, 5, 0, 0, tzinfo=GAME_TZ)
_WINDOW_END = datetime(2026, 9, 23, 4, 59, 59, tzinfo=GAME_TZ)


def _load_manifest(path: Path):
    """按文件路径加载 manifest，不经 plugins 包（不拉起插件重依赖）。"""
    spec = importlib.util.spec_from_file_location("_manifest_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_declared_window():
    m = _load_manifest(_MANIFEST)
    start, err_start = parse_endpoint(m.VALID_FROM, "VALID_FROM")
    until, err_until = parse_endpoint(m.VALID_UNTIL, "VALID_UNTIL")
    assert err_start is None and err_until is None
    assert start == _WINDOW_START
    assert until == _WINDOW_END


def test_id_and_module_class_unchanged():
    m = _load_manifest(_MANIFEST)
    assert m.ID == "treasure"
    assert m.MODULE_CLASS == "module.TreasureModule"
