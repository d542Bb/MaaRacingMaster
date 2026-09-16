# -*- coding: utf-8 -*-
"""图资产通道边界的回归锁。

**为什么要专门锁**：通道写反在灰白画面上完全不可见（R=G=B 时互换无差）——
speedrush 的齿轮锚点与摄像机图标都是白的，treasure 的蛋检测与多数模板匹配又走
灰度，写反可以长期潜伏而不触发任何既有测试。故素材一律取**通道间空间模式互不
相同**的彩图：任何多余或缺失的翻转都会改变像素值。
（红/青同构棋盘抓不到这类错——``TM_CCOEFF_NORMED`` 对亮度不敏感，通道互换后
仍得高分，见 ``test_template_match_v4.py`` 的素材构造。）
"""
from __future__ import annotations

import builtins

import cv2
import numpy as np
import pytest

from maaracing_master.core.image_io import read_rgb, to_bgr, to_rgb, write_rgb


def _colorful(w: int = 16, h: int = 12) -> np.ndarray:
    """R 与 G 通道空间模式相反、B 恒定的彩图：通道互换即被本文件的判据抓到。"""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, w).astype(np.uint8)
    img[:, :, 0] = ramp
    img[:, :, 1] = ramp[::-1]
    img[:, :, 2] = 200
    return img


def _as_any_viewer_sees(path) -> np.ndarray:
    """文件里真实的像素序（R,G,B）——即任何标准读图工具看到的画面。

    标定依据（2026-09-16，PIL 复算）：cv2.imwrite 写入通道0=255 的数组后
    PIL 读回为蓝，故 imread 返回的数组是 BGR 序，翻转它即文件真实像素序。
    """
    raw = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert raw is not None
    return raw[:, :, ::-1]


# ==================== 磁盘边界 ====================

def test_write_rgb_writes_standard_pixel_order(tmp_path) -> None:
    """落盘必须是标准图像语义——看图软件、标注工具、训练框架按常规读即得同色。

    这条锁防的是"把内部数组直接交给 imwrite"：imwrite 期望 BGR，直写会把 R/B
    写反，而白灰素材上完全看不出来（speedrush 已录的三批会话就是这么错的）。
    """
    frame = _colorful()
    path = tmp_path / "shot.png"
    assert write_rgb(path, frame)
    assert np.array_equal(_as_any_viewer_sees(path), frame), "写盘 R/B 反了"


def test_read_rgb_recovers_internal_rgb(tmp_path) -> None:
    """读资产（模板/debug 帧/录制帧同处标准序）须经 read_rgb 拿到内部 RGB。"""
    frame = _colorful()
    path = tmp_path / "asset.png"
    assert write_rgb(path, frame)
    assert np.array_equal(read_rgb(path), frame)

    # 反向自证：裸 imread 得到的是 BGR 序，与内部 RGB 相反
    raw = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert np.array_equal(raw, to_bgr(frame))


def test_read_write_roundtrip_is_lossless(tmp_path) -> None:
    frame = _colorful()
    path = tmp_path / "rt.png"
    write_rgb(path, frame)
    assert np.array_equal(read_rgb(path), frame)


def test_read_rgb_missing_or_undecodable_returns_none(tmp_path) -> None:
    assert read_rgb(tmp_path / "不存在.png") is None
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert read_rgb(broken) is None


def test_jpeg_quality_is_forwarded(tmp_path) -> None:
    """质量参数必须透传到写盘——透丢会静默改用默认质量（体积/画质双失）。"""
    frame = _colorful(128, 96)
    low, high = tmp_path / "low.jpg", tmp_path / "high.jpg"
    write_rgb(low, frame, jpeg_quality=10)
    write_rgb(high, frame, jpeg_quality=98)
    assert low.stat().st_size < high.stat().st_size


# ==================== 纯转换 ====================

def test_to_bgr_and_to_rgb_are_inverse() -> None:
    frame = _colorful()
    assert np.array_equal(to_rgb(to_bgr(frame)), frame)
    assert np.array_equal(to_bgr(frame), frame[:, :, ::-1])
    assert to_bgr(frame).shape == frame.shape


def test_non_three_channel_passes_through() -> None:
    """灰度/单通道无通道序可言，原样返回（不得被误当作三通道拆解）。"""
    gray = np.arange(12, dtype=np.uint8).reshape(3, 4)
    assert to_bgr(gray) is gray
    assert to_rgb(gray) is gray


def test_conversion_does_not_mutate_input() -> None:
    frame = _colorful()
    before = frame.copy()
    to_bgr(frame)
    to_rgb(frame)
    assert np.array_equal(frame, before)


def test_pure_conversions_do_not_require_cv2(monkeypatch) -> None:
    """纯转换走切片，不触发 cv2 加载——core「零重依赖」纪律的回归锁。"""
    real_import = builtins.__import__

    def _no_cv2(name, *args, **kwargs):
        if name == "cv2":
            raise AssertionError("纯转换不应 import cv2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_cv2)
    assert np.array_equal(to_rgb(to_bgr(_colorful())), _colorful())


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
