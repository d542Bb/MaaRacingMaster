#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一遍现状：同一条识别规格在「图」与「policy spec」两侧到底漂没漂。

目的不是判定对错，是给 check_truth 新增的「两面照片必须相等」校验定严格度——
先知道今天有多少处不一致，才知道这条检查该一上来就锁死、还是只能锁子集。

连接键用 templates 集合（不靠节点名/spec 名的字面巧合）：
  图侧 Custom 识别参数（含 Or/And 分支逐个展开）  ←→  spec 锚点的 templates
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from tools.navkit import check_truth as ct  # noqa: E402


def norm_rect(v):
    if not isinstance(v, list) or len(v) != 4:
        return v
    return tuple(round(float(x), 6) for x in v)


def main() -> int:
    graph, origin = ct.load_graph()
    policy = json.loads(ct.POLICY_TRUTH.read_text(encoding="utf-8"))
    spec = policy["perception"]["spec"]

    by_tpl: dict[frozenset, list[str]] = defaultdict(list)
    for a_name, a in spec.items():
        tpls = a.get("templates")
        if isinstance(tpls, list) and tpls:
            by_tpl[frozenset(tpls)].append(a_name)

    graph_params: dict[frozenset, list[str]] = defaultdict(list)
    for n_name, node in sorted(graph.items()):
        for _cn, p in ct.custom_recognitions(node):
            tpls = p.get("templates")
            if isinstance(tpls, list) and tpls:
                graph_params[frozenset(tpls)].append(n_name)

    print("=== 1) 同一 templates 在图侧被抄了几份 ===")
    for tpls, holders in sorted(graph_params.items(), key=lambda kv: -len(kv[1])):
        if len(holders) > 1:
            print(f"  x{len(holders):2d}  {sorted(tpls)[0]:34s} {holders}")

    print("\n=== 2) 图 ↔ spec 参数比对（templates 集合作连接键）===")
    drift = 0
    for tpls, holders in sorted(graph_params.items()):
        matches = by_tpl.get(tpls, [])
        if not matches:
            print(f"  [图有 spec 无] {sorted(tpls)} ← {holders}")
            drift += 1
            continue
        if len(matches) > 1:
            print(f"  [多锚点同模板] {sorted(tpls)} → spec {matches} ← 图 {holders}")
        a = spec[matches[0]]
        for h in holders:
            for _cn, p in ct.custom_recognitions(graph[h]):
                if frozenset(p.get("templates") or []) != tpls:
                    continue
                for field, spec_key in (("rect", "rect"), ("threshold", "threshold"),
                                        ("colorspace", "colorspace")):
                    g_v, a_v = p.get(field), a.get(spec_key)
                    if spec_key == "rect":
                        g_v, a_v = norm_rect(g_v), norm_rect(a_v)
                    if g_v != a_v:
                        print(f"  [漂] {matches[0]}.{spec_key}: 图({h})={g_v!r} spec={a_v!r}")
                        drift += 1
    print(f"\n  不一致条数 = {drift}")

    print("\n=== 3) spec 里有 templates 但图侧从不出现（正常：检测面独有）===")
    only_spec = [a for a, v in spec.items()
                 if isinstance(v.get("templates"), list) and v["templates"]
                 and frozenset(v["templates"]) not in graph_params]
    print(f"  {len(only_spec)} 个: {only_spec}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
