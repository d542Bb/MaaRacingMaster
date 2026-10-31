# -*- coding: utf-8 -*-
"""圆角形状试点的边界锁：只在主控页卡片上试 corner-shape，不铺开全站。

2026-10-31 的裁定：``corner-shape`` 批准**试点**，全站替换等视觉验收。
所以这个文件锁的不是「效果好不好」（那要眼睛看），而是三条不能越界的边界：

1. **必须包在 ``@supports`` 里**：corner-shape 仍是实验特性，不支持的引擎要自动
   退回 ``border-radius`` 圆弧，不能出现「属性被解析但画不出」的半吊子状态。
2. **作用域只到主控页卡片**：试点期间不许出现全站选择器（``*`` / ``body`` / 裸标签），
   也不许顺手给按钮、输入框、弹窗加。
3. **选卡弹窗的描边动画不动**：那是独立的几何实现（``js/gallery-shape.js`` 采样路径 +
   ``stroke-dashoffset`` 过渡）。用 corner-shape 替代它会丢掉「从底边中点长出来」的动画。

局限：源码文本断言只能证明「写法没越界」，不证明观感。观感由验收人开一次真机看。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STYLE = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "style.css"
SHAPE_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "gallery-shape.js"

PILOT_HEADER = "圆角形状试点"


def _pilot_block(css: str) -> str:
    """取出试点 @supports 块（含首尾大括号）。抽取规则失效时直接失败，不静默通过。

    按 `@supports (corner-shape: …) {` 精确匹配，不按注释里的字样定位——说明文字
    里也会出现 `@supports`，按文本搜索会抽到注释。
    """
    m = re.search(r"@supports \(corner-shape:[^{]*\{", css)
    assert m, "找不到 corner-shape 的 @supports 守卫块（试点被拆掉了？）"
    brace = css.index("{", m.start())
    depth = 0
    for i in range(brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[m.start():i + 1]
    raise AssertionError("试点 @supports 块未闭合，抽取规则已失效")


def test_pilot_is_guarded_by_supports():
    """试点必须整块包在 @supports 里——降级靠它自动生效，不另写覆盖规则。"""
    css = STYLE.read_text(encoding="utf-8")
    block = _pilot_block(css)
    assert block.startswith("@supports (corner-shape:"), block[:80]
    assert "corner-shape: var(--mra-corner-shape)" in block


def test_corner_shape_appears_only_inside_the_pilot_block():
    """除试点块外，全文件不得出现生效的 corner-shape 声明（token 定义除外）。"""
    css = STYLE.read_text(encoding="utf-8")
    block = _pilot_block(css)
    outside = css.replace(block, "")
    # 允许：变量定义 --mra-corner-shape（它不是属性声明）
    offenders = re.findall(r"(?<!-)\bcorner-shape\s*:", outside)
    assert not offenders, f"试点块外出现 corner-shape 声明: {offenders}"


def test_pilot_scope_is_control_page_cards_only():
    """试点只作用于主控页两类卡片，不含全站选择器与其它控件。"""
    block = _pilot_block(STYLE.read_text(encoding="utf-8"))
    selectors = block.split("{")[1].strip()
    assert ".task-item" in selectors
    assert "#page-control .card" in selectors
    for forbidden in ("*", "body", ".mra-btn", ".mra-modal", "input", "textarea"):
        assert forbidden not in selectors, f"试点范围越界: {forbidden}"


def test_gallery_stroke_animation_still_drives_its_own_geometry():
    """选卡弹窗的描边仍是 dashoffset 生长 + 自算路径，没有被 corner-shape 取代。"""
    css = STYLE.read_text(encoding="utf-8")
    assert "stroke-dashoffset: 1" in css and "stroke-dashoffset: 0" in css
    assert ".task-gallery-cell--selected .task-gallery-stroke path" in css
    js = SHAPE_JS.read_text(encoding="utf-8")
    assert "clip-path" in js and "offsetInward" in js
