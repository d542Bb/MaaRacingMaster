# -*- coding: utf-8 -*-
"""数据看板内容契约：版本、block 白名单、字段规范化与兼容块。

纯函数测试，不依赖 sidecar / 模块运行时；路由与兼容桥见
``tests/test_sidecar_module_dashboard.py``。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.dashboard_contract import (
    DASHBOARD_CONTRACT_VERSION,
    DashboardContractError,
    normalize_dashboard,
)


def _payload(*blocks):
    return {
        "version": DASHBOARD_CONTRACT_VERSION,
        "title": "今日看板",
        "subtitle": "2026-11-02 05:00 起",
        "badge": {"text": "数据型看板", "tone": "neutral"},
        "blocks": list(blocks),
    }


def test_valid_blocks_are_normalized():
    payload = _payload(
        {"type": "stats", "items": [
            {"label": "银币盈亏", "value": "+128,400", "tone": "positive"},
        ]},
        {"type": "list", "rows": [
            {"leading": "#23", "badge": {"text": "拍中", "tone": "positive"},
             "text": "收 52,000", "trailing": "利 18,400"},
        ]},
        {"type": "text", "text": "说明"},
        {"type": "path", "text": "debug/speedrush/manual/"},
        {"type": "action", "label": "打开数据目录", "action": "open_user_data_folder"},
    )
    normalized, warnings = normalize_dashboard(payload)
    assert warnings == ()
    assert normalized["version"] == DASHBOARD_CONTRACT_VERSION
    assert [block["type"] for block in normalized["blocks"]] == [
        "stats", "list", "text", "path", "action",
    ]
    assert normalized["badge"]["tone"] == "neutral"
    assert normalized["blocks"][0]["items"][0]["tone"] == "positive"


def test_unknown_block_type_is_dropped_with_warning():
    normalized, warnings = normalize_dashboard(_payload(
        {"type": "script", "html": "<b>x</b>"},
        {"type": "text", "text": "保留"},
    ))
    assert [block["type"] for block in normalized["blocks"]] == ["text"]
    assert any("未知类型" in warning for warning in warnings)


def test_unknown_action_is_dropped():
    normalized, warnings = normalize_dashboard(_payload(
        {"type": "action", "label": "执行", "action": "run_shell"},
    ))
    assert normalized["blocks"] == []
    assert any("白名单" in warning for warning in warnings)


def test_unsupported_version_raises():
    with pytest.raises(DashboardContractError):
        normalize_dashboard({"version": DASHBOARD_CONTRACT_VERSION + 1, "blocks": []})


def test_bad_top_level_shape_raises():
    with pytest.raises(DashboardContractError):
        normalize_dashboard([])
    with pytest.raises(DashboardContractError):
        normalize_dashboard({"version": DASHBOARD_CONTRACT_VERSION, "blocks": "nope"})


def test_text_is_truncated_and_control_chars_rejected():
    normalized, warnings = normalize_dashboard(_payload(
        {"type": "text", "text": "甲" * 5000},
        {"type": "text", "text": "含\x00控制字符"},
    ))
    assert len(normalized["blocks"]) == 1
    assert len(normalized["blocks"][0]["text"]) == 2000
    assert any("截断" in warning for warning in warnings)
    assert any("控制字符" in warning for warning in warnings)


def test_today_stats_compat_block_passes_data_through():
    data = {"bucket": "2026-11-02", "summary": None, "games": []}
    normalized, warnings = normalize_dashboard(_payload({"type": "today_stats", "data": data}))
    assert warnings == ()
    assert normalized["blocks"][0] == {"type": "today_stats", "data": data}


def test_invalid_tones_fall_back_to_neutral():
    normalized, warnings = normalize_dashboard(_payload(
        {"type": "stats", "items": [
            {"label": "场次", "value": "23", "tone": "loud"},
        ]},
        {"type": "list", "rows": [
            {"text": "收 1", "badge": {"text": "未知", "tone": "loud"}},
        ]},
    ))
    assert normalized["blocks"][0]["items"][0]["tone"] == "neutral"
    assert normalized["blocks"][1]["rows"][0]["badge"]["tone"] == "neutral"
    assert any("非法" in warning for warning in warnings)
