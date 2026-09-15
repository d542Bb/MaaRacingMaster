# -*- coding: utf-8 -*-
"""模块有效期判定：解析 manifest 声明的 VALID_FROM / VALID_UNTIL，并判断当前是否在窗口内。

纯函数模块：不 import registry / logger / 任何插件，单测可直接导入（不会触发
plugins/* 的重依赖扫描）；registry 是唯一使用方，负责从 manifest 取字段并保管
各插件的端点，判定逻辑全部落在这里。

语义：
- 端点 = ISO 8601 字符串（如 "2026-09-23T04:59:59+08:00"）；未带时区偏移时
  按游戏服时间（国服 UTC+8）解释；
- 闭区间：两端均含；任一端未声明（None）即该方向不设限，两端都缺省 = 永久有效；
- 解析失败一律按「未声明」处理，错误说明交调用方记日志。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 游戏服时间（国服 UTC+8）：manifest 时间戳未标偏移时的解释时区
GAME_TZ = timezone(timedelta(hours=8))


def game_now(now: datetime | None = None) -> datetime:
    """当前时刻（aware）。传入 naive 时间按游戏服时区解释，便于测试注入。"""
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=GAME_TZ)


def parse_endpoint(raw, expected: str) -> tuple[datetime | None, str | None]:
    """解析有效期端点，返回 (时刻 | None, 错误说明 | None)。

    字段缺失（raw is None）→ (None, None)，即该方向不设限；
    类型/格式非法 → (None, 说明)，调用方记 WARNING 后同样按未声明处理。
    """
    if raw is None:
        return (None, None)
    if not isinstance(raw, str):
        return (None, f"{expected} 非字符串: {raw!r}")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return (None, f"{expected}={raw!r} 不是 ISO 8601 时间")
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=GAME_TZ), None)


def is_expired(valid_until: datetime | None, now: datetime | None = None) -> bool:
    """是否已过有效期（valid_until 为 None = 永不过期）。"""
    return valid_until is not None and game_now(now) > valid_until


def in_window(
    valid_from: datetime | None,
    valid_until: datetime | None,
    now: datetime | None = None,
) -> bool:
    """当前是否处于 [valid_from, valid_until] 闭区间内（None 的一端不设限）。"""
    ts = game_now(now)
    return not (
        (valid_from is not None and ts < valid_from)
        or (valid_until is not None and ts > valid_until)
    )
