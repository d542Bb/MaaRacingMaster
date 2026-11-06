#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户数据目录解析（%APPDATA%/MaaRacingMaster，与程序安装目录解耦）。

更新程序/覆盖安装不影响历史数据；APPDATA 缺失时回退到包根旁 data/（源码运行场景）。

目录结构（五类各归其位）：
    config/     配置类（profile.json、maa_option.json）
    data/       运行时数据与用户资产（data/treasure/ 业务库、data/speedrush/macros/ 用户宏、模型权重）
    logs/       会话记录（logs/<会话>/MaaRM_*.log + trace/ + perf.jsonl，共用「日志记录」开关）
    framework/  MAA 框架自产物（debug/maafw.log、debug/on_error 失败截图，跟随两项调试开关）
    debug/      调试产物与录制语料（debug/<会话>/<模块>/<行为>/，会话优先布局 v5）

会话编号（logs/ 与 debug/ 同一规则）：`<YYYYMMDD_HHMMSS>[_<序号>]`，进程启动时
生成一次，与两个开关无关。同一场运行的日志目录与调试目录同名，排查时两边一眼
能对上；轮次/局/阶段只作为索引与清单里的字段，不进入目录名。
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
    """运行时数据与用户资产目录（data/<module>/）：业务库、用户录制内容、模型权重等。

    与 debug/（调试产物，可随手清）分家：这里放用户需要保留、或被生产代码读取的东西。
    """
    return user_data_dir() / "data"


def logs_dir() -> Path:
    """会话记录根目录：logs/<会话>/（MaaRM_*.log 与伴随产物 trace.jsonl）。"""
    return user_data_dir() / "logs"


def framework_dir() -> Path:
    """MAA 框架 user_path：maafw.log 与框架 cache 的隔离落点。"""
    return user_data_dir() / "framework"


def debug_dir() -> Path:
    """调试产物根：`debug/<会话>/<模块>/<行为>/`（会话编号由 logger 持有）。"""
    return user_data_dir() / "debug"
