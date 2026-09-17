#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跳转图专用的模板匹配（全仓模板匹配唯一实现）。

运行时所有模板匹配（图节点 MaaRM_Template、鉴宝阶段检测器、module 私有感知
装载器）都收口在这一份实现，新模块禁止再自带匹配。

算法沿用 racing 已验证的多尺度 TM_CCOEFF_NORMED（窗口尺寸变化时靠 scale 命中）。

模板按 image_dirs 顺序搜索，**先命中先用**：把覆盖图放在靠前的目录里，
换一张图就完成一次热修，不用改代码。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.image_io import read_rgb
from maaracing_master.core.logger import logger

# 与 racing 导航引擎一致的默认尺度表（跨窗口分辨率/全屏-窗口切换）
DEFAULT_SCALES = (0.5, 0.7, 0.9, 1.0, 1.2, 1.5, 1.8)

# 模板缓存（R4 热修语义，P4c 起为全引擎唯一读盘口）：键 = 模板名，值 =
# (解析到的文件路径指纹, RGB ndarray|None)。控制台换图后同路径 mtime/size 变化
# 即自动重读——「改了没用」不允许发生；不存在/读取失败也缓存当前指纹，
# 文件后来出现时指纹变化会重读。
_cache: dict[str, tuple[tuple[int, int], np.ndarray | None]] = {}

Box = tuple[int, int, int, int]  # (x1, y1, x2, y2) 像素


def strip_ext(name: str) -> str:
    """模板名规范化：剥掉自带扩展名（真源两种带/不带扩展名形态并存），裸名原样返回。"""
    return name[:-4] if name.lower().endswith((".png", ".jpg", ".jpeg")) else name


def _fingerprint(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (-1, -1)


def _resolve_template_path(name: str, image_dirs: list[Path]) -> Path | None:
    """按目录顺序解析模板文件路径（先命中先用——覆盖图放靠前目录即热修）。

    name 兼容两种形态：裸名（自动拼 .png/.jpg/.jpeg）与自带扩展名
    （v4 资产模板字段形态，直接按原名查找，不再二次拼接）。
    """
    has_ext = name.lower().endswith((".png", ".jpg", ".jpeg"))
    for d in image_dirs:
        if has_ext:
            path = Path(d) / name
            if path.exists():
                return path
            continue
        for ext in (".png", ".jpg", ".jpeg"):
            path = Path(d) / f"{name}{ext}"
            if path.exists():
                return path
    return None


def load_template(name: str, image_dirs: list[Path]) -> np.ndarray | None:
    """加载模板，返回 RGB ndarray；找不到/读失败返回 None。带 mtime 指纹热修缓存。

    读盘走 ``image_io.read_rgb``（通道边界的唯一实现）：磁盘是标准图像语义，
    模板与运行帧因而同处 RGB 空间，匹配内核不必关心通道。
    """
    path = _resolve_template_path(name, image_dirs)
    fp = _fingerprint(path) if path is not None else (-1, -1)
    cached = _cache.get(name)
    if cached is not None and cached[0] == fp:
        return cached[1]
    img = read_rgb(path) if path is not None else None
    if img is None:
        logger.log(f"模板不存在: {name}.png/.jpg（搜索 {len(image_dirs)} 个目录）", "WARNING")
    _cache[name] = (fp, img)
    return img


def _best_match(frame: np.ndarray, template: np.ndarray, scales,
                roi: Box | None) -> tuple[Box | None, float]:
    """多尺度匹配内核（唯一一份）：返回 (最优框 x1y1x2y2, 置信度)，不放日志。

    roi：(x, y, w, h) 限定搜索区，缺省全图；返回框为全图坐标（含 roi 偏移）。
    尺度全部放不下搜索区时返回 (None, 0.0)。
    """
    search = frame
    ox = oy = 0
    if roi is not None:
        rx, ry, rw, rh = roi
        search = frame[ry:ry + rh, rx:rx + rw]
        ox, oy = rx, ry

    th, tw = template.shape[0], template.shape[1]
    best_val, best_box = 0.0, None
    for scale in scales:
        # P4c 口径统一：整数尺寸（min 4px）+ 缩小时 AREA（避免锯齿）/放大时 CUBIC
        # ——与原 detector/调试台的缩放实现一致，历史校准阈值可原样迁移。
        nw = max(4, int(round(tw * scale)))
        nh = max(4, int(round(th * scale)))
        if nh > search.shape[0] or nw > search.shape[1]:
            continue
        resized = template if (nw == tw and nh == th) else cv2.resize(
            template, (nw, nh),
            interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC)
        try:
            _, max_val, _, max_loc = cv2.minMaxLoc(
                cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED))
        except cv2.error:
            continue
        if max_val > best_val:
            x1, y1 = max_loc[0] + ox, max_loc[1] + oy
            best_val, best_box = max_val, (x1, y1, x1 + nw, y1 + nh)
    return best_box, float(best_val)


def best_match_score(frame: np.ndarray, template: np.ndarray, scales=DEFAULT_SCALES,
                     roi: Box | None = None) -> tuple[Box | None, float]:
    """取分入口（P4c）：调用方自己做阈值/领先仲裁时用——不产生逐模板日志。

    detector 每帧 11 锚点 × 多模板：若逐模板走 find_template 的命中/未命中
    DEBUG 日志，几十条/帧会冲爆日志环形缓冲、淹没关键 INFO。
    返回 (最优框|None, 最高分)；与 find_template 同一内核、同一缩放口径。
    """
    return _best_match(frame, template, scales, roi)


def find_template(frame: np.ndarray, template: np.ndarray, threshold: float = 0.7,
                  scales=DEFAULT_SCALES, roi: Box | None = None,
                  log_miss: bool = True) -> tuple[Box | None, float]:
    """在 frame 里多尺度匹配 template。

    roi：(x, y, w, h) 限定搜索区，缺省全图。
    返回 (命中框 x1y1x2y2, 置信度)；未命中返回 (None, 最高分)。

    log_miss：是否打命中/未命中 DEBUG。**做跨模板仲裁的调用方应传 False**——
    它会自己在聚合层打一条带节点名与最高分的日志（v4 `TemplateRecognizer` 即是），
    逐模板再打一份就是同一件事的第二层表达：常驻锚点在厅里每帧必然不匹配，
    实测这类逐帧重复占运行日志的大头（与 best_match_score 同款理由）。
    """
    best_box, best_val = _best_match(frame, template, scales, roi)
    if best_val < threshold or best_box is None:
        if log_miss:
            logger.log(f"模板未命中: 最高分={best_val:.3f} < {threshold:.2f}", "DEBUG")
        return None, best_val
    if log_miss:
        logger.log(f"模板命中: {best_box} 置信度={best_val:.3f}", "DEBUG")
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
CURSOR_OCCLUSION_PAD_PX = 10.0  # 贴图盘之外的扩散量（环 + hover 高亮），避让用


def cursor_occlusion_radius_px(frame_w: int) -> float:
    """光标遮挡等效半径（帧像素）= 贴图盘半径 + 扩散量。

    与 `cursor_box_norm` 同源（都从 CURSOR_SIZE_NORM 派生）：图侧 mask_cursor 按
    贴图盘判相交，避让要**提前**挪开，故再加环/hover 的扩散余量。1280 宽帧
    → 0.03×1280/2 + 10 ≈ 29.2px（宿主侧原手标 30px 的来处）。
    """
    return CURSOR_SIZE_NORM * frame_w / 2.0 + CURSOR_OCCLUSION_PAD_PX


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
        res_min = cv2.matchTemplate(search[:, :, 0].astype(np.float32),
                                    tpl[:, :, 0].astype(np.float32),
                                    cv2.TM_CCOEFF_NORMED)
        for c in range(1, 3):
            res_c = cv2.matchTemplate(search[:, :, c].astype(np.float32),
                                      tpl[:, :, c].astype(np.float32),
                                      cv2.TM_CCOEFF_NORMED)
            res_min = np.minimum(res_min, res_c)
        _mn, mv, _mnl, ml = cv2.minMaxLoc(res_min)
        if mv < threshold or ml is None:
            return None, float(mv)
        th, tw = tpl.shape[:2]
        return (ml[0] + ox, ml[1] + oy, ml[0] + ox + tw, ml[1] + oy + th), float(mv)
    # gray 直走单通道匹配（find_template 与通道数无关；转三通道会白白 3 倍开销）
    f, t = _match_colorspace(frame, tpl, colorspace)
    # log_miss=False：本函数的契约是「调用方自己做阈值/领先仲裁」（见 find_any_cs），
    # 逐模板日志归聚合层——否则每帧每模板一条，把运行日志淹掉。
    return find_template(f, t, threshold=threshold, scales=scales, roi=roi, log_miss=False)


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

    sub_rect_norm 以命中框为基准的归一化子矩形 [x0,y0,x1,y1]（节点参数 color_assert.rect 语义）。
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
