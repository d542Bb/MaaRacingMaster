# -*- coding: utf-8 -*-
"""数据页看板中心的前端静态锁。

锁两件事：

1. **渲染器齐备**：后端 ``BLOCK_TYPES`` 里每种 block，前端 ``dashBlockHtml`` 都有
   ``case``——新增 block 类型时只改 core、忘了前端，会在这里红。
2. **候选集口径**：看板候选取自任务列表顺序 ∩ 模块的 ``has_dashboard``，而不是
   「最后展开的卡片」；数据页只调用统一的 ``get_module_dashboard``。

为什么是静态锁：前端没有 DOM 测试链；但这两条都是可从源码文本稳定判定的契约。
它不证明运行时行为，只防止「有人又把渲染或候选集改回旧路径」。
"""
from __future__ import annotations

import re
from pathlib import Path

from maaracing_master.core.dashboard_contract import BLOCK_TYPES

REPO = Path(__file__).resolve().parent.parent
SETTINGS_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "settings.js"


def _js_function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for index in range(brace, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:index + 1]
    raise AssertionError(f"找不到 {signature} 的函数体结尾（源码结构变了？）")


def test_dashboard_block_renderers_cover_backend_block_types():
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = _js_function_body(src, "function dashBlockHtml(block) {")
    rendered = set(re.findall(r"case '([a-z_]+)':", body))
    assert rendered, "dashBlockHtml 里没有抽到任何 case，静态锁会空过"
    missing = BLOCK_TYPES - rendered
    assert not missing, f"后端可下发的 block 类型前端没有渲染器：{sorted(missing)}"


def test_candidates_come_from_task_list_and_module_declaration():
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = _js_function_body(src, "function dashboardCandidates() {")
    assert "state.queue.order" in body, "看板候选集必须按任务列表顺序取"
    assert "has_dashboard" in body, "看板候选集必须过滤模块的 has_dashboard 声明"


def test_data_page_uses_unified_dashboard_route():
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = _js_function_body(src, "async function loadDashboard() {")
    assert "'get_module_dashboard'" in body, "看板内容必须走统一的 get_module_dashboard 路由"
    assert "module_id" in body, "读看板必须点名模块，不能依赖隐式的当前模块"


def test_list_renderer_supports_parts_groups_and_paging():
    """行内着色、日期组头与分页是 list 的共享能力；缺一处前端会静默退化。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    assert "row.parts" in src
    assert "board-item" in src
    assert "board-date" in src
    assert "data-page-size" in src
    assert "bindDashboardLists" in src
    body = _js_function_body(src, "async function loadDashboard() {")
    assert "bindDashboardLists(body)" in body


def test_single_dashboard_hides_pager_and_shows_module_chip():
    """单看板没有可翻的页：翻页器隐藏，模块身份由卡头胶囊承载。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    card = _js_function_body(src, "function dashboardCenterCard() {")
    assert "dash-module-chip" in card
    chrome = _js_function_body(src, "function renderDashboardChrome(cands) {")
    assert "pager.hidden = count <= 1" in chrome
    assert "mod.name" in chrome


def test_dashboard_caption_comes_from_subtitle_or_today_stats_bucket():
    """日期说明：新契约读 subtitle，旧兼容块读 today_stats 的 bucket。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = _js_function_body(src, "function dashboardPayloadHtml(payload) {")
    assert "payload.subtitle" in body
    assert "today_stats" in body
    assert "bucket" in body
    assert "05:00 起" in body


def test_list_rows_keep_fixed_badge_and_value_columns():
    """状态与结果各占固定列，换行时仍对齐。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = _js_function_body(src, "function dashRowHtml(row) {")
    assert "dash-row-badge" in body
    assert "dash-row-value" in body
