# -*- coding: utf-8 -*-
"""模块选项卡里「可改写文案」的静态锁。

失败例：`wireSwitchAria` 无条件把标题/说明的 id 改成从开关 id 派生的名字，
按原 id 查找文案的更新函数（录制开关随驾驶模式换说法）找不到元素、静默失效——
宏档仍显示深度档的说明，且没有任何报错。

为什么是静态锁：前端没有 DOM 测试链，但「显式 id 必须保留」能从源码文本稳定判定。
它不证明运行时行为，只防止有人把 id 改写逻辑改回去。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"
SETTINGS_JS = FRONTEND / "js" / "settings.js"
INDEX_HTML = FRONTEND / "index.html"


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


def test_switch_aria_keeps_explicit_title_and_desc_ids():
    body = _js_function_body(SETTINGS_JS.read_text(encoding="utf-8"), "function wireSwitchAria")
    assert "if (!title.id)" in body
    assert "if (!desc.id)" in body


def test_record_copy_targets_the_ids_declared_in_html():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="opt-record-title"' in html
    assert 'id="opt-record-desc"' in html
    body = _js_function_body(SETTINGS_JS.read_text(encoding="utf-8"), "function applyRecordCopy")
    assert "'opt-record-title'" in body
    assert "'opt-record-desc'" in body
