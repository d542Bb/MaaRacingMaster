# -*- coding: utf-8 -*-
"""归档插件（treasure）的配置表单声明锁。

鉴宝已归档、不注册，但它的面板必须跟目录走：声明留在 `_archive/treasure/module.py`，
壳里不再有任何鉴宝专属字段。本测试直接导入归档类校验声明，防「归档目录里的 schema
腐烂」——它不注册，常规 sidecar 路径照不到。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maaracing_master.core.config_ui_contract import UI_OK, normalize_config_ui

try:
    from maaracing_master.plugins._archive.treasure.module import TreasureModule
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 归档模块依赖重，缺依赖时跳过
    _OK, _ERR = False, str(exc)

REPO = Path(__file__).resolve().parents[5]
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"

pytestmark = [
    pytest.mark.retired,
    pytest.mark.skipif(not _OK, reason=f"归档模块不可导入：{_ERR}"),
]


def test_archived_declaration_is_valid():
    res = normalize_config_ui(
        TreasureModule, set(TreasureModule.DEFAULT_MODULE_CONFIG))
    assert res.state == UI_OK and res.warnings == ()
    fields = res.schema["groups"][0]["fields"]
    assert [f["key"] for f in fields] == ["max_daily_loops", "target_session"]
    number, segmented = fields
    assert (number["min"], number["max"], number["suffix"]) == (0, 50, "场")
    assert [o["value"] for o in segmented["options"]] == ["intern", "expert", "master"]


def test_archived_summary_matches_old_copy():
    assert TreasureModule.config_summary(
        {"max_daily_loops": 30, "target_session": "expert"}) == [
        "刷到第 30 场", "目标场次: 专家场"]
    assert TreasureModule.config_summary({}) == ["刷到第 50 场", "目标场次: 大师场"]


def test_shell_keeps_no_treasure_fields():
    for name in ("js/settings.js", "js/run.js", "index.html"):
        src = (FRONTEND / name).read_text(encoding="utf-8")
        for token in ("max_daily_loops", "target_session"):
            assert token not in src, f"{name} 仍硬编码鉴宝字段 {token}"
