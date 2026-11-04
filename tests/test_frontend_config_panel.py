# -*- coding: utf-8 -*-
"""配置面板通用渲染器的静态锁。

锁四件事：
1. 壳里不再留模块专属的静态面板块与摘要函数（插件自述、宿主渲染）；
2. 契约里每种控件类型都有对应的渲染分支（新增类型忘了前端会红）；
3. 渲染器只用 textContent，且带 _rev 守卫（旧轮询响应不得覆盖新状态）；
4. wireSwitchAria 保留显式标题/说明 id（文案更新函数要靠它找人）。

为什么是静态锁：前端没有 DOM 测试链；这些契约可从源码文本稳定判定。时序纯逻辑另由
tests/test_frontend_config_panel_logic.py 跑 node --test 覆盖。
"""
from __future__ import annotations

from pathlib import Path

from maaracing_master.core.config_ui_contract import FIELD_TYPES

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"
HTML = FRONTEND / "index.html"
SETTINGS = FRONTEND / "js" / "settings.js"
RUN = FRONTEND / "js" / "run.js"
PANEL = FRONTEND / "js" / "config-panel.js"
STATE = FRONTEND / "js" / "config-panel-state.js"

MODULE_TOKENS = (
    "drive_mode", "record_mode", "macro_group", "macro_mode", "straight_mode",
    "max_daily_loops", "target_session",
)


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


def test_static_module_panels_are_gone():
    html = HTML.read_text(encoding="utf-8")
    assert 'id="module-options"' not in html
    assert "opt-record-mode" not in html
    assert "opt-drive-mode" not in html
    assert 'src="js/config-panel-state.js"' in html
    assert 'src="js/config-panel.js"' in html
    assert html.index("js/config-panel.js") < html.index('src="js/run.js"')


def test_every_contract_field_type_has_a_renderer():
    src = PANEL.read_text(encoding="utf-8")
    for field_type in FIELD_TYPES:
        assert f"case '{field_type}':" in src, f"缺少 {field_type} 的渲染分支"


def test_host_files_carry_no_module_field_names_or_summary():
    for src in (SETTINGS.read_text(encoding="utf-8"), RUN.read_text(encoding="utf-8")):
        for token in MODULE_TOKENS:
            assert token not in src, f"宿主仍硬编码模块字段 {token}"
    assert "summary: (cfg)" not in RUN.read_text(encoding="utf-8")
    assert "_summary" in RUN.read_text(encoding="utf-8")


def test_summary_chips_render_without_expanding_card():
    """任务卡摘要 chips 与展开态无关：列表渲染时就取配置。

    chips 的数据源是 get_module_config 的 `_summary`；若只在展开时拉取，收起卡片的
    标题一直空着，点开才补上。
    """
    body = _js_function_body(RUN.read_text(encoding="utf-8"), "function renderTaskList")
    assert "!isBuiltin(mid) && state.expanded[mid]" not in body, \
        "活动模块取配置又被展开态挡住了：收起卡片的摘要 chips 会缺席"
    assert "!state.cfg[mid] || state.expanded[mid]" in body
    assert "ensureConfig(mid)" in body


def test_panel_renders_text_only_and_guards_revisions():
    src = PANEL.read_text(encoding="utf-8")
    assert "innerHTML" not in src
    assert "textContent" in src
    assert "acceptResponse" in src
    assert "_ui_note" in src
    state_src = STATE.read_text(encoding="utf-8")
    for token in ("lastRev", "acceptResponse", "isAlive", "pending"):
        assert token in state_src


def test_switch_aria_keeps_explicit_title_and_desc_ids():
    body = _js_function_body(SETTINGS.read_text(encoding="utf-8"), "function wireSwitchAria")
    assert "if (!title.id)" in body
    assert "if (!desc.id)" in body
