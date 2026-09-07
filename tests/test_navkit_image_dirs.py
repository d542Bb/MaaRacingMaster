# -*- coding: utf-8 -*-
"""image_dirs 追加语义（I-2）与 compile_routes global 固定项 / validate-only 单测。

覆盖四条契约：
- I-2：`Assets.load` 显式传参时 core 段恒定前置（W04 的 `image_dirs[0]` 判据依赖此序）；
- `_default_image_dirs`：plugins 路径 / core 路径（root 为 None 硬条款）/ 模块目录缺失；
- compile_routes：global 固定项入扫描集合、无 routes 段 validate-only 跳过（N-P0）；
- `--all` 反断言：固定项缺席（条件式）与无真实模块段均须失败（N-P2）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from maaracing_assistant.core.navkit import CORE_IMAGE_DIR, Assets
from maaracing_assistant.core.navkit.assets import _default_image_dirs
from tools.navkit import compile_routes as cr

_REPO = Path(__file__).resolve().parents[1]
_TREASURE_ASSETS = (
    _REPO / "maaracing_assistant" / "plugins" / "treasure"
    / "resources" / "config" / "treasure_assets.json"
)
_TREASURE_IMAGE = (
    _REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"
)


# ------------------------------------------------------------------
# I-2：image_dirs 追加语义
# ------------------------------------------------------------------


def test_load_explicit_dirs_core_first():
    """显式传参 ≠ 替换：core 段恒为 [0]，W04 判据依赖此序。"""
    assets = Assets.load(
        _TREASURE_ASSETS, module="treasure", image_dirs=(_TREASURE_IMAGE,)
    )
    assert assets.image_dirs[0] == CORE_IMAGE_DIR
    assert assets.image_dirs[1] == _TREASURE_IMAGE


def test_default_image_dirs_plugins_path():
    dirs = _default_image_dirs(_TREASURE_ASSETS, "treasure")
    assert dirs == (CORE_IMAGE_DIR, _TREASURE_IMAGE)


def test_default_image_dirs_core_path_returns_core_only():
    """root 为 None 硬条款：global 文档（core 路径）必须拿到 core 段，禁止返回空。"""
    global_path = (
        _REPO / "maaracing_assistant" / "core" / "resources"
        / "config" / "global_assets.json"
    )
    assert _default_image_dirs(global_path, "global") == (CORE_IMAGE_DIR,)


def test_default_image_dirs_missing_module_dir(tmp_path):
    """模块 image 目录不存在时只回落 core 段，不得把不存在目录塞进 image_dirs。"""
    fake = tmp_path / "stage" / "plugins" / "demo" / "resources" / "config" / "demo_assets.json"
    fake.parent.mkdir(parents=True)
    assert _default_image_dirs(fake, "demo") == (CORE_IMAGE_DIR,)


# ------------------------------------------------------------------
# compile_routes：global 固定项 + validate-only + 反断言
# ------------------------------------------------------------------


def _write_global_assets(path: Path) -> None:
    """无 routes 段的最小合法 v3 global 文档（只载页名/锚点）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "_schema_ver": 3,
        "_module": "global",
        "reference_size": [1280, 720],
        "match": {"scales": [1.0], "threshold": 0.75, "margin_default": 0.01},
        "pages": {"hall": {"label": "大厅"}},
        "anchors": {
            "hall_race_btn": {
                "kind": "template", "owner": "global", "page": "hall",
                "label": "比赛按钮", "rect": [0.1, 0.1, 0.3, 0.3],
                "templates": ["hall_race_btn.png"], "order": 10,
            },
        },
        "stages": {"order": [], "global_anchors": ["hall_race_btn"]},
        # schema 结构必填：无路由内容也须显式空对象（_parse_routes 不容忍缺键）
        "routes": {},
    }
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_paths_for_global_lands_in_core():
    assets, out, image_dir = cr.paths_for("global")
    assert assets == (
        _REPO / "maaracing_assistant" / "core" / "resources"
        / "config" / "global_assets.json"
    )
    assert image_dir == CORE_IMAGE_DIR


def test_compile_one_routesless_is_validate_only(tmp_path, monkeypatch, capsys):
    """N-P0：无 routes 段必须在 check/写盘分支之前返回 0，
    否则 --check 因生成物不存在而误报失败（global 接入当天 CI 必红）。"""
    g = tmp_path / "global_assets.json"
    _write_global_assets(g)
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", g)
    monkeypatch.setattr(cr, "CORE_RES", tmp_path)
    rc = cr.compile_one("global", check=True)
    assert rc == 0
    assert "跳过" in capsys.readouterr().out
    assert not (tmp_path / "generated" / "pipeline" / "global_routes.json").exists()


def test_discover_modules_includes_fixed_global(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    cfg = plugins / "demo" / "resources" / "config"
    cfg.mkdir(parents=True)
    (cfg / "demo_assets.json").write_text("{}", encoding="utf-8")
    g = tmp_path / "global_assets.json"
    _write_global_assets(g)
    monkeypatch.setattr(cr, "PLUGINS", plugins)
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", g)
    assert cr.discover_modules() == ["demo", "global"]


def test_discover_modules_without_global_file(tmp_path, monkeypatch):
    """global 文件尚未建立的过渡期：扫描集合不含固定项、不报错（CI 不红）。"""
    plugins = tmp_path / "plugins"
    cfg = plugins / "demo" / "resources" / "config"
    cfg.mkdir(parents=True)
    (cfg / "demo_assets.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cr, "PLUGINS", plugins)
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", tmp_path / "absent.json")
    assert cr.discover_modules() == ["demo"]


def _run_main(monkeypatch, modules) -> int:
    monkeypatch.setattr(cr, "discover_modules", lambda: modules)
    monkeypatch.setattr(sys, "argv", ["compile_routes.py", "--all", "--check"])
    return cr.main()


def test_main_all_fails_when_global_file_exists_but_missing_from_scan(
    tmp_path, monkeypatch, capsys
):
    """反断言①：global 文件在而扫描集合没有它 = 固定项被误删，必须失败。"""
    g = tmp_path / "global_assets.json"
    _write_global_assets(g)
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", g)
    assert _run_main(monkeypatch, ["treasure"]) == 1
    assert "反断言失败" in capsys.readouterr().err


def test_main_all_fails_without_plugin_module(tmp_path, monkeypatch, capsys):
    """反断言②：固定项不得架空空集合保护——真实模块段缺失必须失败。"""
    monkeypatch.setattr(cr, "CORE_RES", tmp_path)
    assert _run_main(monkeypatch, ["global"]) == 1
    assert "反断言失败" in capsys.readouterr().err


# ------------------------------------------------------------------
# schedule.json 轻校验（D1/N5）
# ------------------------------------------------------------------


def _write_schedule(assets_dir: Path, window: dict) -> None:
    (assets_dir / "schedule.json").write_text(
        json.dumps({"activity_window": window}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_schedule_missing_is_optional(tmp_path):
    assert cr.check_schedule(tmp_path / "config" / "x_assets.json") == []


def test_schedule_valid_window_passes(tmp_path):
    _write_schedule(tmp_path, {"start": "2026-08-06 05:00", "end": "2026-09-03 23:04"})
    assert cr.check_schedule(tmp_path / "x_assets.json") == []


def test_schedule_bad_format_and_inverted_window(tmp_path):
    _write_schedule(tmp_path, {"start": "2026/08/06", "end": "2026-09-03 23:04"})
    errs = cr.check_schedule(tmp_path / "x_assets.json")
    assert any("不符合" in e for e in errs)

    _write_schedule(tmp_path, {"start": "2026-09-03 23:04", "end": "2026-08-06 05:00"})
    assert any("晚于" in e for e in cr.check_schedule(tmp_path / "x_assets.json"))


def test_schedule_unparsable_and_missing_fields(tmp_path):
    (tmp_path / "schedule.json").write_text("{broken", encoding="utf-8")
    assert any("不可解析" in e for e in cr.check_schedule(tmp_path / "x_assets.json"))

    _write_schedule(tmp_path, {"start": "2026-08-06 05:00"})
    errs = cr.check_schedule(tmp_path / "x_assets.json")
    assert any("end" in e for e in errs)


def test_compile_one_fails_on_bad_schedule(tmp_path, monkeypatch, capsys):
    """schedule 坏档在 check/写盘之前拦截，退出码 1。"""
    assets_dir = tmp_path / "config"
    assets_dir.mkdir()
    doc = {
        "_schema_ver": 3, "_module": "global",
        "reference_size": [1280, 720],
        "match": {"scales": [1.0], "threshold": 0.75, "margin_default": 0.01},
        "pages": {}, "stages": {"order": [], "global_anchors": []}, "routes": {},
    }
    (assets_dir / "global_assets.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )
    _write_schedule(assets_dir, {"start": "2026-09-03 23:04", "end": "2026-08-06 05:00"})
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", assets_dir / "global_assets.json")
    monkeypatch.setattr(cr, "CORE_RES", tmp_path)
    assert cr.compile_one("global", check=True) == 1
    assert "schedule" in capsys.readouterr().err
