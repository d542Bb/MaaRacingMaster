# -*- coding: utf-8 -*-
"""分层红线（ADR 0001）的 Python import 方向机检：core 不得 import 插件。

ADR 0001 的机检（check_truth.namespace_checks）只覆盖 pipeline 真源的按名引用；
Python 层的"core 不 import 业务层"此前无门禁——任何人往 core 加一行
`from maaracing_master.plugins.treasure import ...` 都不会被 CI 拦住。本文件补上
这一条：扫描包内除 plugins/ 外的全部源码 AST，断言无 plugins 子包的 import
（绝对形态与相对形态都查）。

合法方向（业务层 → 通用层）不在本闸范围：插件 import core 由生态契约与
registry 加载顺序保证。模块 id 的字符串引用（如 sidecar 的默认模块常量）也
不在本闸——那是"可接受的引用"（有守卫与回退），红线针对的是代码级依赖。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "maaracing_master"


def _iter_sources():
    for p in sorted(PACK.rglob("*.py")):
        if (PACK / "plugins") in p.parents or "__pycache__" in p.parts:
            continue
        yield p


def _plugin_imports(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "maaracing_master.plugins" \
                        or alias.name.startswith("maaracing_master.plugins."):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # 相对形态：core 模块的 `from ..plugins.x import y` 同样是红线
            if node.level >= 1 and mod.startswith("plugins"):
                hits.append(f"(relative level={node.level}) {mod}")
            elif mod == "maaracing_master.plugins" \
                    or mod.startswith("maaracing_master.plugins."):
                hits.append(mod)
    return hits


def test_core_never_imports_plugins():
    offenders = []
    for p in _iter_sources():
        tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        for name in _plugin_imports(tree):
            offenders.append(f"{p.relative_to(REPO)}: {name}")
    assert not offenders, (
        "core（或包根）import 了插件——分层红线 ADR 0001：依赖方向只能业务层指向"
        "通用层，反向引用应改为插件侧声明该边：\n" + "\n".join(offenders)
    )
