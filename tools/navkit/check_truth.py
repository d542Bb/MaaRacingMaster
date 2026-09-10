#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit v4 真源自洽校验（CI 闸门，P4b 起承担图/数据面全部机检）。

读盘三件套真源并校验：
1. 图自洽（校验器第 1/5/6 条）：next/on_error 引用闭合、入口可达、无出口告警、
   跨锚点重复识别告警——逻辑自 migrate_v4.validate_graph 原样迁移；
2. 数据面可加载：policy.json 经 v4_source 装配（结构性错误 P01-P09 fail-fast）；
3. 两面交叉一致：图 dwell `attach._signals` 引用的锚点、policy spec 里
   stages/transitions 引用的名字必须互洽（编辑任一面时的防脱钩机械检）；
4. 几何合法（校验器第 3 条）：两面一切 rect/roi/box 值域 [0,1] 且有序；
5. 分层红线（校验器第 7 条）：core 真源不得占用、也不得引用模块命名空间的
   节点名——协议层节点名全城唯一（无命名空间），"core/plugin 分离"只能靠
   引用方向单向守住，不靠文件摆放位置。

用法：python tools/navkit/check_truth.py    （纯标准库，CI 零依赖直接运行）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "maaracing_assistant"
# 真源按目录发现（不再硬编码文件名）：core 目录缺席是合法状态（尚无跨模块共用链）。
CORE_PIPELINE_DIR = PACK / "core" / "resources" / "pipeline"
PLUGIN_PIPELINE_DIRS = [PACK / "plugins" / "treasure" / "resources" / "pipeline"]
TREASURE_TRUTH = PLUGIN_PIPELINE_DIRS[0] / "treasure.json"
POLICY_TRUTH = PACK / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"
# 模块命名空间 = plugins/<id>/ 下带 module.py 的目录名（分层红线的判据来源）
MODULE_NS = frozenset(p.name for p in (PACK / "plugins").iterdir()
                      if p.is_dir() and (p / "module.py").is_file())


def att(n: dict) -> dict:
    """节点元数据统一从 attach 读（3.1 唯一文档化扩展位；顶层 `_xxx` 会被框架丢弃）。"""
    return n.get("attach") or {}


def pipeline_files() -> list[Path]:
    files: list[Path] = []
    for d in [CORE_PIPELINE_DIR, *PLUGIN_PIPELINE_DIRS]:
        if d.is_dir():
            files += sorted(p for p in d.rglob("*.json") if not p.name.startswith("."))
    return files


def load_graph() -> tuple[dict, dict]:
    """合并全部 pipeline 真源 → (节点表, 节点→来源文件)。

    同名节点出现在两个文件 = 双真源，直接失败（协议层节点名全城唯一，静默覆盖
    会让其中一份成为死文件）。

    `$` 前缀根级键一律跳过——协议明文「以 $ 开头的 JSON root field 不会被解析」，
    MaaFW 不认它们；MPE 保存时会回写 `$__mpe_config_*`（画布配置）与
    `$__mpe_external_*`（跨文件外部节点占位），校验器必须与框架同口径，否则把
    工具元数据当节点，凭空冒出「入口不可达/无出口」告警与节点数漂移。
    """
    graph: dict = {}
    origin: dict = {}
    for f in pipeline_files():
        for name, node in _load(f).items():
            if name.startswith("$"):
                continue
            if name in origin:
                raise ValueError(f"节点名重复（双真源）: {name} 同见于 {origin[name]} 与 {f}")
            graph[name] = node
            origin[name] = f
    return graph, origin


def namespace_checks(graph: dict, origin: dict) -> list[str]:
    """分层红线：通用层（core）不得点名业务层（plugins/<id>）。

    两条：① core 真源里的节点不得占用模块命名空间；② core 节点的 next/on_error
    不得引用模块命名空间的节点。方向唯一合法解 = 业务层引用通用层锚点。
    合并单次 post 是协议约束（节点名全城唯一），所以"分离"只能靠引用方向守住。
    """
    problems: list[str] = []
    for name, f in origin.items():
        if CORE_PIPELINE_DIR not in f.parents:
            continue
        ns = name.split(".", 1)[0]
        if ns in MODULE_NS:
            problems.append(f"core 真源占用模块命名空间: {name}（{f.name}）→ 应迁往 plugins/{ns}/")
        for key in ("next", "on_error"):
            for r in graph[name].get(key) or []:
                ref = ref_name(r)
                if ref and ref.split(".", 1)[0] in MODULE_NS:
                    problems.append(
                        f"core 真源引用模块节点: {name}.{key} → {ref}（{f.name}）"
                        f"→ 通用层不得点名业务层，改由模块侧声明该边")
    return problems


def ref_name(r: Any) -> str | None:
    """next/on_error 元素 → 节点名；兼容字符串与对象形式 NodeAttr。"""
    if isinstance(r, str):
        return r
    if isinstance(r, dict) and isinstance(r.get("name"), str):
        return r["name"]
    return None


def custom_recognitions(node: dict) -> list[tuple[str | None, dict]]:
    """取节点声明的 (Custom 识别名, 参数表) 列表，**两种协议形态 + Or 分支全兼容**。

    v1 平铺：节点顶层 `custom_recognition` + `custom_recognition_param`；
    v2 归一：`recognition: {type, param:{custom_recognition, custom_recognition_param}}`
    ——MPE 保存时统一按 v2 写回（框架两种都吃）。只认其中一种形态的读取方会在
    MPE 存盘后静默拿到空值（重复识别机检、模板契约断言都会假装通过）。
    Or 分支（如起跑汇聚节点内联全 stage 信号）逐个展开。
    """
    out: list[tuple[str | None, dict]] = []

    def level(n: dict) -> bool:
        p = n.get("custom_recognition_param")
        if isinstance(p, dict):
            out.append((n.get("custom_recognition"), p))
            return True
        return False

    def walk(n: Any) -> None:
        if not isinstance(n, dict) or level(n):
            return
        reco = n.get("recognition")
        if isinstance(reco, dict):
            rp = reco.get("param") or {}
            if level(rp):
                return
            for sub in rp.get("any_of") or []:   # Or 分支递归
                walk(sub)

    walk(node)
    return out


def custom_reco_params(node: dict) -> list[dict]:
    """只要参数表的简写（口径与 custom_recognitions 一致）。"""
    return [p for _name, p in custom_recognitions(node)]


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
    entries = [n for n, d in full.items() if att(d).get("_entry")]
    if not entries:
        problems.append("无 attach._entry 入口节点")
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
        a = att(n)
        if (not n.get("next") and not n.get("on_error")
                and not a.get("_dwell") and not a.get("_policy_loop")):
            problems.append(f"WARN 无出口节点: {name}")
    # 第 6 条：疑似重复识别——**不同基锚**共用同一模板集才报（链复制/dwell 信号
    # 内联与基节点同模板属结构性复制，去基名后自然合并，不告警）。
    def _base_name(nm: str) -> str:
        return nm.split(".r", 1)[0]
    tpl_seen: dict[frozenset, set[str]] = {}
    for name, n in full.items():
        # dwell/confirm/boot 的模板是锚点信号的结构性内联副本（boot = 起跑汇聚，
        # 按设计内联全 stage 信号），不参与重复判定；真正该报的是不同基锚之间共用同一模板。
        if att(n).get("_dwell") or att(n).get("_boot") or ".__confirm." in name:
            continue
        p_list = custom_reco_params(n)
        for p in p_list:
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
    # 图 dwell 的 attach._signals（合格式锚点名）必须能在对应数据面或图中解释
    for name, n in graph.items():
        for sig in att(n).get("_signals") or []:
            short = sig.split(".", 1)[-1]
            if short not in spec and sig not in graph:
                problems.append(f"{name}.attach._signals 引用不存在锚点 {sig}")
    return problems


def rect_checks(graph: dict, policy: dict) -> list[str]:
    """校验器第 3 条：两面一切 `rect`/`roi`/`box` 几何字段——4 元数值、值域
    [0,1]、x1<x2 / y1<y2（归一化矩形统一形；越界 rect 运行时静默错区）。"""
    errs: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("rect", "roi", "box"):
                    label = f"{path}.{k}"
                    if not (isinstance(v, list) and len(v) == 4
                            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
                        errs.append(f"{label} 必须是 4 元数值数组：{v!r}")
                        continue
                    if any(not 0.0 <= float(x) <= 1.0 for x in v):
                        errs.append(f"{label} 值越出 [0,1]：{v!r}")
                    elif not (v[0] < v[2] and v[1] < v[3]):
                        errs.append(f"{label} 必须满足 x1<x2 且 y1<y2：{v!r}")
                else:
                    walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(graph, "graph")
    walk(policy, "policy")
    return errs


def main() -> int:
    graph, origin = load_graph()
    errors, warns = validate_graph(graph)
    errors += namespace_checks(graph, origin)
    for w in warns:
        print(f"[warn] {w}")
    policy_doc = None
    try:
        policy_doc = _load(POLICY_TRUTH)
        errors += cross_checks(graph, policy_doc)
        errors += rect_checks(graph, policy_doc)
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
