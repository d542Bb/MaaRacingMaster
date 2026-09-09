# -*- coding: utf-8 -*-
"""M0 实验：v4 dwell 展开（O1 方案）与真实 trace 的离线对拍。

目的（不写生产码，纯数据实验）：
1. 序列覆盖对拍：trace 里观测到的阶段流转，能否在 dwell 展开图上走通；
2. 感知预算估算：各 stage 的 dwell.next 候选数 vs v3 该阶段检测面上界；
3. 拓扑等价抽查：dwell 图上的阶段可达性与 v3 transition 图一致。

用法：.venv\\Scripts\\python.exe tools\\experiments\\v4-dwell-parity\\parity.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_assistant.core.paths import debug_dir  # noqa: E402

ASSETS = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "config" / "treasure_assets.json"
WILDCARD = "*"
ROUND_MARK = "$round"


def load_assets() -> dict:
    with ASSETS.open(encoding="utf-8") as f:
        return json.load(f)


def round_stages(doc: dict) -> list[str]:
    """动态 $round 展开目标：名字含'回合'的阶段。"""
    return [s for s in doc["stages"]["definitions"] if "回合" in s]


def build_dwell_edges(doc: dict) -> tuple[dict, dict]:
    """把 v3 transitions 展开为 dwell 图。

    规则（O1 草案）：
    - 每个 stage S 生成驻留节点 S.dwell；
    - (S, A, T) → dwell 边 (S, A, T)；T='same' 回指 S；T='*' 回指 S；T='$round' 展开为全部回合阶段；
    - ('*', A, T) 通配 → 对**每个** stage S 生成 (S, A, T')，S='*' 的 to 同样解析。
    返回 (出边表 from->set((anchor,to)), 通配边集合)。
    """
    rs = round_stages(doc)
    stages = list(doc["stages"]["definitions"])
    out: dict[str, set[tuple[str, str]]] = {s: set() for s in stages}

    def expand_to(t: str, src: str) -> list[str]:
        if t in ("same", WILDCARD):
            return [src]
        if t == ROUND_MARK or t.startswith("$"):
            return rs
        return [t]

    for tr in doc["transitions"]:
        src, on, dst = tr.get("stage"), tr.get("on"), tr.get("to")
        if not on:
            continue
        if src == WILDCARD:
            for s in stages:
                for t in expand_to(dst, s):
                    out[s].add((on, t))
        else:
            for t in expand_to(dst, src):
                out[src].add((on, t))
    return out, set()


def observed_transitions(session_dir: Path) -> tuple[list, list]:
    """扫 trace 会话：返回 (观测流转三元组列表[(from,anchor,to)], 会话 plan_version 集合)。

    流转 = 相邻两帧 stage 变化；经过锚点 = 变化前最后一次非空 hit_anchor。
    """
    rows: list[dict] = []
    with (session_dir / "trace.jsonl").open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    trans: list[tuple[str, str | None, str]] = []
    prev_stage = None
    last_anchor = None
    versions = set()
    for r in rows:
        v = r.get("plan_version")
        if v:
            versions.add(str(v))
        st = r.get("stage")
        ha = r.get("hit_anchor")
        if prev_stage and st and st != prev_stage:
            trans.append((prev_stage, last_anchor, st))
        if st:
            prev_stage = st
        if ha:
            last_anchor = ha
        elif st and st != prev_stage:
            last_anchor = None
    return trans, versions


def stage_bfs(edges: dict, start: str) -> set[str]:
    seen = {start}
    q = deque([start])
    while q:
        cur = q.popleft()
        for _, t in edges.get(cur, set()):
            if t not in seen:
                seen.add(t)
                q.append(t)
    return seen


def main() -> None:
    doc = load_assets()
    dwell, _ = build_dwell_edges(doc)
    stages = list(doc["stages"]["definitions"])
    first = stages[0]

    # ---- 3. 拓扑抽查：dwell 图从大厅可达全部阶段 ----
    reach = stage_bfs(dwell, first)
    print(f"[拓扑] 自 {first} 可达阶段 {len(reach)}/{len(stages)}"
          + ("" if len(reach) == len(stages) else f"  不可达: {sorted(set(stages) - reach)}"))
    print(f"[拓扑] dwell 出边总数 = {sum(len(v) for v in dwell.values())}")

    # ---- 1. 序列覆盖对拍 ----
    root = debug_dir() / "treasure"
    sess = sorted([p for p in root.iterdir() if (p / "trace.jsonl").is_file()], reverse=True) if root.is_dir() else []
    if not sess:
        print("[trace] 未找到会话，跳过序列对拍")
        return
    total = hit_full = hit_edge = miss = 0
    miss_table = Counter()
    for s in sess:
        trans, versions = observed_transitions(s)
        for frm, anchor, to in trans:
            total += 1
            edges = dwell.get(frm, set())
            tos = {t for _, t in edges}
            if to in tos:
                if anchor and (anchor, to) in edges:
                    hit_full += 1
                else:
                    hit_edge += 1
                    miss_table[(frm, anchor, to)] += 1
            else:
                miss += 1
                miss_table[(frm, anchor, to)] += 1
        print(f"[会话] {s.name}  流转 {len(trans):4d} 次  plan_version={sorted(versions) or ['?']}")
    print(f"[覆盖] 观测流转 {total} 次：精确(边+锚点) {hit_full}，仅边可达 {hit_edge}，图上不可达 {miss}")
    if hit_full + hit_edge:
        print(f"[覆盖] 可解释率 = {(hit_full + hit_edge) / total:.1%}（分母为全部观测流转）")
    for (frm, anchor, to), n in miss_table.most_common(10):
        print(f"    未对上 {n:3d}x  {frm} --{anchor}--> {to}")

    # ---- 2. 感知预算估算 ----
    global_anchors = set(doc["stages"].get("global_anchors") or [])
    defs = doc["stages"]["definitions"]
    wild_cards = sum(1 for tr in doc["transitions"] if tr.get("stage") == WILDCARD)
    print(f"[预算] 通配转移 {wild_cards} 条会进入每个 dwell 的候选。")
    for s in stages:
        fanout = len(dwell[s])
        v3_upper = len(set(defs[s].get("anchors") or []) | set(defs[s].get("ocr") or []) | global_anchors
                       | {a for a, _ in dwell[s]})
        print(f"[预算] {s:12s} dwell.next候选={fanout:3d}  v3检测面上界={v3_upper:3d}")


if __name__ == "__main__":
    main()
