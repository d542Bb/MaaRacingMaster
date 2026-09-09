# -*- coding: utf-8 -*-
"""P2a-Q5：MRA_Template v4 引擎件合成帧契约（colorspace/仲裁/断言/遮挡/桥编排）。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from maaracing_assistant.core import template_match as tm
from maaracing_assistant.core.nav_graph import TemplateRecognizer

W, H = 1280, 720


def _patch(frame: np.ndarray, x: int, y: int, tile: np.ndarray) -> None:
    frame[y:y + tile.shape[0], x:x + tile.shape[1]] = tile


def _checker(color_a, color_b, cell=8, rows=3, cols=6) -> np.ndarray:
    t = np.zeros((rows * cell, cols * cell, 3), dtype=np.uint8)
    for r in range(rows):
        for c in range(cols):
            t[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = color_a if (r + c) % 2 == 0 else color_b
    return t


RED_A, RED_B = (200, 40, 40), (120, 20, 20)      # 红系棋盘（RGB）
CYAN_A, CYAN_B = (40, 200, 200), (20, 120, 120)  # 同构青系棋盘（R 通道亮暗模式反相）
TPL = _checker(RED_A, RED_B)
TPL_CYAN = _checker(CYAN_A, CYAN_B)


@pytest.fixture()
def tpl_dir(tmp_path):
    cv2.imwrite(str(tmp_path / "red.png"), cv2.cvtColor(TPL, cv2.COLOR_RGB2BGR))
    return tmp_path


def _frame_with(tile: np.ndarray) -> np.ndarray:
    f = np.zeros((H, W, 3), dtype=np.uint8)
    _patch(f, 600, 300, tile)
    return f


# ==================== 引擎件 ====================

def test_colorspace_gray_and_rgb_hit(tpl_dir):
    frame = _frame_with(TPL)
    for cs in ("rgb", "gray"):
        box, score = tm.match_template_cs(frame, TPL, colorspace=cs, threshold=0.8)
        assert box is not None, cs


def test_rgb_strict_rejects_same_structure_wrong_color(tpl_dir):
    """rgb_strict 方向性契约：对颜色错图案，分通道最低分必须比联合匹配更严。

    （联合 NCC 为能量加权合成，青帧 R 通道反相但幅值小 → 联合分被拉低；
    strict 直接取含负值的分通道 min，必然 ≤ 联合分——颜色容差更小。）
    """
    frame = _frame_with(_checker(CYAN_B, CYAN_A))  # 反相青：亮暗互换 → R 通道 NCC 反相
    _rgb_box, rgb_score = tm.match_template_cs(frame, TPL, colorspace="rgb", threshold=0.0)
    strict_box, strict_score = tm.match_template_cs(frame, TPL, colorspace="rgb_strict",
                                                    threshold=0.0)
    # 真命中帧（同构红棋盘）的 strict 分恒为 +1.0——反相帧必须明显更低
    true_frame = _frame_with(TPL)
    _, true_score = tm.match_template_cs(true_frame, TPL, colorspace="rgb_strict", threshold=0.0)
    assert true_score == pytest.approx(1.0)
    assert strict_score < true_score  # 颜色错在 strict 下达不到真命中分
    assert strict_score < 0.8 or strict_box is None


def test_rgb_strict_accepts_true_positive(tpl_dir):
    frame = _frame_with(TPL)
    box, _ = tm.match_template_cs(frame, TPL, colorspace="rgb_strict", threshold=0.8)
    assert box is not None


def test_per_template_thresholds(tpl_dir):
    """仲裁：逐模板独立阈值（低阈值模板带噪声也能过，高阈值模板被拒）。"""
    frame = _frame_with(TPL)
    noisy = frame.copy()
    noise = np.random.default_rng(7).integers(-25, 26, noisy.shape, dtype=np.int16)
    noisy = np.clip(noisy.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    strict = tm.find_any_cs(noisy, ["red"], [tpl_dir], colorspace="rgb", threshold=0.97)
    assert strict[0] is None
    relaxed = tm.find_any_cs(noisy, ["red"], [tpl_dir], colorspace="rgb",
                             thresholds={"red": 0.6})
    assert relaxed[0] is not None


def test_color_assert_and_occlusion(tpl_dir):
    frame = _frame_with(TPL)
    box = (600, 300, 648, 324)
    assert tm.color_assert_ok(frame, box, [0.3, 0.3, 0.7, 0.7], [0, 15])   # 红 hue≈0
    assert not tm.color_assert_ok(_frame_with(TPL_CYAN),
                                  box, [0.3, 0.3, 0.7, 0.7], [0, 15])     # 青 hue≈90
    assert tm.occlusion_ratio(box, (610, 310, 630, 320)) > 0.01            # 光标压命中框
    assert tm.occlusion_ratio(box, (100, 100, 110, 110)) == 0.0            # 光标在别处
    assert tm.occlusion_ratio(box, None) == 0.0


# ==================== 桥编排（TemplateRecognizer v4 参数面） ====================

def _bridge(tpl_dir, cursor=None):
    graph = MagicMock()
    graph.image_dirs = [tpl_dir]
    graph.frame = MagicMock(return_value=None)
    return TemplateRecognizer(graph, cursor_pos_provider=cursor)


def _argv(frame_rgb, param):
    a = MagicMock()
    a.image = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)  # 桥吃 BGR 注入帧
    a.custom_recognition_param = json.dumps(param)
    a.node_name = "t"
    return a


def test_bridge_hit_and_point_target(tpl_dir):
    br = _bridge(tpl_dir)
    res = br.analyze(None, _argv(_frame_with(TPL), {
        "mode": "template", "rect": [0.3, 0.3, 0.7, 0.6], "templates": ["red.png"],
        "threshold": 0.8}))
    assert res.box is not None
    # point：无 templates，识别 = guard 模板；detail 带 target（rect 中心）
    res_p = br.analyze(None, _argv(_frame_with(TPL), {
        "mode": "point", "rect": [0.7, 0.75, 0.8, 0.85],
        "guard": {"templates": ["red.png"], "rect": [0.3, 0.3, 0.7, 0.6], "threshold": 0.8}}))
    assert res_p.box is not None


def test_bridge_guard_fuse_blocks(tpl_dir):
    """保险丝：主模板命中但守卫模板不在画面 → 拒绝。"""
    br = _bridge(tpl_dir)
    empty = np.zeros((H, W, 3), dtype=np.uint8)
    _patch(empty, 100, 100, TPL)  # 主模板在画面左上（rect 外），guard 区(0.3,0.3 起)无守卫
    res = br.analyze(None, _argv(empty, {
        "mode": "template", "rect": [0.0, 0.0, 0.3, 0.3], "templates": ["red.png"],
        "threshold": 0.8,
        "guard": {"templates": ["red.png"], "rect": [0.3, 0.3, 0.7, 0.6], "threshold": 0.8}}))
    assert res.box is None and res.detail.get("blocked_by") == "guard"


def test_bridge_color_assert_and_cursor_blocks(tpl_dir):
    red_frame = _frame_with(TPL)
    br = _bridge(tpl_dir)
    # color_assert 拦截：命中成立但声明的 hue 区间与实际（红 hue≈0）不符
    res = br.analyze(None, _argv(red_frame, {
        "mode": "template", "rect": [0.3, 0.3, 0.7, 0.6], "templates": ["red.png"],
        "threshold": 0.6, "color_assert": {"rect": [0.3, 0.3, 0.7, 0.7], "hue": [100, 140]}}))
    assert res.box is None and res.detail.get("blocked_by") == "color_assert"

    br2 = _bridge(tpl_dir, cursor=lambda: (0.505, 0.435))  # 光标压命中框中心
    res2 = br2.analyze(None, _argv(red_frame, {
        "mode": "template", "rect": [0.3, 0.3, 0.7, 0.6], "templates": ["red.png"],
        "threshold": 0.8, "mask_cursor": True, "max_occlusion": 0.1}))
    assert res2.box is None and res2.detail.get("blocked_by") == "cursor"
