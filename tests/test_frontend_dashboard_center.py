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
