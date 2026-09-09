# -*- coding: utf-8 -*-
"""P2b 诊断 v4（一次性）：真实 maa binding 走生产 NavKitV4.load 全链验证。

真源两目录 + 真实 Resource/Tasker bind，验证合并修法在生产函数形态下
加载成功且节点数 = 31。跑完即删。
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from maaracing_assistant.core.nav_graph import NavKitV4

ctx = MagicMock()
v4 = NavKitV4(
    ctx,
    pipeline_dirs=[
        "maaracing_assistant/core/resources/nav",
        "maaracing_assistant/plugins/treasure/resources/nav",
    ],
    image_dirs=["maaracing_assistant/core/resources/image"],
)
ok = v4.load()
print(f"[prod-load] -> {ok}")
if ok:
    nodes = v4._resource.node_list
    print(f"[prod-load] node_list 数量 = {len(nodes)}（期望 32：global 7 + treasure 25 含 boot）")
    print(f"[prod-load] boot 在列 = {'treasure.__boot.dwell' in nodes}")
