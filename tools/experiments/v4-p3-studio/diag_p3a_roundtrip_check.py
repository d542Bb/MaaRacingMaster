# -*- coding: utf-8 -*-
"""P3a 金丝雀：MPE+mpelb round-trip 保真校验（plan v4-p3-studio.md §3 V1）。

用法：
    python diag_p3a_roundtrip_check.py <原真源.json> <MPE保存后.json>

断言三层（全部来自 v4 真源的既有契约）：
  1. 语义等价：json 深度相等（值与结构，与键序无关）；
  2. 键序保真：递归逐 dict 比较 keys 顺序（真源人读人改，键序是文档的一部分）；
  3. 扩展字段：根级 `_` 前缀、`$` 语法糖字段、custom_*_param 黑盒逐项在场。
退出码 0=全绿；1=有丢失/漂移（打印明细）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _walk(a, b, path, issues):
    """递归比较：语义 + 键序。a=原始，b=保存后。"""
    if isinstance(a, dict) and isinstance(b, dict):
        ka, kb = list(a.keys()), list(b.keys())
        missing = [k for k in ka if k not in kb]
        extra = [k for k in kb if k not in ka]
        if missing or extra:
            issues.append(f"键集漂移 {path or '/'}: 丢失={missing} 新增={extra}")
        shared = [k for k in ka if k in kb]
        if shared != [k for k in kb if k in ka]:
            issues.append(f"键序漂移 {path or '/'}: {shared} != {list(b.keys())}")
        for k in shared:
            _walk(a[k], b[k], f"{path}/{k}", issues)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            issues.append(f"数组长度漂移 {path}: {len(a)} -> {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                _walk(x, y, f"{path}[{i}]", issues)
    elif a != b:
        issues.append(f"值漂移 {path}: {a!r} -> {b!r}")


def _collect_special(doc, path, acc):
    """收集 `_`/`$`/custom_*_param 扩展字段路径清单。"""
    if isinstance(doc, dict):
        for k, v in doc.items():
            p = f"{path}/{k}"
            if k.startswith("_") or k.startswith("$") or k.startswith("custom_"):
                acc.append(p)
            _collect_special(v, p, acc)
    elif isinstance(doc, list):
        for i, x in enumerate(doc):
            _collect_special(x, f"{path}[{i}]", acc)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    b = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

    issues: list[str] = []
    _walk(a, b, "", issues)

    sa: list[str] = []
    sb: list[str] = []
    _collect_special(a, "", sa)
    _collect_special(b, "", sb)
    lost = [p for p in sa if p not in set(sb)]
    if lost:
        issues.append(f"扩展字段丢失（{len(lost)}/{len(sa)}）: {lost[:20]}")

    print(f"原文件扩展字段: {len(sa)} 个 | 保存后: {len(sb)} 个")
    if issues:
        print(f"FAIL 共 {len(issues)} 项：")
        for i in issues[:50]:
            print(f"  - {i}")
        return 1
    print("PASS：语义等价 + 键序保真 + 扩展字段全量在场")
    return 0


if __name__ == "__main__":
    sys.exit(main())
