# -*- coding: utf-8 -*-
"""P3a 实证②：MPE+mpelb 保存产物能否过生产加载链（NavKitV4.load / post_pipeline）。

V1 diff 定罪的四类漂移中，B1（Custom 平铺→嵌套对象）与 B2（next 写成
[JumpBack] 语法糖）需运行时裁决。本实验把 MPE 保存后的 global+treasure 放进
独立目录，走真实 maa binding 的 Resource.post_pipeline：
  - 失败 → 语法糖/嵌套形态不被框架接受，需在加载前做归一化（或 MPE 关语法糖）；
  - 成功 → 进一步核对节点数与 policy_loop 引用是否真实可达（防静默吞掉）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_assistant.core.nav_graph import NavKitV4

SRC = Path(r"D:\mpe-poc\workspace\pipeline")
TMP = Path(r"D:\mpe-poc\load-check")


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    for name in ("global.json", "treasure.json"):
        shutil.copy2(SRC / name, TMP / name)
    ctx = MagicMock()
    v4 = NavKitV4(ctx, pipeline_dirs=[str(TMP)], image_dirs=[])
    ok = v4.load()
    print(f"[load] post_pipeline -> {ok}")
    if not ok:
        return 1
    nodes = v4._resource.node_list
    print(f"[load] 节点数 = {len(nodes)}（真源基线 21：global 7 + treasure 14）")
    print(f"[load] policy_loop 在列 = {'treasure.policy_loop' in nodes}")
    print(f"[load] boot 在列 = {'treasure.__boot.dwell' in nodes}")
    stray = [n for n in nodes if n.startswith("$") or "[JumpBack]" in n]
    print(f"[load] 可疑节点（$ 前缀 / [JumpBack] 名）= {stray or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
