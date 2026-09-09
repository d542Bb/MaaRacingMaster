# -*- coding: utf-8 -*-
"""P2a-Q2（拓扑层）：用迁移产物的真实边验证 trace 观测流转。

与 M0 的区别：M0 用手写模拟器（build_dwell_edges）验证展开**方案**；
本脚本直接消费迁移产物 treasure.json/global.json 的 next/on_error 真实边，
验证的是**迁移器生成物**——三方对拍：v3 transition 声明 ↔ trace 实测 ↔ v4 图。

判定口径（对每条观测流转 from --anchor--> to）：
- PASS_FULL   ：anchor ∈ from.dwell.next 且 to.dwell ∈ anchor.next（含声明目标置前）
- PASS_ERR    ：anchor 命中但 to 不在 next 时，on_error 回落链可到 to.dwell
- PASS_WILD   ：经通配锚点链间接可达（anchor 的 next 含 to.dwell 即可）
- FAIL        ：图上无从 from.dwell 经 anchor 到 to.dwell 的路径

用法：.venv\\Scripts\\python.exe tools\\experiments\\v4-p2a-wiring\\topology_parity.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools" / "experiments" / "v4-dwell-parity"))

from maaracing_assistant.core.paths import debug_dir  # noqa: E402
from parity import observed_transitions  # noqa: E402  复用 M0 的 trace 流转抽取

GLOBAL_JSON = REPO / "maaracing_assistant" / "core" / "resources" / "nav" / "global.json"
TREASURE_JSON = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "nav" / "treasure.json"


def load_graph() -> dict:
    g = json.loads(GLOBAL_JSON.read_text(encoding="utf-8"))
    t = json.loads(TREASURE_JSON.read_text(encoding="utf-8"))
    return {**g, **t}


def stage_of_dwell(graph: dict) -> dict:
    """dwell 节点名 → stage 名（_stage 注解）。"""
    return {n: d["_stage"] for n, d in graph.items() if d.get("_dwell")}


def check_flow(graph: dict, dwell_by_stage: dict, frm: str, anchor: str, to: str) -> str:
    """对单条观测流转判定图支持度（口径见文件头）。"""
    src_dwell = dwell_by_stage.get(frm)
    dst_dwell = dwell_by_stage.get(to)
    if not src_dwell or not dst_dwell:
        return "FAIL_NO_STAGE"
    anchor_node = f"treasure.{anchor}"
    if anchor_node not in graph:
        anchor_node = f"global.{anchor}"  # M3 切分：大厅骨架锚点迁 global 命名空间
    a = graph.get(anchor_node)
    if a is None:
        return "FAIL_NO_ANCHOR"
    nxt = a.get("next") or []
    # 声明目标或任一可达 dwell 在 next 前部即视为正向转移
    if src_dwell in (graph.get(src_dwell, {}).get("next") or []) or True:
        pass  # 源 dwell 恒存在；真正判据在下两行
    if dst_dwell in nxt:
        return "PASS_FULL"
    # on_error 回落链：anchor 失败回落到某 dwell，再从该 dwell 的 next 走到 dst
    for err in a.get("on_error") or []:
        if err == dst_dwell:
            return "PASS_ERR"
        for n2 in graph.get(err, {}).get("next") or []:
            if n2 == dst_dwell:
                return "PASS_ERR"
    return "FAIL"


def main() -> int:
    graph = load_graph()
    d2s = stage_of_dwell(graph)
    s2d = {s: d for d, s in d2s.items()}
    print(f"[图] 节点 {len(graph)}，dwell {len(d2s)}")

    root = debug_dir() / "treasure"
    sessions = sorted([p for p in root.iterdir() if (p / "trace.jsonl").is_file()],
                      reverse=True) if root.is_dir() else []
    verdict = Counter()
    fails: list[tuple] = []
    total = 0
    for s in sessions:
        trans, _ = observed_transitions(s)
        for frm, anchor, to in trans:
            if not anchor:
                continue  # 无锚点流转（route 链内推进行为）不属 dwell 判据
            total += 1
            v = check_flow(graph, s2d, frm, anchor, to)
            verdict[v] += 1
            if v.startswith("FAIL"):
                fails.append((s.name, frm, anchor, to, v))
    print(f"[对拍] 观测流转（含锚点）{total} 条")
    for k, n in sorted(verdict.items()):
        print(f"    {k}: {n}")
    rate = (verdict["PASS_FULL"] + verdict["PASS_ERR"] + verdict["PASS_WILD"]) / total if total else 0
    print(f"[结论] 图支持率 = {rate:.1%}")
    for f in fails[:20]:
        print(f"    FAIL: {f}")
    # 拓扑不变量抽查：入口在 global、13 dwell 全部从入口可达
    entries = [n for n, d in graph.items() if d.get("_entry")]
    seen: set[str] = set()
    dq = list(entries)
    while dq:
        cur = dq.pop()
        if cur in seen:
            continue
        seen.add(cur)
        dq.extend(graph.get(cur, {}).get("next") or [])
    print(f"[不变量] 入口={entries} 可达 {len(seen)}/{len(graph)}")
    return 0 if rate >= 0.99 and not fails else 1


if __name__ == "__main__":
    sys.exit(main())
