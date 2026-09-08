#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit routes 编译共用库（CLI 与 Studio server 的唯一写盘入口）。

本模块只依赖 Python 标准库与 `maaracing_assistant.core.navkit`（同样纯标准库），不依赖
cv2/numpy；CI 只装 pytest 也可以执行 `compile_routes.py --check`。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from maaracing_assistant.core.navkit import (
    CORE_IMAGE_DIR,
    OWNER_GLOBAL,
    Assets,
    compile_routes_json,
)

_PROJ = Path(__file__).resolve().parents[2]
_PLUGINS = _PROJ / "maaracing_assistant" / "plugins"
_CORE_RES = CORE_IMAGE_DIR.parent
_GLOBAL_ASSETS = _CORE_RES / "config" / "global_assets.json"


def default_paths_for(module: str) -> tuple[Path, Path, Path]:
    """返回 (assets, generated routes, image_dir)。"""
    if module == "global":
        return (
            _GLOBAL_ASSETS,
            _CORE_RES / "generated" / "pipeline" / "global_routes.json",
            CORE_IMAGE_DIR,
        )
    base = _PLUGINS / module / "resources"
    return (
        base / "config" / f"{module}_assets.json",
        base / "generated" / "pipeline" / f"{module}_routes.json",
        base / "image",
    )


def _check_schedule(assets_path: Path) -> list[str]:
    sched = assets_path.parent / "schedule.json"
    if not sched.is_file():
        return []
    try:
        data = json.loads(sched.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"schedule.json 不可解析: {exc}"]
    window = data.get("activity_window") if isinstance(data, dict) else None
    if not isinstance(window, dict):
        return ["schedule.json 缺 activity_window 对象"]
    errors: list[str] = []
    fmt = "%Y-%m-%d %H:%M"
    parsed: dict[str, datetime] = {}
    for key in ("start", "end"):
        raw = window.get(key)
        if not isinstance(raw, str):
            errors.append(f"activity_window.{key} 缺失或非字符串")
            continue
        try:
            parsed[key] = datetime.strptime(raw, fmt)
        except ValueError:
            errors.append(f"activity_window.{key}={raw!r} 不符合 {fmt} 格式")
    if parsed.get("start") and parsed.get("end") and parsed["start"] > parsed["end"]:
        errors.append("activity_window.start 晚于 end")
    return errors


def compile_document(
    module: str,
    document: dict,
    *,
    output_path: Path | None = None,
    global_assets: Path | None = None,
) -> dict[str, object]:
    """编译内存文档但不写盘，供 Studio preview 使用。"""
    assets = Assets.from_document(document, module=module)
    out_path = output_path or default_paths_for(module)[1]
    global_path = global_assets or _GLOBAL_ASSETS
    if module != OWNER_GLOBAL and global_path.is_file():
        assets = assets.merge(Assets.load(global_path, module=OWNER_GLOBAL))
    if not assets.routes:
        return {"status": "skipped_no_routes", "out_path": str(out_path), "output": ""}
    return {"status": "preview", "out_path": str(out_path), "output": compile_routes_json(assets)}


def compile_and_write(
    module: str,
    *,
    check: bool = False,
    write: bool = True,
    paths_for: Callable[[str], tuple[Path, Path, Path]] = default_paths_for,
    global_assets: Path | None = None,
    project_root: Path = _PROJ,
) -> dict[str, object]:
    """编译并按 CLI 形态写盘，返回结构化结果供 server 预览/保存使用。

    返回：
      ``status``: ``written`` / ``skipped_no_routes`` / ``checked``；
      ``out_path``: 生成物路径（无 routes 时仍返回目标路径）；
      ``output``: 生成物文本（无 routes 时为空）；
      ``error``: 失败原因（同时抛出 ValueError，CLI 可映射为退出码）。

    `check=True` 从不写盘；正式 Studio 保存调用 `check=False`，这保证 CLI 与 server 的
    成物字节完全由同一个 `compile_routes_json` 和同一个写盘出口产生。
    """
    assets_path, out_path, image_dir = paths_for(module)
    if not assets_path.is_file():
        raise FileNotFoundError(f"资产不存在: {assets_path}")
    explicit_dirs = () if module == "global" else (image_dir,)
    assets = Assets.load(assets_path, module=module, image_dirs=explicit_dirs)
    schedule_errors = _check_schedule(assets_path)
    if schedule_errors:
        raise ValueError("; ".join(schedule_errors))
    if not assets.routes:
        return {
            "status": "skipped_no_routes",
            "out_path": str(out_path),
            "output": "",
            "source_hash": assets.source_hash,
        }
    global_path = global_assets or _GLOBAL_ASSETS
    if module != OWNER_GLOBAL and global_path.is_file():
        assets = assets.merge(Assets.load(global_path, module=OWNER_GLOBAL))
    generated = compile_routes_json(assets)
    if check:
        if not out_path.exists() or out_path.read_text(encoding="utf-8") != generated:
            raise ValueError(f"生成物与重新编译结果不一致: {out_path}")
        return {
            "status": "checked",
            "out_path": str(out_path),
            "output": generated,
            "source_hash": assets.source_hash,
        }
    if not write:
        return {
            "status": "preview",
            "out_path": str(out_path),
            "output": generated,
            "source_hash": assets.source_hash,
        }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"` 保持成物字节规范，避免 Windows 转换为 CRLF。
    out_path.write_text(generated, encoding="utf-8", newline="\n")
    return {
        "status": "written",
        "out_path": str(out_path),
        "output": generated,
        "source_hash": assets.source_hash,
    }
