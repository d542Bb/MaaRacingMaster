#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户数据目录解析（%APPDATA%/MaaRacingMaster，与程序安装目录解耦）。

更新程序/覆盖安装不影响历史数据；APPDATA 缺失时回退到包根旁 data/（源码运行场景）。

目录结构（五类各归其位）：
    config/     配置类（profile.json、maa_option.json）
    data/       结构化业务数据（data/treasure/treasure.db）
    logs/       应用日志（MaaRM_*.log）
    framework/  MAA 框架自产物（maafw.log、cache）
    debug/      调试截图会话（debug/<module>/<会话>/，调试台契约）
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
    """应用日志目录：MaaRM_*.log。"""
    return user_data_dir() / "logs"


def framework_dir() -> Path:
    """MAA 框架 user_path：maafw.log 与框架 cache 的隔离落点。"""
    return user_data_dir() / "framework"


def debug_dir() -> Path:
    """调试截图会话根：debug/<module>/<会话>/（调试台 session_dir 契约）。"""
    return user_data_dir() / "debug"
