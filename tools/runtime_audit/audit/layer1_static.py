"""Layer 1：Python 静态 import 图。

从 sidecar / 插件 entrypoints 出发，用 AST 递归解析 import / from-import /
importlib.import_module / __import__ 引用，建立 模块->被引模块 的有向关系。

对跨站点包的顶层 package，我们只记录 edge 不无限向下发散（避免 debug、tests
这类大目录把静态图撑爆）。动态 import（importlib 拼接）独立标记，供 classify 使用。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from . import config


class PyStaticGraph:
    def __init__(self, exp_root: Path):
        self.exp_root = exp_root
        self.sidecar_dir = exp_root / "maaracing_master"
        self.packages_dir = exp_root / "runtime" / "python" / "packages"
        # module_key -> set(被引用绝对模块名)
        self.refs: dict[str, set[str]] = {}
        # module_key -> set(importlib.import_module / __import__ 的动态字符串片段)
        self.dynamic: dict[str, set[str]] = {}
        self.errors: list[str] = []

    # ---- 入口 ----
    def build(self) -> None:
        # 从 sidecar 包与插件收集所有 .py（包含 __main__、core、plugins、modules）
        py_files = _collect_py(self.sidecar_dir)
        for f in py_files:
            self._scan_file(f)

    # ---- 扫描单个 .py（AST）----
    def _scan_file(self, path: Path) -> None:
        module_key = _rel_module(path, self.exp_root)
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self.errors.append(f"read {path}: {e}")
            return
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            self.errors.append(f"parse {path}: {e}")
            return
        refs = self.refs.setdefault(module_key, set())
        dyn = self.dynamic.setdefault(module_key, set())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    refs.add(n.name.split(".")[0])  # 只记顶层
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    refs.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # importlib.import_module(...) / importlib.import_module
                if node.func.attr == "import_module" and _is_importlib(node.func):
                    for a in node.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            dyn.add(a.value)
                        elif isinstance(a, ast.JoinedStr):
                            dyn.add("f-string")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                # __import__(...)
                if node.func.id == "__import__":
                    for a in node.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            refs.add(a.value.split(".")[0])
                            dyn.add(a.value)

    # ---- 查询 ----
    def refs_of(self, module_key: str) -> set[str]:
        return self.refs.get(module_key, set())

    def dynamic_of(self, module_key: str) -> set[str]:
        return self.dynamic.get(module_key, set())

    def iterate(self) -> Iterator[tuple[str, set[str], set[str]]]:
        for m, r in self.refs.items():
            yield m, r, self.dynamic.get(m, set())


def _collect_py(root: Path) -> list[Path]:
    out = []
    if not root.exists():
        return out
    for p in root.rglob("*.py"):
        # 跳过 __pycache__
        if "__pycache__" in p.parts:
            continue
        out.append(p)
    return out


def _is_importlib(attr: ast.Attribute) -> bool:
    """判断 node 是否为 importlib(.xxx)? .<attr>，宽松匹配 importlib / importlib.* 。"""
    cur: ast.AST = attr
    parts = []
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    parts.append(getattr(cur, "id", ""))
    return "importlib" in parts


def _rel_module(path: Path, root: Path) -> str:
    """把发行根下的 .py 路径转成模块点路径。"""
    rel = path.relative_to(root).as_posix()
    rel = rel[:-3] if rel.endswith(".py") else rel
    rel = rel.replace("/__init__", "")
    rel = rel.replace("/", ".")
    return rel


def top_package(module: str) -> str:
    """取模块的顶层包名（split('.')[0]）。"""
    return module.split(".")[0]