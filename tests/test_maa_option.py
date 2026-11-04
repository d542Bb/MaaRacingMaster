# -*- coding: utf-8 -*-
"""框架侧落盘策略：固定「不写持续日志、只在识别失败时留截图」，与界面开关无关。

**要守住的三条**：
1. 启动写策略后 `logging` 一律 false、`save_on_error` 一律 true——即使旧配置来自
   界面开关联动（logging=true），也按固定策略改写；
2. 本模块不管的键（`save_draw` / `draw_quality` / `stdout_level`）原样保留，
   文件缺失或损坏时按框架默认值补齐——不许把整份配置写成空；
3. 写不进去只返回 None、不抛异常（启动不得被它拖垮），且不留下半截临时文件。
"""
from __future__ import annotations

import json
import os
import time

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


def test_policy_overrides_switch_linked_values(opt_dir):
    """旧配置由界面开关写成 logging=false / save_on_error=true，策略一律改回固定值。"""
    opt_dir.parent.mkdir(parents=True, exist_ok=True)
    opt_dir.write_text(json.dumps({
        "logging": False, "save_on_error": True,
    }), encoding="utf-8")

    applied = maa_option.apply_policy()

    assert applied is not None
    data = _read(opt_dir)
    assert data["logging"] is True
    assert data["save_on_error"] is False


def test_untouched_keys_preserved(opt_dir):
    """既有文件里本模块不管的键原样保留（不拿默认值覆盖用户/框架的设定）。"""
    opt_dir.parent.mkdir(parents=True, exist_ok=True)
    opt_dir.write_text(json.dumps({
        "draw_quality": 60, "save_draw": True, "stdout_level": 5,
    }), encoding="utf-8")

    maa_option.apply_policy()
    data = _read(opt_dir)
    assert data["draw_quality"] == 60
    assert data["save_draw"] is True
    assert data["stdout_level"] == 5


def test_missing_file_falls_back_to_framework_defaults(opt_dir):
    """文件缺失时按框架默认值补齐（与 maafw 5.12.3 生成结果一致）。"""
    maa_option.apply_policy()
    data = _read(opt_dir)
    assert data["draw_quality"] == 85
    assert data["save_draw"] is False
    assert data["stdout_level"] == 2


def test_corrupt_file_does_not_blank_config(opt_dir):
    """损坏的 JSON 按默认值重建，不把整份配置写成空对象。"""
    opt_dir.parent.mkdir(parents=True, exist_ok=True)
    opt_dir.write_text("{ not json", encoding="utf-8")

    maa_option.apply_policy()
    data = _read(opt_dir)
    assert data["logging"] is True and data["save_on_error"] is False
    assert data["draw_quality"] == 85


def test_write_failure_returns_none_without_raising(tmp_path, monkeypatch):
    """目录不可建（父路径是个文件）时返回 None，不抛、不留临时文件。"""
    blocker = tmp_path / "framework"
    blocker.write_text("x", encoding="utf-8")     # 占位文件，让 config/ 建不出来
    monkeypatch.setattr(maa_option, "framework_dir", lambda: blocker)

    assert maa_option.apply_policy() is None
    assert not (tmp_path / "framework" / "config").exists()


# ---------- 文本日志备份上限 ----------

@pytest.fixture
def fw_dir(tmp_path, monkeypatch):
    """把框架目录指到 tmp。"""
    root = tmp_path / "framework"
    monkeypatch.setattr(maa_option, "framework_dir", lambda: root)
    return root


def _backup(root, i, size, mtime):
    path = root / "debug" / f"maafw.bak.2026.01.0{i}-00.00.00.000.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_prune_log_backups_keeps_newest_under_cap(fw_dir, monkeypatch):
    """超量时从最旧的备份删起；当前 maafw.log 与截图不归它管。"""
    monkeypatch.setattr(maa_option, "LOG_BACKUP_MAX_BYTES", 10)
    now = time.time()
    paths = [_backup(fw_dir, i, 6, now - 300 + i) for i in range(3)]
    current = fw_dir / "debug" / "maafw.log"
    current.write_bytes(b"x" * 100)
    shot = fw_dir / "debug" / "on_error" / "a.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"x")

    removed, freed = maa_option.prune_log_backups()

    assert removed == 2 and freed == 12
    assert not paths[0].exists() and not paths[1].exists()
    assert paths[2].exists() and current.exists() and shot.exists()


def test_prune_log_backups_noop_when_under_cap(fw_dir):
    """没超上限时一个都不删。"""
    _backup(fw_dir, 0, 4, time.time())
    assert maa_option.prune_log_backups() == (0, 0)


def test_prune_log_backups_missing_dir_is_noop(fw_dir):
    """debug 目录尚未生成时返回 (0, 0)，不抛。"""
    assert maa_option.prune_log_backups() == (0, 0)

