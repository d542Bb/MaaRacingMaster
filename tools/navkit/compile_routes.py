#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译模块 v3 routes 到 resources/generated/pipeline/<module>_routes.json。

用法：
    python tools/navkit/compile_routes.py                    # 默认 treasure，写盘
    python tools/navkit/compile_routes.py --module racing    # 指定模块，写盘
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

from maaracing_assistant.core.navkit import Assets, compile_routes_json  # noqa: E402

PLUGINS = _PROJ / "maaracing_assistant" / "plugins"


def paths_for(module: str) -> tuple[Path, Path, Path]:
    """返回 (assets, out, image_dir) 三件套路径。"""
    base = PLUGINS / module / "resources"
    return (
        base / "config" / f"{module}_assets.json",
        base / "generated" / "pipeline" / f"{module}_routes.json",
        base / "image",
    )


def discover_modules() -> list[str]:
    """扫描带 v3 资产的插件模块名（文件名前缀与目录名一致才算数）。"""
    found = []
    for assets in sorted(PLUGINS.glob("*/resources/config/*_assets.json")):
        module = assets.parent.parent.parent.name
        if assets.name == f"{module}_assets.json":
            found.append(module)
    return found


def compile_one(module: str, *, check: bool) -> int:
    assets_path, out_path, image_dir = paths_for(module)
    if not assets_path.is_file():
        print(f"[compile_routes:{module}] 资产不存在: {assets_path.relative_to(_PROJ)}",
              file=sys.stderr)
        return 1
    assets = Assets.load(assets_path, module=module, image_dirs=(image_dir,))
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
    rc = 0
    for module in modules:
        rc |= compile_one(module, check=args.check)
    if args.all and not modules:
        print("[compile_routes] plugins/ 下未发现任何 v3 资产", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
