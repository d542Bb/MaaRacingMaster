# -*- coding: utf-8 -*-
"""template_match.load_template 模板名形态兼容测试（P2a-Q3 前置修复）。"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from maaracing_assistant.core import template_match as tm


@pytest.fixture()
def tpl_dir(tmp_path, monkeypatch):
    """写一张 20x10 蓝色测试模板并清空模块缓存。"""
    img = np.zeros((10, 20, 3), dtype=np.uint8)
    img[:, :] = (30, 60, 200)
    cv2.imwrite(str(tmp_path / "btn.png"), img)
    monkeypatch.setattr(tm, "_cache", {})
    return tmp_path


def test_bare_name_still_resolves(tpl_dir):
    assert tm.load_template("btn", [tpl_dir]) is not None


def test_name_with_extension_no_double_append(tpl_dir):
    got = tm.load_template("btn.png", [tpl_dir])
    assert got is not None and got.shape[:2] == (10, 20)


def test_missing_name_returns_none(tpl_dir):
    assert tm.load_template("nope", [tpl_dir]) is None
    assert tm.load_template("nope.png", [tpl_dir]) is None
