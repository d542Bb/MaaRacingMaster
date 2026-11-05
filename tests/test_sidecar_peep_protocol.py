# -*- coding: utf-8 -*-
"""sidecar PEEP 协议（P0）：序号去重 + 元数据透传。"""
from __future__ import annotations

import base64
import types

import numpy as np
import pytest

sc = pytest.importorskip("maaracing_master.core.sidecar")
from maaracing_master.core.debug import NavigationDebugger


def _service(tmp_path):
    d = NavigationDebugger(tmp_path)
    d.enabled = True
    d.peep_enabled = True
    d.begin_run("peep_test")
    svc = object.__new__(sc.SidecarService)
    svc._controller = types.SimpleNamespace(debug=d)
    return svc, d


def test_frame_metadata_and_same_seq(tmp_path):
    svc, d = _service(tmp_path)
    try:
        seg = d.open_segment("nav", "进入大厅", peep_fps=1000)
        seg.offer(np.zeros((16, 16, 3), np.uint8), fid=7)
        assert d._wait_drain(2.0)
        ok, data, err = svc.get_peep_frame({})
        assert ok and err is None
        assert data["state"] == "live"
        assert data["owner"] == "nav" and data["label"] == "进入大厅"
        assert isinstance(data["frame"], str) and data["frame"]
        assert base64.b64decode(data["frame"])

        ok, same, _ = svc.get_peep_frame({"after_seq": data["seq"]})
        assert same["state"] == "same"
        assert same["frame"] is None
        assert same["seq"] == data["seq"]
    finally:
        d.end_run()


def test_toggle_has_stream_position_during_run(tmp_path):
    svc, d = _service(tmp_path)
    try:
        d.open_segment("nav", "x")
        d.peep_enabled = False
        assert d.last_toggle_seq > 0     # 运行中：开关事件在流里有位置
    finally:
        d.end_run()


def test_disabled_returns_no_frame(tmp_path):
    svc, d = _service(tmp_path)
    try:
        d.peep_enabled = False
        ok, data, _ = svc.get_peep_frame({})
        assert ok
        assert data["state"] == "disabled"
        assert data["frame"] is None
    finally:
        d.end_run()
