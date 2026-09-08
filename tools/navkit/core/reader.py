#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""帧/模板读取与替换匹配核心（DebugStudio Core · 与内容无关）。

把「加载灰度模板（缓存）+ ROI 内多尺度模板匹配」收敛成通用实现，供调试台在任意
模块会话帧上预览命中。与内容无关：模板名只按白名单校验、矩形只按坐标契约放大，
不解释模板/矩形代表什么语义。

依赖 cv2（调试台运行态需要）；纯逻辑部分（白名单、命中框归一化）已尽量独立。
"""
from __future__ import annotations

import math
import threading
from pathlib import Path

import cv2
import numpy as np

from tools.navkit.core import session as sessmod

# 多尺度匹配缩放档兜底值（0.70×~1.30×，步长 0.05）。
#
# 真源是资产文档的 `match.scales`（与运行时 DetectionPlan.scales 同源）：调试台校准的
# 分值须在运行时同口径复现，故调用方一律显式传入文档档位（见 server 的
# `_match_scales()`）。本常量仅在文档缺 `match` 段（过渡期/空资产）时兜底，
# 不要把它当成权威——文档改了档位而这里不改，「所见即运行时」就失效。
MATCH_SCALES: tuple[float, ...] = (
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30,
)


class TemplateStore:
    """灰度模板的带锁缓存加载器。同一个模板加载一次，后续复用。"""

    def __init__(self, template_dir: Path):
        self.template_dir = Path(template_dir)
        self._cache: dict[str, np.ndarray | None] = {}
        self._lock = threading.Lock()

    def load_gray(self, name: str) -> np.ndarray | None:
        """按文件名白名单加载灰度模板；白名单外/不存在/读取失败返回 None。"""
        if not sessmod.TPL_RE.match(name):
            return None
        with self._lock:
            if name in self._cache:
                return self._cache[name]
        path = self.template_dir / name
        if not path.is_file():
            return None
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        with self._lock:
            self._cache[name] = img
        return img

    def invalidate(self, name: str) -> None:
        """模板文件被更新后清缓存（供上传/裁剪后调用）。"""
        with self._lock:
            self._cache.pop(name, None)

    def reload(self, name: str) -> np.ndarray | None:
        """强制重读一个模板（清缓存后重新加载）。"""
        self.invalidate(name)
        return self.load_gray(name)


def match_local(
    gray_big: np.ndarray,
    gray_tpl: np.ndarray,
    rect: "list[float]",
    scales: "tuple[float, ...] | list[float] | None" = None,
) -> dict:
    """ROI 内多尺度模板匹配，返回全局最优（分数最高）尺度的命中框。

    迁移自 treasures 匹配逻辑；适配 debug 场景：返回 size_ok/score/best_scale/
    pixel_box/crop_size/tpl_size/hit_box/hit_norm/reason，供前端叠加显示。

    `scales` 为待尝试的缩放档位：调用方须传资产文档 `match.scales`（运行时同口径），
    缺省回落模块常量 `MATCH_SCALES`（仅兜底，不是真源）。
    """
    H, W = gray_big.shape[:2]
    x1n, y1n, x2n, y2n = (float(v) for v in rect)
    x1, y1 = max(0, int(x1n * W)), max(0, int(y1n * H))
    x2, y2 = min(W, int(x2n * W)), min(H, int(y2n * H))
    if x2 <= x1 or y2 <= y1:
        return {"size_ok": False, "score": -1.0, "reason": "empty_crop"}
    crop = gray_big[y1:y2, x1:x2]
    th0, tw0 = gray_tpl.shape[:2]
    ch, cw = crop.shape[:2]
    best = None  # (score, scale, th_h, tw_w, mx_in_roi, my_in_roi)
    for s in (MATCH_SCALES if scales is None else scales):
        nw = max(4, int(round(tw0 * s)))
        nh = max(4, int(round(th0 * s)))
        if nh > ch or nw > cw:
            continue
        if nw == tw0 and nh == th0:
            tpl_s = gray_tpl
        else:
            interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
            try:
                tpl_s = cv2.resize(gray_tpl, (nw, nh), interpolation=interp)
            except Exception:
                continue
        try:
            res = cv2.matchTemplate(crop, tpl_s, cv2.TM_CCOEFF_NORMED)
        except Exception:
            continue
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if best is None or float(max_val) > best[0]:
            best = (float(max_val), s, nh, nw, int(max_loc[0]), int(max_loc[1]))
    if best is None:
        return {
            "size_ok": False, "score": -1.0,
            "pixel_box": [x1, y1, x2, y2], "crop_size": [cw, ch],
            "tpl_size": [tw0, th0], "reason": "roi_too_small",
        }
    score, scale, th, tw, mx, my = best
    hit_box = [x1 + mx, y1 + my, x1 + mx + tw, y1 + my + th]
    hit_norm = [
        max(0.0, min(1.0, hit_box[0] / W)), max(0.0, min(1.0, hit_box[1] / H)),
        max(0.0, min(1.0, hit_box[2] / W)), max(0.0, min(1.0, hit_box[3] / H)),
    ]
    return {
        "size_ok": True, "score": float(score), "best_scale": float(scale),
        "pixel_box": [x1, y1, x2, y2], "crop_size": [cw, ch],
        "tpl_size": [tw, th], "hit_box": hit_box, "hit_norm": hit_norm,
        "reason": "ok",
    }


def rect_to_pixel(rect: "list[float]", W: int, H: int) -> tuple[int, int, int, int]:
    """归一化 rect → 像素边界（含 clamp），供裁剪/预览；空 rect 返回 None 语义由调用方判断。

    仅作工具，与 ROIConfig.NormalizedROI.to_pixel 的循环统一口径（此处为调试台用的
    int-truncate 裁剪风格）。坐标契约的 floor/ceil exclusive 换算在运行时检测器落地。
    """
    x1n, y1n, x2n, y2n = (float(v) for v in rect)
    x1, y1 = max(0, int(x1n * W)), max(0, int(y1n * H))
    x2, y2 = min(W, math.ceil(x2n * W)), min(H, math.ceil(y2n * H))
    return x1, y1, x2, y2