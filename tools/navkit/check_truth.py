#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit v4 真源自洽校验（CI 闸门；P4b 承接自已退役的 v3→v4 迁移器）。

读盘三件套真源并校验：
1. 图自洽（校验器第 1/5/6 条）：next/on_error 引用闭合、入口可达、无出口告警、
   跨锚点重复识别告警——逻辑自 migrate_v4.validate_graph 原样迁移；
2. 数据面可加载：policy.json 经 v4_source 装配（结构性错误 P01-P09 fail-fast）；
3. 两面交叉一致：图 dwell `_signals` 引用的锚点、policy spec 里 stages/transitions
   引用的名字必须互洽（编辑任一面时的防脱钩机械检）。

用法：python tools/navkit/check_truth.py    （纯标准库，CI 零依赖直接运行）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "maaracing_assistant"
GLOBAL_TRUTH = PACK / "core" / "resources" / "pipeline" / "global.json"
TREASURE_TRUTH = PACK / "plugins" / "treasure" / "resources" / "pipeline" / "treasure.json"
POLICY_TRUTH = PACK / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"


def ref_name(r: Any) -> str | None:
    """next/on_error 元素 → 节点名；兼容字符串与对象形式 NodeAttr。"""
    if isinstance(r, str):
        return r
    if isinstance(r, dict) and isinstance(r.get("name"), str):
        return r["name"]
    return None


def validate_graph(full: dict) -> tuple[list[str], list[str]]:
    """校验器 1/5 条（引用闭合、死胡同/不可达）+ 第 6 条（疑似重复识别）。

    next/on_error 元素兼容字符串与对象形式 NodeAttr（{"name": ..., "jump_back": ...}）。
    """
    problems: list[str] = []
    for name, n in full.items():
        for key in ("next", "on_error"):
            for r in n.get(key) or []:
                ref = ref_name(r)
                if ref is None:
                    problems.append(f"{name}: {key} 元素形态非法 {r!r}")
                elif ref not in full:
                    problems.append(f"{name}: {key} 悬空引用 {ref}")
    entries = [n for n, d in full.items() if d.get("_entry")]
    if not entries:
        problems.append("无 _entry 入口节点")
    seen: set[str] = set()
    dq = list(entries)
    while dq:
        cur = dq.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for r in full.get(cur, {}).get("next") or []:
            name = ref_name(r)
            if name:
                dq.append(name)
    unreachable = sorted(set(full) - seen)
    for u in unreachable:
        problems.append(f"WARN 入口不可达: {u}")
    for name, n in full.items():
        if (not n.get("next") and not n.get("on_error")
                and not n.get("_dwell") and not n.get("_policy_loop")):
            problems.append(f"WARN 无出口节点: {name}")
    # 第 6 条：疑似重复识别——**不同基锚**共用同一模板集才报（链复制/dwell 信号
    # 内联与基节点同模板属结构性复制，去基名后自然合并，不告警）。
    def _base_name(nm: str) -> str:
        return nm.split(".r", 1)[0]
    tpl_seen: dict[frozenset, set[str]] = {}
    for name, n in full.items():
        # dwell/confirm 的模板是锚点信号的结构性内联副本，不参与重复判定；
        # 真正该报的是不同基锚之间共用同一模板。
        if n.get("_dwell") or ".__confirm." in name:
            continue
        p = n.get("custom_recognition_param") or {}
        if p.get("templates"):
            tpl_seen.setdefault(frozenset(p["templates"]), set()).add(_base_name(name))
    for tpls, holders in sorted(tpl_seen.items(), key=lambda kv: -len(kv[1])):
        if len(holders) > 1:
            problems.append(f"WARN 重复识别 {'+'.join(sorted(tpls))} 跨锚点: {sorted(holders)}")
    return [p for p in problems if not p.startswith("WARN")], \
           [p for p in problems if p.startswith("WARN")]


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def cross_checks(graph: dict, policy: dict) -> list[str]:
    """图↔policy 数据面交叉一致（errors 语义：引用不存在的名字 = 运行期静默失效）。"""
    problems: list[str] = []
    spec = policy["perception"]["spec"]
    defs = policy["perception"]["stages"]["definitions"]
    for s, d in defs.items():
        for a in d.get("active") or []:
            if a not in spec:
                problems.append(f"stages.{s}.active 引用不存在锚点 {a}")
        for o in d.get("ocr") or []:
            if o not in spec:
                problems.append(f"stages.{s}.ocr 引用不存在锚点 {o}")
    for tr in policy["perception"].get("transitions") or []:
        if tr.get("on") not in spec:
            problems.append(f"transitions.on 引用不存在锚点 {tr.get('on')}")
    for g in policy["perception"]["stages"].get("global_anchors") or []:
        if g not in spec:
            problems.append(f"global_anchors 引用不存在锚点 {g}")
    # P4c：spec 锚点 colorspace 只认引擎实现的三值（缺省=rgb 合法）
    for a_name, a in spec.items():
        cs = a.get("colorspace")
        if cs is not None and cs not in ("gray", "rgb", "rgb_strict"):
            problems.append(f"spec.{a_name}.colorspace 非法值 {cs!r}（可选 gray/rgb/rgb_strict）")
    # 图 dwell 的 _signals（合格式锚点名）必须能在对应数据面或图中解释
    for name, n in graph.items():
        for sig in n.get("_signals") or []:
            short = sig.split(".", 1)[-1]
            if short not in spec and sig not in graph:
                problems.append(f"{name}._signals 引用不存在锚点 {sig}")
    return problems


def main() -> int:
    graph = {**_load(GLOBAL_TRUTH), **_load(TREASURE_TRUTH)}
    errors, warns = validate_graph(graph)
    for w in warns:
        print(f"[warn] {w}")
    policy_doc = None
    try:
        policy_doc = _load(POLICY_TRUTH)
        errors += cross_checks(graph, policy_doc)
    except (KeyError, TypeError) as exc:
        errors.append(f"policy.json 段结构非法: {exc}")
    if policy_doc is not None:
        sys.path.insert(0, str(REPO))
        try:
            from maaracing_assistant.core.navkit.v4_source import load_nav_source
            load_nav_source(POLICY_TRUTH)
        except Exception as exc:
            errors.append(f"policy.json 数据面装配失败: {exc}")
    for e in errors:
        print(f"[error] {e}")
    if errors:
        return 1
    print(f"[check_truth] OK：图 {len(graph)} 节点自洽，policy 数据面可装配且交叉互洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
