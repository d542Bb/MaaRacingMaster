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


def strip_ext(name: str) -> str:
    """模板名规范化：剥掉自带扩展名（v3/v4 资产形态），裸名原样返回。"""
    return name[:-4] if name.lower().endswith((".png", ".jpg", ".jpeg")) else name


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


# ==================== v4 引擎件（P2a-Q5）：colorspace / 仲裁 / 断言 / 遮挡 ====================

CURSOR_SIZE_NORM = 0.03  # 光标贴图归一化宽（P2b 真机校准前的引擎常量）


def _match_colorspace(frame: np.ndarray, tpl: np.ndarray,
                      colorspace: str) -> tuple[np.ndarray, np.ndarray]:
    """按 colorspace 预处理匹配双方。gray=亮度单通道；rgb_strict 由调用方分通道。"""
    if colorspace == "gray":
        g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        t = cv2.cvtColor(tpl, cv2.COLOR_RGB2GRAY)
        return g, t
    return frame, tpl  # rgb / rgb_strict 用彩色


def match_template_cs(frame: np.ndarray, tpl: np.ndarray, *, colorspace: str = "rgb",
                      threshold: float = 0.7, scales=DEFAULT_SCALES,
                      roi: Box | None = None) -> tuple[Box | None, float]:
    """带色彩空间的单模板匹配。

    - rgb：三通道联合匹配（默认，与 find_template 同源，多尺度）；
    - gray：双方转亮度单通道（MAA 生态互操作形态，多尺度）；
    - rgb_strict：R/G/B 三通道独立 NCC 取**最低分**——"结构对但颜色错"零容忍。
      负分（反相）必须参与 min，故不走 find_template（其内部 0 下限会吞负分）；
      尺度固定 1.0（strict 场景为固定 UI 的激活态/稀有度判定），多尺度 strict 归 P2b。
    """
    if colorspace == "rgb_strict":
        search, ox, oy = frame, 0, 0
        if roi is not None:
            rx, ry, rw, rh = roi
            search = frame[ry:ry + rh, rx:rx + rw]
            ox, oy = rx, ry
        # 逐像素 min：同一位置三通道 NCC 都要过（"每通道各自 max 的 min"会放行
        # 各通道在不同位置命中的假阳性）。负分（反相）天然参与 min。
        res_min = None
        for c in range(3):
            res_c = cv2.matchTemplate(search[:, :, c].astype(np.float32),
                                      tpl[:, :, c].astype(np.float32),
                                      cv2.TM_CCOEFF_NORMED)
            res_min = res_c if res_min is None else np.minimum(res_min, res_c)
        _mn, mv, _mnl, ml = cv2.minMaxLoc(res_min)
        if mv < threshold or ml is None:
            return None, float(mv)
        th, tw = tpl.shape[:2]
        return (ml[0] + ox, ml[1] + oy, ml[0] + ox + tw, ml[1] + oy + th), float(mv)
    f, t = _match_colorspace(frame, tpl, colorspace)
    if f.ndim == 2:
        f3 = np.stack([f] * 3, axis=-1)
        t3 = np.stack([t] * 3, axis=-1)
        return find_template(f3, t3, threshold=threshold, scales=scales, roi=roi)
    return find_template(f, t, threshold=threshold, scales=scales, roi=roi)


def find_any_cs(frame: np.ndarray, names: list[str], image_dirs: list[Path], *,
                colorspace: str = "rgb", threshold: float = 0.7,
                thresholds: dict[str, float] | None = None,
                scales=DEFAULT_SCALES, roi: Box | None = None) -> tuple[Box | None, float, str]:
    """v4 多候选匹配：逐模板独立阈值（arbitration.template_thresholds）+ colorspace。"""
    thresholds = thresholds or {}
    best_box, best_val, best_name = None, 0.0, ""
    for name in names:
        tpl = load_template(name, image_dirs)
        if tpl is None:
            continue
        box, val = match_template_cs(frame, tpl, colorspace=colorspace,
                                     threshold=float(thresholds.get(name, threshold)),
                                     scales=scales, roi=roi)
        if box is not None and val > best_val:
            best_box, best_val, best_name = box, val, name
    return best_box, best_val, best_name


def color_assert_ok(frame: np.ndarray, box: Box, sub_rect_norm: list[float],
                    hue_range: list[float]) -> bool:
    """色相断言（L1 闸门）：命中框内子区域均值 H ∈ [lo, hi]（HSV，OpenCV H∈[0,180]）。

    sub_rect_norm 以命中框为基准的归一化子矩形 [x0,y0,x1,y1]（v3 color_assert.rect 语义）。
    """
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    sx1 = int(x1 + sub_rect_norm[0] * w)
    sy1 = int(y1 + sub_rect_norm[1] * h)
    sx2 = int(x1 + sub_rect_norm[2] * w)
    sy2 = int(y1 + sub_rect_norm[3] * h)
    region = frame[max(sy1, 0):sy2, max(sx1, 0):sx2]
    if region.size == 0:
        return False
    hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
    mean_h = float(hsv[:, :, 0].mean())
    lo, hi = float(hue_range[0]), float(hue_range[1])
    return lo <= mean_h <= hi if lo <= hi else (mean_h >= lo or mean_h <= hi)


def occlusion_ratio(box: Box, cursor_box: Box | None) -> float:
    """命中框被光标矩形覆盖的面积占比（L1 遮挡过滤判据；无光标返回 0）。"""
    if cursor_box is None:
        return 0.0
    ix1, iy1 = max(box[0], cursor_box[0]), max(box[1], cursor_box[1])
    ix2, iy2 = min(box[2], cursor_box[2]), min(box[3], cursor_box[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    area_box = (box[2] - box[0]) * (box[3] - box[1])
    return ((ix2 - ix1) * (iy2 - iy1)) / area_box if area_box > 0 else 0.0


def cursor_box_norm(cx: float, cy: float, size_norm: float = CURSOR_SIZE_NORM,
                    frame_w: int = 1, frame_h: int = 1) -> Box:
    """归一化光标中心 → 像素矩形（遮挡过滤用）。"""
    w, h = size_norm * frame_w, size_norm * frame_h
    return (int(cx * frame_w - w / 2), int(cy * frame_h - h / 2),
            int(cx * frame_w + w / 2), int(cy * frame_h + h / 2))
