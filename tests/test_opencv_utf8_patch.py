# -*- coding: utf-8 -*-
"""opencv_utf8_patch 回归锁：中文路径读写、ASCII 直通与全局生效。

补丁在 sidecar 入口 import 时全局替换 cv2 两个 IO 函数；ASCII 路径必须原样交给
OpenCV（保持编码参数与行为不变），中文路径必须能落盘并回读。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core import opencv_utf8_patch as patch


def _img() -> np.ndarray:
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    img[..., 1] = 200
    return img


def test_patch_is_applied_at_import():
    assert cv2.imwrite is patch._imwrite_utf8
    assert cv2.imread is patch._imread_utf8
    assert getattr(cv2, "_utf8_patched", False)


class TestAsciiPassthrough:
    def test_detection(self):
        assert patch._is_ascii_path(Path("plain/frame.png"))
        assert not patch._is_ascii_path(Path("中文/frame.png"))

    def test_write_delegates_without_params(self, monkeypatch, tmp_path):
        calls: list[tuple[str, object]] = []

        def fake(filename, img, params=None):
            calls.append((filename, params))
            return True

        monkeypatch.setattr(patch, "_original_imwrite", fake)
        assert patch._imwrite_utf8(tmp_path / "plain.png", _img()) is True
        assert calls == [(str(tmp_path / "plain.png"), None)]

    def test_write_delegates_with_params(self, monkeypatch, tmp_path):
        calls: list[tuple[str, object]] = []
        params = [cv2.IMWRITE_JPEG_QUALITY, 80]

        def fake(filename, img, p=None):
            calls.append((filename, p))
            return True

        monkeypatch.setattr(patch, "_original_imwrite", fake)
        assert patch._imwrite_utf8(tmp_path / "plain.jpg", _img(), params) is True
        assert calls == [(str(tmp_path / "plain.jpg"), params)]

    def test_read_delegates(self, monkeypatch, tmp_path):
        calls: list[tuple[str, int]] = []

        def fake(filename, flags):
            calls.append((filename, flags))
            return "sentinel"

        monkeypatch.setattr(patch, "_original_imread", fake)
        assert patch._imread_utf8(tmp_path / "plain.png") == "sentinel"
        assert calls == [(str(tmp_path / "plain.png"), cv2.IMREAD_COLOR)]


class TestChinesePath:
    def test_write_creates_parent_and_roundtrips(self, tmp_path):
        target = tmp_path / "中文目录" / "帧.png"
        assert not target.parent.exists()

        assert patch._imwrite_utf8(target, _img()) is True
        assert target.exists()

        back = patch._imread_utf8(target)
        assert back is not None
        assert back.shape == (8, 8, 3)
        assert int(back[0, 0, 1]) == 200

    def test_missing_file_reads_none(self, tmp_path):
        assert patch._imread_utf8(tmp_path / "不存在" / "x.png") is None

    def test_writes_png_when_suffix_absent(self, tmp_path):
        target = tmp_path / "中文" / "无后缀"
        assert patch._imwrite_utf8(target, _img()) is True
        assert target.read_bytes()[:4] == b"\x89PNG"
