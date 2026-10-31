# -*- coding: utf-8 -*-
"""圆角形状（corner-shape）的边界锁：只作用于「面」类，且半径必须一起补。

2026-10-31 的裁定：``corner-shape`` 批准**试点**，全站替换等视觉验收。
第一版试点踩了两个坑，这个文件把两条都锁上：

1. **必须包在 ``@supports`` 里**：corner-shape 仍是实验特性，不支持的引擎要自动
   退回 ``border-radius`` 圆弧，不能出现「属性被解析但画不出」的半吊子状态。
2. **半径必须跟着补**：超椭圆的切角比圆弧浅（0.292×R vs 0.414×R），只换形状不换半径
   会让圆角看起来几乎成了方角——第一版就是这个毛病。
3. **作用域只到「面」类**：卡片 / 任务卡 / 日志卡 / 弹窗。它们同屏出现，只改一处
   必然「各有各的样子」；但也不许扩到按钮、输入框或全站选择器。
4. **选卡弹窗的描边动画不动**：那是独立的几何实现（``js/gallery-shape.js`` 采样路径 +
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


def test_pilot_scope_is_surface_classes_only():
    """作用域只到「面」类（页面卡片/关于页头卡/任务卡/日志卡/弹窗），不含全站选择器与控件。"""
    block = _pilot_block(STYLE.read_text(encoding="utf-8"))
    body = block[block.index("{") + 1:-1]   # 守卫块内部（可能有多条规则）
    for want in (".card", ".about-hero", ".task-item", ".log-section", ".mra-modal-card"):
        assert want in body, f"面类 {want} 未纳入，同屏会不统一"
    for forbidden in ("*", "body", ".mra-btn", "input", "textarea", "a "):
        assert forbidden not in body, f"作用域越界: {forbidden}"


def test_radius_is_compensated_inside_the_guard():
    """半径必须按「等效圆」补：非圆弧的拐角会贴边收，肉眼圆润度远小于 border-radius。

    实测（同一半径，按拐角缺口面积折算等效圆半径）：round 1.00 / squircle 0.69 /
    superellipse(2.5) 0.54 / superellipse(3) 0.43。
    前两版就是漏了这条：superellipse(3) + 12px 只相当于 5.2px 圆弧，比原 8px 还小。
    """
    block = _pilot_block(STYLE.read_text(encoding="utf-8"))
    assert "corner-shape: squircle" in block, "应用 squircle（非圆弧里最圆的一档）"
    assert "border-radius: 16px" in block, "卡片面没把半径补到 16（等效约 11px）"
    assert "border-radius: 20px" in block, "弹窗没把半径补到 20（等效约 14px）"


def test_gallery_stroke_animation_still_drives_its_own_geometry():
    """选卡弹窗的描边仍是 dashoffset 生长 + 自算路径，没有被 corner-shape 取代。"""
    css = STYLE.read_text(encoding="utf-8")
    assert "stroke-dashoffset: 1" in css and "stroke-dashoffset: 0" in css
    assert ".task-gallery-cell--selected .task-gallery-stroke path" in css
    js = SHAPE_JS.read_text(encoding="utf-8")
    assert "clip-path" in js and "offsetInward" in js
