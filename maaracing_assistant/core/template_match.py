#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跳转图专用的模板匹配（唯一一份实现）。

为什么单独有一份：模板匹配此前在 racing（Navigation._find_template）和鉴宝
（detector._match_local）各写了一套，再接新模块就是第三套。跳转图（pipeline）
只认这一份，新模块不再自己写匹配。

算法沿用 racing 已验证的多尺度 TM_CCOEFF_NORMED（窗口尺寸变化时靠 scale 命中）。

模板按 image_dirs 顺序搜索，**先命中先用**：把覆盖图放在靠前的目录里，
换一张图就完成一次热修，不用改代码。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from maaracing_assistant.core.logger import logger

# 与 racing 导航引擎一致的默认尺度表（跨窗口分辨率/全屏-窗口切换）
DEFAULT_SCALES = (0.5, 0.7, 0.9, 1.0, 1.2, 1.5, 1.8)

# 模板缓存：键含目录版本（同一目录树 mtime 不变即复用），避免每帧读盘
_cache: dict[str, np.ndarray | None] = {}

Box = tuple[int, int, int, int]  # (x1, y1, x2, y2) 像素


def load_template(name: str, image_dirs: list[Path]) -> np.ndarray | None:
    """按目录顺序加载模板（.png / .jpg），返回 RGB ndarray；找不到返回 None。

    name 兼容两种形态：裸名（自动拼 .png/.jpg/.jpeg）与自带扩展名
    （v3/v4 资产模板字段形态，直接按原名查找，不再二次拼接）。
    """
    if name in _cache:
        return _cache[name]
    has_ext = name.lower().endswith((".png", ".jpg", ".jpeg"))
    img = None
    for d in image_dirs:
        if has_ext:
            path = Path(d) / name
            if path.exists():
                raw = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if raw is not None:
                    img = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
                break
            continue
        for ext in (".png", ".jpg", ".jpeg"):
            path = Path(d) / f"{name}{ext}"
            if not path.exists():
                continue
            raw = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if raw is not None:
                img = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
            break
        if img is not None:
            break
    if img is None:
        logger.log(f"模板不存在: {name}.png/.jpg（搜索 {len(image_dirs)} 个目录）", "WARNING")
    _cache[name] = img
    return img


def find_template(frame: np.ndarray, template: np.ndarray, threshold: float = 0.7,
                  scales=DEFAULT_SCALES, roi: Box | None = None) -> tuple[Box | None, float]:
    """在 frame 里多尺度匹配 template。

    roi：(x, y, w, h) 限定搜索区，缺省全图。
    返回 (命中框 x1y1x2y2, 置信度)；未命中返回 (None, 最高分)。
    """
    search = frame
    ox = oy = 0
    if roi is not None:
        rx, ry, rw, rh = roi
        search = frame[ry:ry + rh, rx:rx + rw]
        ox, oy = rx, ry

    th, tw = template.shape[0], template.shape[1]
    best_val, best_box, best_scale = 0.0, None, 1.0
    for scale in scales:
        resized = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        if resized.shape[0] > search.shape[0] or resized.shape[1] > search.shape[1]:
            continue
        result = cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best_val:
            w, h = int(tw * scale), int(th * scale)
            x1, y1 = max_loc[0] + ox, max_loc[1] + oy
            best_val, best_box, best_scale = max_val, (x1, y1, x1 + w, y1 + h), scale

    if best_val < threshold or best_box is None:
        logger.log(f"模板未命中: 最高分={best_val:.3f} < {threshold:.2f}", "DEBUG")
        return None, best_val
    logger.log(f"模板命中: {best_box} 置信度={best_val:.3f} scale={best_scale:.2f}", "DEBUG")
    return best_box, best_val


def find_any(frame: np.ndarray, names: list[str], image_dirs: list[Path],
             threshold: float = 0.7, scales=DEFAULT_SCALES,
             roi: Box | None = None) -> tuple[Box | None, float, str]:
    """一张节点挂多张候选图（新旧皮肤同时有效），取最高分。

    返回 (命中框, 置信度, 命中的模板名)。
    """
    best_box, best_val, best_name = None, 0.0, ""
    for name in names:
        tpl = load_template(name, image_dirs)
        if tpl is None:
            continue
        box, val = find_template(frame, tpl, threshold=threshold, scales=scales, roi=roi)
        if box is not None and val > best_val:
            best_box, best_val, best_name = box, val, name
    return best_box, best_val, best_name
