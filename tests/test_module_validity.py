# -*- coding: utf-8 -*-
"""模块有效期（manifest VALID_FROM / VALID_UNTIL）单测。

覆盖：
- 端点解析：带/不带时区偏移、非法类型、非法格式、字段缺省；
- 过期与窗口判定：闭区间两端、未声明 = 永久有效、当前时刻注入。

只依赖 core/module_validity（纯函数）：不 import registry / sidecar——它们会触发
plugins/* 的重依赖扫描（原因见 tests/conftest.py 顶部说明）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from maaracing_master.core.module_validity import (
    GAME_TZ,
    game_now,
    in_window,
    is_expired,
    parse_endpoint,
)

# 样本窗口（游戏服时间 UTC+8）：2026-08-06 05:00 — 2026-09-23 04:59
_WINDOW_START = datetime(2026, 8, 6, 5, 0, 0, tzinfo=GAME_TZ)
_WINDOW_END = datetime(2026, 9, 23, 4, 59, 59, tzinfo=GAME_TZ)


class TestParseEndpoint:
    def test_iso_with_offset(self):
        dt, err = parse_endpoint("2026-09-23T04:59:59+08:00", "VALID_UNTIL")
        assert err is None
        assert dt == _WINDOW_END
        assert dt.utcoffset() == timedelta(hours=8)

    def test_naive_iso_reads_as_game_tz(self):
        dt, err = parse_endpoint("2026-09-23T04:59:59", "VALID_UNTIL")
        assert err is None
        assert dt == _WINDOW_END  # 未标偏移 → 按游戏服 UTC+8 解释

    def test_missing_field_is_unbounded(self):
        assert parse_endpoint(None, "VALID_UNTIL") == (None, None)

    def test_bad_type_reported(self):
        dt, err = parse_endpoint(20260923, "VALID_UNTIL")
        assert dt is None
        assert err

    def test_bad_format_reported(self):
        dt, err = parse_endpoint("活动结束后", "VALID_UNTIL")
        assert dt is None
        assert err


class TestExpiryPredicate:
    def test_undeclared_until_never_expires(self):
        assert is_expired(None, _WINDOW_END + timedelta(days=3650)) is False

    def test_until_endpoint_is_inclusive(self):
        assert is_expired(_WINDOW_END, _WINDOW_END) is False               # 端点当刻仍有效
        assert is_expired(_WINDOW_END, _WINDOW_END + timedelta(seconds=1)) is True

    def test_naive_now_reads_as_game_tz(self):
        assert is_expired(_WINDOW_END, datetime(2026, 9, 23, 4, 59, 59)) is False
        assert is_expired(_WINDOW_END, datetime(2026, 9, 23, 5, 0, 0)) is True

    def test_aware_now_other_timezone_agrees(self):
        # 04:59:59+08:00 == 2026-09-22T20:59:59Z
        utc_end = datetime(2026, 9, 22, 20, 59, 59, tzinfo=timezone.utc)
        utc_next = datetime(2026, 9, 22, 21, 0, 0, tzinfo=timezone.utc)
        assert is_expired(_WINDOW_END, utc_end) is False
        assert is_expired(_WINDOW_END, utc_next) is True


class TestWindowPredicate:
    def test_both_endpoints_included(self):
        assert in_window(_WINDOW_START, _WINDOW_END, _WINDOW_START) is True
        assert in_window(_WINDOW_START, _WINDOW_END, _WINDOW_END) is True

    def test_before_start_not_available(self):
        assert in_window(_WINDOW_START, _WINDOW_END, _WINDOW_START - timedelta(seconds=1)) is False

    def test_after_end_not_available(self):
        assert in_window(_WINDOW_START, _WINDOW_END, _WINDOW_END + timedelta(seconds=1)) is False

    def test_unbounded_sides(self):
        far_future = datetime(2050, 1, 1, tzinfo=timezone.utc)
        assert in_window(None, None, far_future) is True
        assert in_window(None, _WINDOW_END, _WINDOW_END) is True
        assert in_window(_WINDOW_START, None, _WINDOW_START) is True

    def test_game_now_normalizes_naive_to_game_tz(self):
        assert game_now(datetime(2026, 9, 23, 4, 0, 0)).utcoffset() == timedelta(hours=8)
        assert game_now(_WINDOW_END.astimezone(timezone.utc)) == _WINDOW_END
