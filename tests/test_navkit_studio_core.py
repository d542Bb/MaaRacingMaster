# -*- coding: utf-8 -*-
"""ROI Studio 帧库契约（v4 重写基线：v3 `core/session.py` → `studio_sessions.py`）。

覆盖与内容无关的骨架：会话目录/帧名/模板名三份白名单、目录穿越防护、
「只认 png」历史坑回归。不启动进程、不依赖真实截图目录。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.navkit.studio_sessions import (
    RAW_RE,
    SESSION_RE,
    TPL_RE,
    RawFrameRef,
    SessionBrowser,
    SessionInfo,
    list_templates,
)

# ----------------------------------------------------------------------
# 白名单正则形态（锁死）
# ----------------------------------------------------------------------


class TestWhitelistPatterns:
    @pytest.mark.parametrize("name", ["20260812_183611", "session_20260303_000000"])
    def test_session_re_accepts(self, name):
        assert SESSION_RE.match(name)

    @pytest.mark.parametrize("name", ["2026081_183611", "20260812-183611", "not_a_session", ""])
    def test_session_re_rejects(self, name):
        assert not SESSION_RE.match(name)

    @pytest.mark.parametrize("name", [
        "0001_raw.png", "0002_raw.jpg", "0003_raw.jpeg", "0004_raw.webp",
    ])
    def test_raw_re_accepts_all_four_formats(self, name):
        """只认 png 会让 jpg 帧全黑（历史坑）——四种格式必须全放行。"""
        assert RAW_RE.match(name)

    @pytest.mark.parametrize("name", ["0001.png", "1_raw.png", "0001_raw.gif", "0001_raw.PNG"])
    def test_raw_re_rejects(self, name):
        assert not RAW_RE.match(name)

    @pytest.mark.parametrize("name", ["smart_bid_btn.png", "round-1.png", "a_b-c.png"])
    def test_tpl_re_accepts(self, name):
        assert TPL_RE.match(name)

    @pytest.mark.parametrize("name", ["../evil.png", "a/b.png", "x.jpg", "x.png.png.bak"])
    def test_tpl_re_rejects(self, name):
        assert not TPL_RE.match(name)


# ----------------------------------------------------------------------
# SessionBrowser：会话/帧白名单 + 目录穿越防护
# ----------------------------------------------------------------------


class TestSessionBrowser:
    @pytest.fixture
    def layout(self, tmp_path):
        """构造 debug_root/20260812_183611/raw/{0001_raw.jpg,0002_raw.png} 等结构。"""
        root = tmp_path / "debug"
        root.mkdir()
        sess_dir = root / "20260812_183611"
        raw = sess_dir / "raw"
        raw.mkdir(parents=True)
        (raw / "0001_raw.jpg").write_bytes(b"x")
        (raw / "0002_raw.png").write_bytes(b"x")
        (raw / "not_raw.txt").write_text("nope", encoding="utf-8")   # 非法 raw 名
        # 既无 raw/ 也无 trace.jsonl 的空目录不算合法会话
        (root / "20260101_000000").mkdir()
        # 独立 trace 会话（无图，仅决策流水）：合法入列
        trace_only = root / "20260202_000000"
        trace_only.mkdir()
        (trace_only / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        # 遗留 session_ 前缀形态：保持可读
        legacy = root / "session_20260303_000000"
        legacy.mkdir()
        (legacy / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        (root / "not_a_session").mkdir(parents=True)
        (root / "not_a_session" / "raw").mkdir()
        return root

    def test_list_sessions_only_valid(self, layout):
        b = SessionBrowser(layout)
        # 无 raw 且无 trace 的空目录、非法名被排除；含二者任一即入列，按时间戳降序
        assert b.list_sessions() == [
            "20260812_183611", "session_20260303_000000", "20260202_000000",
        ]

    def test_list_sessions_missing_root_empty(self, tmp_path):
        assert SessionBrowser(tmp_path / "nope").list_sessions() == []

    def test_has_frames_distinguishes_trace_only(self, layout):
        b = SessionBrowser(layout)
        assert b.has_frames("20260812_183611") is True    # 截图会话
        assert b.has_frames("20260202_000000") is False   # 纯决策流水会话
        assert b.has_frames("not_a_session") is False     # 非法名恒 False

    def test_list_raw_filters_by_whitelist(self, layout):
        b = SessionBrowser(layout)
        raw_list = b.list_raw("20260812_183611")
        assert raw_list == ["0001_raw.jpg", "0002_raw.png"]
        assert "not_raw.txt" not in raw_list

    def test_list_raw_invalid_session_empty(self, layout):
        b = SessionBrowser(layout)
        assert b.list_raw("bad_session") == []
        assert b.list_raw("00000000_000000") == []  # 合法形态但目录不存在

    def test_resolve_raw_in_bounds(self, layout):
        b = SessionBrowser(layout)
        p = b.resolve_raw("20260812_183611", "0001_raw.jpg")
        assert p is not None and p.is_file()
        assert p.parent.name == "raw"

    def test_resolve_raw_blocks_illegal_names(self, layout):
        b = SessionBrowser(layout)
        assert b.resolve_raw("20260812_183611", "not_raw.txt") is None
        assert b.resolve_raw("20260812_183611", "..\\escape.png") is None
        assert b.resolve_raw("bad_session", "0001_raw.jpg") is None

    def test_resolve_raw_blocks_traversal_outside_raw(self, layout):
        # raw/ 内没有该文件、但 raw/ 之外存在同名文件时，绝不越界读取外部路径
        (layout / "20260812_183611" / "0000_raw.jpg").write_bytes(b"x")  # 仅放在 raw 外
        b = SessionBrowser(layout)
        assert b.resolve_raw("20260812_183611", "0000_raw.jpg") is None

    def test_dataclasses_shape(self):
        assert SessionInfo.is_valid_name("20260812_183611") is True
        assert SessionInfo.is_valid_name("nope") is False
        ref = RawFrameRef(session="20260812_183611", name="0001_raw.png", path=Path("/tmp/x"))
        assert ref.session == "20260812_183611"


# ----------------------------------------------------------------------
# 模板名白名单目录
# ----------------------------------------------------------------------


class TestListTemplates:
    def test_lists_only_png_sorted(self, tmp_path):
        (tmp_path / "b.png").write_bytes(b"x")
        (tmp_path / "a.png").write_bytes(b"x")
        (tmp_path / "c.jpg").write_bytes(b"x")
        (tmp_path / "note.txt").write_text("x", encoding="utf-8")
        assert list_templates(tmp_path) == ["a.png", "b.png"]

    def test_missing_dir_empty(self, tmp_path):
        assert list_templates(tmp_path / "nope") == []
