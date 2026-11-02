#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""框架侧产物开关——把 maafw 的 `maa_option.json` 挂到 GUI 两项调试开关上。

**为什么存在**：maafw 自己会往 `framework/debug/` 写 `maafw.log` 与失败截图
（`on_error/`），这两类产物原先无条件生成、且不轮转：用户把「DEBUG 截图」和
「日志记录」都关掉时，它们照样增长（实机一天 2.4GB，全在失败截图）。本模块把
它们的开关与用户开关对齐，让「两个开关都关 = 不再有框架侧落盘」成立：

    logging         ← 日志记录（关 = 不写 maafw.log）
    save_on_error   ← DEBUG 截图（关 = 不写失败截图）
    save_draw / draw_quality / stdout_level 保持原值，不由本模块改

**生效时机**：maafw 的配置在 `Toolkit.init_option()` 那一次被读入，之后改文件
不会回头重读。故运行中切换开关只更新文件、**下次启动**才被框架采用——调用方
的日志措辞须如实这么说，不得承诺即时生效。

写文件走「临时文件 + 原子替换」：中途失败不会留下半截 JSON 让框架读到空配置
（空配置会让 logging/save_on_error 回落框架默认值，等于悄悄把开关又打开）。
"""

from __future__ import annotations

import json
import os

from maaracing_master.core.paths import framework_dir

__all__ = ["option_path", "sync"]

# 框架默认值：文件缺失/字段缺失时按这套补齐。数值与 maafw 5.12.3 的默认生成
# 结果一致，改这里等于改框架行为，须有实机依据。
_DEFAULTS = {
    "logging": True,
    "save_draw": False,
    "save_on_error": True,
    "draw_quality": 85,
    "stdout_level": 2,
}


def option_path():
    """框架选项文件路径：`framework/config/maa_option.json`（= init_option 的落点）。"""
    return framework_dir() / "config" / "maa_option.json"


def _read_existing(path) -> dict:
    """读既有选项（保留本模块不管的键）；文件缺失或损坏时回落到空 dict。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sync(debug_enabled: bool, file_logging: bool) -> dict | None:
    """按 GUI 开关写框架选项，返回实际写入的内容；写失败返回 None。

    debug_enabled → save_on_error（失败截图）
    file_logging  → logging（maafw.log）

    写失败（无权限/磁盘满）不抛：框架侧产物开关降级为「维持原状」，由调用方
    决定是否告警——它不该拦住启动或拖垮开关切换。
    """
    path = option_path()
    merged = dict(_DEFAULTS)
    merged.update(_read_existing(path))
    merged["save_on_error"] = bool(debug_enabled)
    merged["logging"] = bool(file_logging)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=4)
        os.replace(tmp, path)
    except OSError:
        return None
    return merged
