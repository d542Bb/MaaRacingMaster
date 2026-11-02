# -*- coding: utf-8 -*-
"""框架侧产物开关：`maa_option.json` 跟随两项调试开关。

**要守住的三条**：
1. 两个开关都关 → `logging` / `save_on_error` 双 false（框架侧零落盘的前提）；
2. 本模块不管的键（`save_draw` / `draw_quality` / `stdout_level`）原样保留，
   文件缺失或损坏时按框架默认值补齐——不许把整份配置写成空；
3. 写不进去只返回 None、不抛异常（开关切换与启动都不得被它拖垮），且不留下
   半截临时文件（半截 JSON 会让框架回落默认值，等于把开关又打开）。
"""
from __future__ import annotations

import json

import pytest

try:
    from maaracing_master.core import maa_option
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


@pytest.fixture
def opt_dir(tmp_path, monkeypatch):
    """把框架目录指到 tmp，返回选项文件路径。"""
    root = tmp_path / "framework"
    monkeypatch.setattr(maa_option, "framework_dir", lambda: root)
    return root / "config" / "maa_option.json"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_both_switches_off_disables_framework_products(opt_dir):
    """两个开关都关：框架日志与失败截图双 false（用户要求的「不落盘」前提）。"""
    applied = maa_option.sync(False, False)
    assert applied is not None
    data = _read(opt_dir)
    assert data["logging"] is False
    assert data["save_on_error"] is False


def test_switches_map_independently(opt_dir):
    """截图开关管 save_on_error，日志开关管 logging，互不串位。"""
    maa_option.sync(True, False)
    data = _read(opt_dir)
    assert data["save_on_error"] is True
    assert data["logging"] is False

    maa_option.sync(False, True)
    data = _read(opt_dir)
    assert data["save_on_error"] is False
    assert data["logging"] is True


def test_untouched_keys_preserved(opt_dir):
    """既有文件里本模块不管的键原样保留（不拿默认值覆盖用户/框架的设定）。"""
    opt_dir.parent.mkdir(parents=True, exist_ok=True)
    opt_dir.write_text(json.dumps({
        "draw_quality": 60, "save_draw": True, "stdout_level": 5, "logging": True,
    }), encoding="utf-8")

    maa_option.sync(False, False)
    data = _read(opt_dir)
    assert data["draw_quality"] == 60
    assert data["save_draw"] is True
    assert data["stdout_level"] == 5
    assert data["logging"] is False


def test_missing_file_falls_back_to_framework_defaults(opt_dir):
    """文件缺失时按框架默认值补齐（与 maafw 5.12.3 生成结果一致）。"""
    maa_option.sync(False, False)
    data = _read(opt_dir)
    assert data["draw_quality"] == 85
    assert data["save_draw"] is False
    assert data["stdout_level"] == 2


def test_corrupt_file_does_not_blank_config(opt_dir):
    """损坏的 JSON 按默认值重建，不把整份配置写成空对象。"""
    opt_dir.parent.mkdir(parents=True, exist_ok=True)
    opt_dir.write_text("{ not json", encoding="utf-8")

    maa_option.sync(True, True)
    data = _read(opt_dir)
    assert data["logging"] is True and data["save_on_error"] is True
    assert data["draw_quality"] == 85


def test_write_failure_returns_none_without_raising(tmp_path, monkeypatch):
    """目录不可建（父路径是个文件）时返回 None，不抛、不留临时文件。"""
    blocker = tmp_path / "framework"
    blocker.write_text("x", encoding="utf-8")     # 占位文件，让 config/ 建不出来
    monkeypatch.setattr(maa_option, "framework_dir", lambda: blocker)

    assert maa_option.sync(False, False) is None
    assert not (tmp_path / "framework" / "config").exists()
