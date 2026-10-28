#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户数据目录解析（%APPDATA%/MaaRacingMaster，与程序安装目录解耦）。

更新程序/覆盖安装不影响历史数据；APPDATA 缺失时回退到包根旁 data/（源码运行场景）。

目录结构（五类各归其位）：
    config/     配置类（profile.json、maa_option.json）
    data/       结构化业务数据（data/treasure/treasure.db）
    logs/       会话记录（logs/<会话>/MaaRM_*.log + trace.jsonl，共用「日志记录」开关）
    framework/  MAA 框架自产物（maafw.log、cache）
    debug/      调试产物与录制语料（debug/<module>/<会话>/，调试台契约）

会话目录命名（logs/ 与 debug/ 同一规则）：`<YYYYMMDD_HHMMSS>[_<标签>]`——时间戳
恒在最前，标签按「用途 → 阶段」追加（`20261028_220635`、`20261028_220635_manual_p2`）。
同一场运行的日志目录与调试目录同名：调试侧取 `logger.session_token` 作前缀，
所以排查时两边一眼能对上。
"""

from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    """用户数据根目录：%APPDATA%/MaaRacingMaster（无 APPDATA 时回退包根旁 data/）。"""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "MaaRacingMaster"
    return Path(__file__).resolve().parent.parent.parent / "data"


def config_dir() -> Path:
    """配置目录：profile.json（用户偏好）等。"""
    return user_data_dir() / "config"


def data_dir() -> Path:
    """结构化业务数据目录：各模块 SQLite 等（data/<module>/）。"""
    return user_data_dir() / "data"


def logs_dir() -> Path:
    """会话记录根目录：logs/<会话>/（MaaRM_*.log 与伴随产物 trace.jsonl）。"""
    return user_data_dir() / "logs"


def framework_dir() -> Path:
    """MAA 框架 user_path：maafw.log 与框架 cache 的隔离落点。"""
    return user_data_dir() / "framework"


def debug_dir() -> Path:
    """调试截图会话根：debug/<module>/<会话>/（调试台 session_dir 契约）。"""
    return user_data_dir() / "debug"
