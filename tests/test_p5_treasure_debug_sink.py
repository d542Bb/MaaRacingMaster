# -*- coding: utf-8 -*-
"""P5 迁移单测：treasure 清私有 debug 依赖 → 用 debug.update_peep 公开接口。

验证目标（计划 §十一 treasure Before/After invariants 的 debug output 等价部分）：
- debug 提供公开 update_peep(img)，封装 `_frame_lock`/`_latest_frame` 写入，
  模块不再直接访问私有成员；
- update_peep 写副本（调用方后续原地修改不污染预览）、与 get_peep_jpeg 读同一缓冲；
- treasure/module.py 对 `ctx.debug._frame_lock`/`_latest_frame` 的裸访问已移除。

注：get_peep_jpeg 在写入前已 copy 再编码，测试通过「写入→读回 latest 是否变化」判断
update_peep 的副本语义，无需依赖 JPEG 编码细节。
"""
from __future__ import annotations

import numpy as np
from pathlib import Path

from maaracing_master.core.debug import NavigationDebugger


def _make_debugger(tmp_path=None):
    # 构造最简 NavigationDebugger（debug_root 必填），启用 peep（不触发存盘目录）
    d = NavigationDebugger(Path(tmp_path or "/tmp/deb_stub"))
    d.peep_enabled = True
    return d


class TestUpdatePeep:
    def test_write_then_read_roundtrip(self, tmp_path):
        d = _make_debugger(tmp_path)
        frame = np.zeros((10, 10), dtype=np.uint8)
        d.update_peep(frame)
        with d._frame_lock:
            assert d._latest_frame.shape == frame.shape

    def test_copy_semantics(self, tmp_path):
        # 写副本：调用方原地修改原数组不影响已投递的预览帧
        d = _make_debugger(tmp_path)
        frame = np.full((8, 8), 7, dtype=np.uint8)
        d.update_peep(frame)
        frame[:] = 255  # 原地修改原数组
        with d._frame_lock:
            assert (d._latest_frame == 7).all()

    def test_repeated_write_replaces_last(self, tmp_path):
        d = _make_debugger(tmp_path)
        d.update_peep(np.full((4, 4), 1, dtype=np.uint8))
        d.update_peep(np.full((4, 4), 2, dtype=np.uint8))
        with d._frame_lock:
            assert (d._latest_frame == 2).all()

    def test_idempotent(self, tmp_path):
        d = _make_debugger(tmp_path)
        f = np.zeros((3, 3), dtype=np.uint8)
        d.update_peep(f)
        d.update_peep(f)
        d.update_peep(f)


def test_treasure_no_private_debug_member_access():
    """treasure/module.py 不再直接访问 debug 的私有帧成员（P5 红线）。

    直接以源码文本断言，避免 import treasure/module.py（拉入重依赖）——
    只确认私有成员访问已从 IO worker 移除。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / \
        "maaracing_master/plugins/treasure/module.py"
    text = src.read_text(encoding="utf-8")
    # 不应再通过 ctx.debug 直接访问私有帧成员（P5 红线）；注释提及不算访问，故只拦访问形态。
    assert "ctx.debug._frame_lock" not in text
    assert "ctx.debug._latest_frame" not in text