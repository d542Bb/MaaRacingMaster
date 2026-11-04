#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""框架侧落盘策略——只留文本日志（含框架自带 7 天回收），与界面开关无关。

**为什么存在**：maafw 在 `Toolkit.init_option()` 时读取 `framework/config/maa_option.json`，
默认生成 `logging=true`（持续写 TRACE 级 `maafw.log`）与 `save_on_error=true`（每次识别
失败落一张全屏 PNG）。这两项属于框架自身行为，不跟程序的「DEBUG 截图」「日志记录」开关
挂钩——那两个开关只管辖应用自己的调试语料与会话日志。本模块只保证一条固定策略：

    logging=true          写持续文本日志；框架每次启动在后台删除 debug/ 下超过 7 天的
                          .log/.jpg/.png —— 这是框架唯一的自动回收通道
    save_on_error=false   不落失败截图：框架把探针「没问到」也算出错，这类图实测约
                          2.3GB/天、95% 以上是噪声；视觉证据由应用的 DEBUG 开关按需提供
    save_draw / draw_quality / stdout_level 不由本模块改

成本（实测 5.12.3 + 本项目驾驶流程）：文本约 183MB/天。框架只按 16MB 轮转、靠 7 天
回收会到 ~1.25GB，故本模块在启动时把轮转备份总量压到 256MB（`prune_log_backups`），
框架 debug 目录的文本部分稳定在 ~272MB（备份 + 当前文件）。
框架的「出错」口径是**一次节点执行到超时都没有命中**（动作失败、节点无效同样算），
包括模块有意做的负向探针（如驾驶中每 0.5s 问一次横幅在不在）——这是关掉失败截图的原因。

**生效时机**：框架只在 init 那一次读配置，故启动时写一次；运行中切界面开关不重写。

写文件走「临时文件 + 原子替换」：中途失败不会留下半截 JSON 让框架读到空配置
（空配置会让 logging/save_on_error 回落框架默认值）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from maaracing_master.core.paths import framework_dir

__all__ = ["option_path", "apply_policy", "prune_log_backups"]

# 固定策略：文本日志开（同时启用框架自带的 7 天回收），失败截图关（探针失败是噪声）。
_POLICY = {
    "logging": True,
    "save_on_error": False,
}

# 轮转备份保留上限。框架只按 16MB 轮转、不按数量回收，只靠 7 天回收会到 ~1.25GB；
# 这里启动时删最旧的 maafw.bak.*.log，把备份总量压进上限（当前 maafw.log 不动）。
# 256MB ≈ 连续驾驶十余小时的框架内部流水，足够覆盖近几天的排障回看。
LOG_BACKUP_MAX_BYTES = 256 * 1024 ** 2

# 本模块不管的键：文件缺失/字段缺失时按框架默认值补齐。数值与 maafw 5.12.3 的
# 默认生成结果一致，改这里等于改框架行为，须有实机依据。
_DEFAULTS = {
    "save_draw": False,
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


def apply_policy() -> dict | None:
    """写入固定的框架落盘策略，返回实际写入的内容；写失败返回 None。

    写失败（无权限/磁盘满）不抛：框架按既有配置运行，由调用方决定是否告警——
    它不该拦住启动。
    """
    path = option_path()
    merged = dict(_DEFAULTS)
    merged.update(_read_existing(path))
    merged.update(_POLICY)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=4)
        os.replace(tmp, path)
    except OSError:
        return None
    return merged


def prune_log_backups() -> tuple[int, int]:
    """删除最旧的文本日志轮转备份，返回 (删除文件数, 释放字节数)。

    框架的 7 天回收太宽松（实测文本 ~183MB/天，稳态 1.25GB），这里按体积上限收到
    256MB：只看 `debug/maafw.bak.*.log`，从最旧的删到总量不超限；当前 `maafw.log`
    由框架持有、不在此列。单个文件删不掉跳过，不抛——清理失败不该拦住启动。
    """
    debug_dir = framework_dir() / "debug"
    if not debug_dir.is_dir():
        return (0, 0)
    entries: list[tuple[float, int, Path]] = []
    for path in debug_dir.glob("maafw.bak.*.log"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, path))
    if not entries:
        return (0, 0)

    total = sum(size for _, size, _ in entries)
    removed = 0
    freed = 0
    for _, size, path in sorted(entries):
        if total <= LOG_BACKUP_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
        freed += size
    return (removed, freed)
