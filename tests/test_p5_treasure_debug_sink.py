# -*- coding: utf-8 -*-
"""debug.update_peep 公开接口契约：写入副本、读写同一缓冲。"""
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