#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译模块 v3 routes 到 resources/generated/pipeline/<module>_routes.json。

用法：
    python tools/navkit/compile_routes.py                    # 默认 treasure，写盘
    python tools/navkit/compile_routes.py --module speedrush  # 指定模块，写盘
    python tools/navkit/compile_routes.py --all --check      # 扫描全部插件资产逐一校验
    python tools/navkit/compile_routes.py --check            # 生成物与重编译一致性校验

路径派生契约（与 server.assets_path_for 同形，目录/文件名都随模块名）：
    assets   = plugins/<m>/resources/config/<m>_assets.json
    out      = plugins/<m>/resources/generated/pipeline/<m>_routes.json
    images   = plugins/<m>/resources/image
--all 扫描 plugins/*/resources/config/*_assets.json（文件名前缀须与目录名一致）。
纯标准库：不 import cv2 / numpy，可在最干净环境（CI）直接运行。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[2]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from maaracing_assistant.core.navkit import (
    CORE_IMAGE_DIR,
    OWNER_GLOBAL,
    Assets,
    compile_routes_json,  # noqa: E402
)
from tools.navkit.compile_lib import compile_and_write

PLUGINS = _PROJ / "maaracing_assistant" / "plugins"
CORE_RES = CORE_IMAGE_DIR.parent          # core/resources
GLOBAL_ASSETS = CORE_RES / "config" / "global_assets.json"


def paths_for(module: str) -> tuple[Path, Path, Path]:
    """返回 (assets, out, image_dir) 三件套路径。

    global 是 core 侧固定段（只载页名/锚点，无 routes），out 仅占位、永不写盘。
    """
    if module == "global":
        return (
            GLOBAL_ASSETS,
            CORE_RES / "generated" / "pipeline" / "global_routes.json",
            CORE_IMAGE_DIR,
        )
    base = PLUGINS / module / "resources"
    return (
        base / "config" / f"{module}_assets.json",
        base / "generated" / "pipeline" / f"{module}_routes.json",
        base / "image",
    )


def discover_modules() -> list[str]:
    """扫描带 v3 资产的模块名：plugins glob ∪ global 固定项。

    global 恒在扫描集合内（资产文件存在即纳入），`--all` 的反断言依赖它；
    plugins 侧文件名前缀与目录名一致才算数。
    """
    found = []
    for assets in sorted(PLUGINS.glob("*/resources/config/*_assets.json")):
        module = assets.parent.parent.parent.name
        if assets.name == f"{module}_assets.json":
            found.append(module)
    if GLOBAL_ASSETS.is_file():
        found.append("global")
    return found


def check_schedule(assets_path: Path) -> list[str]:
    """D1 轻校验：同目录 `schedule.json` 存在即校验活动时间窗，缺失不强制
    （永久玩法模块没有此文件）。GUI 直读意味着格式错误要拖到运行时才暴露，
    它不能成为唯一没有守卫的真源。

    格式：{"activity_window": {"start": "YYYY-MM-DD HH:MM", "end": "..."}}
    """
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
    start = end = None
    for key in ("start", "end"):
        raw = window.get(key)
        if not isinstance(raw, str):
            errors.append(f"activity_window.{key} 缺失或非字符串")
            continue
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            errors.append(f"activity_window.{key}={raw!r} 不符合 {fmt} 格式")
            continue
        if key == "start":
            start = parsed
        else:
            end = parsed
    if start is not None and end is not None and start > end:
        errors.append("activity_window.start 晚于 end")
    return errors


def _load_global_assets() -> Assets | None:
    """加载 core 侧 global 资产段（若文件已建立）。经模块级 `GLOBAL_ASSETS` 读取，
    令测试的 monkeypatch 生效；文件不存在（过渡期）返回 None，merge 侧按无 global 处理。
    """
    if not GLOBAL_ASSETS.is_file():
        return None
    return Assets.load(GLOBAL_ASSETS, module=OWNER_GLOBAL)


def compile_one(module: str, *, check: bool) -> int:
    """CLI 适配层：编译实际委托给 compile_lib.compile_and_write。"""
    assets_path, out_path, image_dir = paths_for(module)
    try:
        result = compile_and_write(
            module,
            check=check,
            paths_for=paths_for,
            global_assets=GLOBAL_ASSETS,
            project_root=_PROJ,
        )
    except FileNotFoundError:
        print(f"[compile_routes:{module}] 资产不存在: {assets_path.relative_to(_PROJ)}", file=sys.stderr)
        return 1
    except ValueError as exc:
        message = str(exc)
        if "activity_window" in message or "schedule.json" in message:
            message = f"schedule: {message}"
        print(f"[compile_routes:{module}] {message}", file=sys.stderr)
        return 1
    if result["status"] == "skipped_no_routes":
        print(f"[compile_routes:{module}] 无 routes 段，跳过编译（不产生成物）")
    elif check:
        print(f"[compile_routes:{module}] --check 通过")
    else:
        try:
            shown = out_path.relative_to(_PROJ)
        except ValueError:
            shown = out_path
        print(f"[compile_routes:{module}] 已写入 {shown}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", default="treasure",
                        help="插件模块 ID（决定资产/生成物/模板图三件套路径）")
    parser.add_argument("--all", action="store_true",
                        help="扫描 plugins/ 下全部带 v3 资产的模块逐一处理（覆盖 --module）")
    parser.add_argument("--check", action="store_true",
                        help="只校验生成物与重编译结果一致，不写盘；任一不一致退出码 1")
    args = parser.parse_args()

    modules = discover_modules() if args.all else [args.module]
    if args.all:
        if not modules:
            print("[compile_routes] plugins/ 下未发现任何 v3 资产", file=sys.stderr)
            return 1
        # 反断言×2：① global 资产文件在而扫描集合没有它 = 固定项被误删（条件式，
        # 文件尚未建立时不拦）；② 固定项不得架空"至少一个真实模块段"的底线——
        # 否则插件资产被整体误删时 --all 依旧静默通过（假绿）。
        if GLOBAL_ASSETS.is_file() and "global" not in modules:
            print("[compile_routes] --all 扫描集合缺少 global 固定项（反断言失败）",
                  file=sys.stderr)
            return 1
        if not [m for m in modules if m != "global"]:
            print("[compile_routes] --all 扫描集合不含任何插件模块段（反断言失败）",
                  file=sys.stderr)
            return 1
    rc = 0
    for module in modules:
        rc |= compile_one(module, check=args.check)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
