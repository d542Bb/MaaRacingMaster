# -*- coding: utf-8 -*-
"""P2b 诊断 v3：决定性实验——互相引用的两目录合并到临时目录一次 post。

若成功 → 修法定案：NavKitV4.load 运行时合并目录（真源布局不变）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from maa.toolkit import Toolkit

Toolkit.init_option(str(Path(__file__).resolve().parent / "logs"), "")

from maa.resource import Resource  # noqa: E402

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="v4-merge-"))
    for f in (*CORE_NAV.glob("*.json"), *PLUGIN_NAV.glob("*.json")):
        shutil.copy2(f, tmp / f.name)
    print(f"[merge] 合并 {len(list(tmp.glob('*.json')))} 个文件 -> {tmp.name}")

    res = Resource()
    ok = bool(res.post_pipeline(str(tmp)).wait().succeeded)
    print(f"[merged-dir] 一次 post -> {ok}")
    nodes = res.node_list
    print(f"[merged-dir] node_list 数量 = {len(nodes)}（期望 7+23=30）")
    print(f"[tmp] {tmp}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
