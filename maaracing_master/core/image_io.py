#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图资产的通道边界：磁盘标准序 ↔ 内存内部 RGB，翻转只在这里发生。

cv2 的 ``imread`` 回来的是 BGR 序，而本仓库内部（capture 契约、模板、匹配内核）
一律用 RGB 序。两界相邻处的翻转只由本模块承担——散在各处各写一遍 ``cvtColor``
时漏掉一处，那一处就静默 R/B 互换。

**这类错误为什么难发现**：R=G=B 的灰白像素互换无差。speedrush 的齿轮锚点与
摄像机图标都是白的，treasure 的蛋检测与多数模板匹配又走灰度——写反可以长期潜伏
而不触发任何既有测试（红/青同构棋盘也抓不到：``TM_CCOEFF_NORMED`` 对亮度不敏感，
通道互换后仍高分）。故配 ``tests/test_image_io.py`` 的彩色素材回归锁。

**通道序的权威依据**：
  - OpenCV 官方文档（imgcodecs）：``imread`` 默认 ``IMREAD_COLOR_BGR``，彩色图像
    "the decoded images will have the channels stored in **B G R** order"；
    ``imwrite`` 与之对称，期望 BGR 输入。
  - 本机标定（2026-09-16）：``cv2.imwrite`` 写入通道0=255 的数组，用 PIL（与 OpenCV
    无关联的标准语义读取器）读回是**蓝**，印证 imwrite 期望 BGR。

**据此复算的三份资产**：
  - 模板 ``confirm_red_btn.png``：PIL 读得 R=216 G=23 B=42 即红色 ✓ 标准序；
  - debug 原始帧（出价页）：PIL 读得按钮区偏暖 ✓ 标准序；
  - 录制帧（驾驶页天空）：PIL 读得 R=101 G=111 B=120 偏冷 ✗ 而画面实为暖色
    —— 录制器曾绕过本模块直写 ``imwrite``，把 R/B 写反（已修，见 recorder）。
  - 由末条反推 ``ctx.capture`` 交出的是内部 RGB（imwrite 期望 BGR，要写出反序
    文件就必须收到 RGB）——即 ``CaptureCapability`` 的契约成立。

cv2 延迟 import：保持 core 「零重依赖」纪律——纯切片转换的 ``to_bgr`` / ``to_rgb``
不触发 cv2 加载。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

__all__ = ["to_bgr", "to_rgb", "read_rgb", "write_rgb"]


def to_bgr(frame_rgb: np.ndarray) -> np.ndarray:
    """内部 RGB 帧 → BGR 序数组（交给吃 BGR 的接口）。非 3 通道原样返回。"""
    if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
        return frame_rgb
    return np.ascontiguousarray(frame_rgb[:, :, ::-1])


def to_rgb(frame_bgr: np.ndarray) -> np.ndarray:
    """BGR 序数组 → 内部 RGB。非 3 通道原样返回（灰度无通道序可言）。"""
    return to_bgr(frame_bgr)  # 互换是对合运算，两个方向同一份实现


def read_rgb(path: str | Path, flags: int | None = None) -> np.ndarray | None:
    """读图，返回内部 RGB；不存在/解码失败返回 None（不抛）。

    这是读**任何入库图资产**（模板、debug 原始帧、录制帧）的通用入口——它们同处
    标准序，故一套读法通吃。离线工具读资产一律走这里，别再各写各的 ``imread``：
    漏掉翻转就会拿到 R/B 互换的画面，而这在白灰素材上看不出来。
    """
    import cv2

    raw = cv2.imread(str(path), cv2.IMREAD_COLOR if flags is None else flags)
    if raw is None:
        return None
    return to_rgb(raw)


def write_rgb(path: str | Path, frame_rgb: np.ndarray,
              *, jpeg_quality: int | None = None) -> bool:
    """把内部 RGB 帧写成**标准图像语义**（任何看图软件/标注工具/训练框架按常规
    读法打开即得运行时同色）。``jpeg_quality`` 仅对有损格式有意义。"""
    import cv2

    params: list[int] = []
    if jpeg_quality is not None:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    return bool(cv2.imwrite(str(path), to_bgr(frame_rgb), params))
