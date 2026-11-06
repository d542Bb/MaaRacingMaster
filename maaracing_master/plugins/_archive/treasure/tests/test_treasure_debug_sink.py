# -*- coding: utf-8 -*-
"""treasure 不再直接访问 debug 私有帧成员（P5 红线）。"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.retired


def test_treasure_no_private_debug_member_access():
    """treasure/module.py 不再直接访问 debug 的私有帧成员（P5 红线）。

    直接以源码文本断言，避免 import treasure/module.py（拉入重依赖）——
    只确认私有成员访问已从 IO worker 移除。
    """
    src = Path(__file__).resolve().parents[1] / "module.py"
    text = src.read_text(encoding="utf-8")
    # 不应再通过 ctx.debug 直接访问私有帧成员（P5 红线）；注释提及不算访问，故只拦访问形态。
    assert "ctx.debug._frame_lock" not in text
    assert "ctx.debug._latest_frame" not in text
