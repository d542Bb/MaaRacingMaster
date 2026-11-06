#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点 JSON 的 ROI 读写：只动 `custom_recognition_param.rect`，保存后跑同一校验器。

写盘纪律：
- 只允许写本索引发现过的 pipeline 文件（路径白名单，防任意文件写入）；
- rect 必须是 4 个 [0,1] 内的有限数且 x1<x2、y1<y2；
- **文本替换**目标 rect 字面量，文件其余字节原样保留（缩进、换行、键序、数字格式）；
- 保存后重新跑 `truth` 的图校验；出现保存前没有的新错误就回滚原字节。
"""
from __future__ import annotations

import difflib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tools.pipeline import truth

RECT_PATH = ("recognition", "param", "custom_recognition_param", "rect")


@dataclass(frozen=True)
class NodeRect:
    file: str                 # 仓库相对 posix 路径（前端回传用）
    node: str
    module: str
    page: str
    label: str
    rect: list[float]
    templates: list[str]
    colorspace: str


def _module_of(path: Path) -> str:
    parts = path.as_posix().split("/")
    if "plugins" in parts:
        i = parts.index("plugins")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "core"


def _rect_of(node: dict) -> list[float] | None:
    cur = node
    for key in RECT_PATH:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if not isinstance(cur, list) or len(cur) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in cur):
        return None
    return [float(v) for v in cur]


def _node_meta(node: dict) -> dict:
    attach = node.get("attach") or {}
    cp = ((node.get("recognition") or {}).get("param") or {}).get(
        "custom_recognition_param") or {}
    return {
        "page": str(attach.get("_page") or ""),
        "label": str(attach.get("_label") or ""),
        "templates": [str(t) for t in (cp.get("templates") or [])],
        "colorspace": str(cp.get("colorspace") or ""),
    }


def normalize_rect(rect) -> list[float]:
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise ValueError("rect 必须是 4 元数组")
    try:
        values = [float(v) for v in rect]
    except (TypeError, ValueError):
        raise ValueError("rect 元素必须是数字") from None
    if not all(math.isfinite(v) for v in values):
        raise ValueError("rect 元素必须是有限数")
    x1, y1, x2, y2 = values
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        raise ValueError("rect 必须位于 [0,1] 且 x1<x2、y1<y2")
    return [round(v, 4) for v in values]


def _replace_rect_text(text: str, node: str, rect: list[float]) -> str | None:
    key = re.search(r'"' + re.escape(node) + r'"\s*:', text)
    if key is None:
        return None
    cp = text.find('"custom_recognition_param"', key.end())
    if cp < 0:
        return None
    lit = re.search(r'"rect"\s*:\s*(\[[^\]]*\])', text[cp:])
    if lit is None:
        return None
    start = cp + lit.start(1)
    end = cp + lit.end(1)
    old = lit.group(1)
    sep = ", " if ", " in old else ","
    values = sep.join(json.dumps(v) for v in rect)
    return text[:start] + "[" + values + "]" + text[end:]


def _dump_with_style(doc: dict, original: str) -> str:
    lines = original.splitlines()
    indent = 2
    for line in lines:
        stripped = line.lstrip(" ")
        if stripped and len(line) - len(stripped) > 0:
            indent = min(8, len(line) - len(stripped))
            break
    newline = "\r\n" if "\r\n" in original else "\n"
    body = json.dumps(doc, ensure_ascii=False, indent=indent)
    if newline != "\n":
        body = body.replace("\n", newline)
    if original.endswith(("\n", "\r\n")):
        body += newline
    return body


def _set_rect_in_doc(doc: dict, node: str, rect: list[float]) -> None:
    target = doc.get(node)
    if not isinstance(target, dict):
        raise ValueError(f"节点不存在: {node}")
    cur = target
    for key in RECT_PATH[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            raise ValueError(f"节点没有自定义归一化矩形: {node}")
        cur = nxt
    cur["rect"] = rect


def validate_repo() -> list[str]:
    """与 `truth.py` 主入口同一组图侧校验（不含已过时的策略表装配）。"""
    try:
        graph, origin = truth.load_graph()
    except (OSError, ValueError) as exc:
        return [f"真源加载失败: {exc}"]
    errors, _ = truth.validate_graph(graph)
    errors += truth.action_checks(graph)
    errors += truth.namespace_checks(graph, origin)
    errors += truth.rect_checks(graph)
    return errors


class NodeIndex:
    def __init__(self, *, extra_dirs: tuple[Path, ...] = (),
                 validator: Callable[[], list[str]] | None = None):
        self.files = {
            f.resolve(): f for f in truth.pipeline_files(extra_dirs)
        }
        self.validator = validator or validate_repo

    def nodes(self) -> list[dict]:
        out: list[dict] = []
        for path in sorted(self.files):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            rel = self._rel(path)
            for name, node in doc.items():
                if name.startswith("$") or not isinstance(node, dict):
                    continue
                rect = _rect_of(node)
                if rect is None:
                    continue
                meta = _node_meta(node)
                out.append(NodeRect(
                    file=rel, node=name, module=_module_of(path),
                    page=meta["page"], label=meta["label"], rect=rect,
                    templates=meta["templates"],
                    colorspace=meta["colorspace"],
                ).__dict__)
        return out

    @staticmethod
    def _rel(path: Path) -> str:
        try:
            return path.resolve().relative_to(truth.REPO).as_posix()
        except ValueError:
            return path.as_posix()

    def _path(self, rel: str) -> Path | None:
        if not isinstance(rel, str) or not rel:
            return None
        target = (truth.REPO / rel).resolve()
        return target if target in self.files else None

    def _validate(self) -> list[str]:
        try:
            return list(self.validator())
        except Exception as exc:  # noqa: BLE001 —— 校验器崩溃按新错误处理，触发回滚
            return [f"校验器异常: {exc!r}"]

    def save(self, rel: str, node: str, rect) -> dict:
        path = self._path(rel)
        if path is None:
            raise ValueError(f"不是可写的真源文件: {rel}")
        try:
            values = normalize_rect(rect)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        original = path.read_bytes().decode("utf-8")
        try:
            doc = json.loads(original)
        except ValueError as exc:
            raise ValueError(f"真源 JSON 非法: {exc}") from None
        if node not in doc:
            raise ValueError(f"节点不存在: {node}")
        old_rect = _rect_of(doc[node])
        if old_rect is None:
            raise ValueError(f"节点没有自定义归一化矩形: {node}")
        if old_rect == values:
            return {"ok": True, "changed": False, "diff": "", "errors": []}

        updated = _replace_rect_text(original, node, values)
        if updated is None:
            _set_rect_in_doc(doc, node, values)
            updated = _dump_with_style(doc, original)
        try:
            json.loads(updated)
        except ValueError as exc:
            raise ValueError(f"改写后的 JSON 非法（未落盘）: {exc}") from None

        before = self._validate()
        path.write_bytes(updated.encode("utf-8"))
        after = self._validate()
        new_errors = [e for e in after if e not in before]
        if new_errors:
            path.write_bytes(original.encode("utf-8"))
            return {"ok": False, "changed": False, "diff": "", "errors": new_errors}
        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), updated.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        return {"ok": True, "changed": True, "diff": diff,
                "errors": [], "rect": values}
