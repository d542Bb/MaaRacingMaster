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
import sys
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[2]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from maaracing_assistant.core.navkit import (
    CORE_IMAGE_DIR,
    Assets,
    compile_routes_json,  # noqa: E402
)

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


def compile_one(module: str, *, check: bool) -> int:
    assets_path, out_path, image_dir = paths_for(module)
    if not assets_path.is_file():
        print(f"[compile_routes:{module}] 资产不存在: {assets_path.relative_to(_PROJ)}",
              file=sys.stderr)
        return 1
    # 追加语义下 core 段由 Assets.load 恒定前置，这里只供模块目录；global 无模块目录。
    explicit_dirs = () if module == "global" else (image_dir,)
    assets = Assets.load(assets_path, module=module, image_dirs=explicit_dirs)
    if not assets.routes:
        # 无 routes 段（global：只载页名/锚点）不产生成物，validate-only 直接通过。
        # 该判定必须在 check/写盘分支之前——check 首判 `not out_path.exists()`，
        # 不产生成物的资产会在这里被误报失败。
        print(f"[compile_routes:{module}] 无 routes 段，跳过编译（不产生成物）")
        return 0
    generated = compile_routes_json(assets)
    if check:
        if not out_path.exists() or out_path.read_text(encoding="utf-8") != generated:
            print(f"[compile_routes:{module}] 生成物与重新编译结果不一致", file=sys.stderr)
            return 1
        print(f"[compile_routes:{module}] --check 通过")
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generated, encoding="utf-8")
    print(f"[compile_routes:{module}] 已写入 {out_path.relative_to(_PROJ)}")
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
