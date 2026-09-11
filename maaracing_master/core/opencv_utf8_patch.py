#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
opencv_utf8_patch.py
===================
程序入口处 import 一次，全局生效。
原理：替换 cv2.imwrite/cv2.imread 以支持中文路径。
"""

from __future__ import annotations

import cv2
import numpy as np
from pathlib import Path
import sys

# 保存原始函数
_original_imwrite = cv2.imwrite
_original_imread = cv2.imread


def _is_ascii_path(path: Path) -> bool:
    """快速检测路径是否纯 ASCII"""
    try:
        str(path).encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def _imwrite_utf8(filename, img, params: list[int] | None = None) -> bool:
    """兼容中文路径的 cv2.imwrite 替代品"""
    path = Path(filename)

    # 纯 ASCII 路径走原生 API
    if _is_ascii_path(path):
        if params is None:
            return _original_imwrite(str(path), img)
        return _original_imwrite(str(path), img, params)

    # 中文路径：自动创建目录
    path.parent.mkdir(parents=True, exist_ok=True)

    # 编码为字节流
    ext = path.suffix.lower() or '.png'
    if params:
        success, buffer = cv2.imencode(ext, img, params)
    else:
        success, buffer = cv2.imencode(ext, img)

    if not success:
        return False

    # Python 写文件（支持 Unicode）
    path.write_bytes(buffer.tobytes())
    return True


def _imread_utf8(filename, flags=cv2.IMREAD_COLOR):
    """兼容中文路径的 cv2.imread 替代品"""
    path = Path(filename)

    # 纯 ASCII 路径走原生 API
    if _is_ascii_path(path):
        return _original_imread(str(path), flags)

    # 中文路径：Python 读字节再解码
    if not path.exists():
        return None

    buf = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    return cv2.imdecode(buf, flags)


# 应用 patch
if not getattr(cv2, '_utf8_patched', False):
    cv2.imwrite = _imwrite_utf8
    cv2.imread = _imread_utf8
    setattr(cv2, '_utf8_patched', True)

    # 必须走 stderr：sidecar 的 stdout 承载 JSONL 协议，任何时机的 stdout 输出都会污染协议流
    if sys.platform == 'win32':
        sys.stderr.write("[opencv_utf8_patch] 已启用中文路径兼容模式 (Windows)\n")
        sys.stderr.flush()