#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布分发剔除判定：打包时刻已过有效期的活动模块不随包分发。

与 `assemble.ps1` 的契约：
    python list_ship_excluded_modules.py <plugins_root> [--validity-module <path>] [--now <ISO8601>]
stdout 输出单行 JSON：
    {"expired": [{"id": "...", "dir": "..."}], "kept": [{"id": "...", "dir": "..."}], "warnings": [...]}
`expired` 按 dir 剔除（assemble 删 stage 目录），`kept` 供反向守卫断言"应保留仍在"。

判据真源：各插件 `manifest.py` 的 `VALID_UNTIL`，端点解析与"已失效"判定复用
`maaracing_master/core/module_validity`（与运行期 registry 置灰同一口径，不建第二真相）。
- 未声明 VALID_UNTIL = 永久有效 = 随包分发；
- 闭区间语义：打包时刻恰等于端点仍在窗口内（is_expired 为严格大于）；
- manifest 值解析失败 / 非字面量：按「未声明」处理并记 warning（宁可多分发，
  不静默吞模块——与 registry 运行期对非法端点的容错方向一致）；
- VALID_FROM 在未来（活动未开放）不构成剔除：随包分发、运行期照常按窗口置灰；
- 无 manifest.py 的目录不是插件，不参与判定。

manifest 用 AST 读字面量而非 import：打包工具不执行插件代码（插件包 __init__ 与
manifest 保持纯数据契约，但工具侧不依赖这一自觉）。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# manifest 里参与判定的字段（ID 供上报与守卫引用，端点字段供有效期判定）
_FIELDS = ("ID", "VALID_FROM", "VALID_UNTIL")


def _load_validity(module_path: Path):
    """按路径加载 core/module_validity（避免触发 maaracing_master 包初始化）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_ship_module_validity", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 module_validity: {module_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest_literals(path: Path) -> tuple[dict[str, Any], list[str]]:
    """AST 解析 manifest 顶层字面量赋值，返回 (字段值表, warnings)。"""
    values: dict[str, Any] = {}
    warnings: list[str] = []
    # utf-8-sig 剥 BOM：importlib 容忍带 BOM 的源文件，ast.parse 不容忍——
    # 口径向 importlib 看齐（manifest 被 IDE 存成 BOM 时运行期照常注册，工具不得双标）
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        for name in targets:
            if name not in _FIELDS or value is None:
                continue
            try:
                values[name] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                warnings.append(
                    f"{path.parent.name}/manifest.py: {name} 非字面量，按未声明处理"
                )
    return values, warnings


def scan(plugins_root: Path, validity_path: Path, now: datetime | None = None) -> dict:
    """扫描 plugins_root，返回 {expired, kept, warnings}；dir 为相对 plugins_root 的目录名。"""
    validity = _load_validity(validity_path)
    expired: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    warnings: list[str] = []

    for entry in sorted(plugins_root.iterdir()):
        manifest = entry / "manifest.py"
        if not entry.is_dir() or not manifest.is_file():
            continue
        values, manifest_warnings = _manifest_literals(manifest)
        warnings.extend(manifest_warnings)
        mod_id = values.get("ID")
        if not isinstance(mod_id, str) or not mod_id:
            mod_id = entry.name
            warnings.append(f"{entry.name}/manifest.py: 缺有效 ID，按目录名上报")
        until, err = validity.parse_endpoint(values.get("VALID_UNTIL"), "VALID_UNTIL")
        if err:
            warnings.append(f"{entry.name}/manifest.py: {err}（按未声明处理）")
        item = {"id": mod_id, "dir": entry.name}
        (expired if validity.is_expired(until, now) else kept).append(item)

    return {"expired": expired, "kept": kept, "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plugins_root", type=Path, help="maaracing_master/plugins 目录")
    parser.add_argument(
        "--validity-module", type=Path, default=None,
        help="core/module_validity.py 路径；缺省按 plugins_root 相对仓库布局推导")
    parser.add_argument("--now", default=None, help="ISO 8601 判定时刻（测试注入用；缺省取当前）")
    args = parser.parse_args(argv)

    if not args.plugins_root.is_dir():
        print(f"plugins_root 不存在: {args.plugins_root}", file=sys.stderr)
        return 3
    validity_path = args.validity_module
    if validity_path is None:
        # 布局：<repo>/maaracing_master/plugins → plugins 的上级即包根，core 在包根下
        validity_path = args.plugins_root.parent / "core" / "module_validity.py"
    if not validity_path.is_file():
        print(f"module_validity 不存在: {validity_path}", file=sys.stderr)
        return 3

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            print(f"--now 不是 ISO 8601: {args.now}", file=sys.stderr)
            return 2

    result = scan(args.plugins_root, validity_path, now)
    for w in result["warnings"]:
        print(f"[ship-exclusion] {w}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
