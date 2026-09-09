# -*- coding: utf-8 -*-
"""NavKit v3 → v4 迁移器（P1-M1：字段映射 + 最小校验）。

真源格式 = MaaFW pipeline 原生文件根节点映射（无 _schema_ver/nodes 包装，
P0 金样与 loader 双向印证；plan §2 示例包装为 bug #2，本工具按修正后形态产出）。

M1 行为位一致原则：
- template/point 锚点 → Custom(MRA_Template) 节点，param 直接承载 v3 锚点数据
  （guarded_by/arbitration 等以黑盒随行，And/max_hit 等原生统一列为 M2 后续人工动作）；
- ocr 锚点 = 读数传感，不建节点，迁入 policy 表 perception 段；
- 无去向字段整体保留于节点根 `_v3`（extras 透传实证，不静默丢）。

用法：
    .venv\\Scripts\\python.exe tools\\navkit\\migrate_v4.py [--module treasure]
        [--page <v3 page id>] [--out <dir>] [--check-only]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
V3_ASSETS = {
    "treasure": REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "config" / "treasure_assets.json",
}
DEFAULT_OUT = {
    # 真源目录须命名为 pipeline/（mpelb/MSE/MaaMCP 等生态工具按 ProjectInterface
    # 惯例只索引名为 pipeline 的子目录，P3a 一手实证）。迁移器输出与其一致。
    "treasure": REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "pipeline",
}
POLICY_OUT = {
    "treasure": REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "policy",
}
IMAGE_ROOTS = {
    "treasure": REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources",
}


class MigrateError(RuntimeError):
    pass


def _node_name(anchor: str) -> str:
    return f"treasure.{anchor}"


def _check_rect(rect: Any, where: str) -> list[float]:
    if not (isinstance(rect, list) and len(rect) == 4):
        raise MigrateError(f"{where}: rect 必须为 4 元归一化坐标，实为 {rect!r}")
    for v in rect:
        if not (isinstance(v, (int, float)) and 0.0 <= v <= 1.0):
            raise MigrateError(f"{where}: rect 分量越界 [0,1]: {rect!r}")
    return [float(x) for x in rect]


def migrate_anchor(name: str, a: dict, images: set[str], anchors_raw: dict | None = None) -> dict | None:
    """v3 锚点 → v4 节点；ocr 返回 None（进策略表）。

    guard/point 自包含内联（plan §2 节点自包含可复制）：guarded_by 的守卫
    模板/阈值/区域内联进 param.guard，桥不做跨节点查询。
    """
    kind = a.get("kind")
    where = f"anchor {name}"
    if kind == "ocr":
        return None
    if kind not in ("template", "point"):
        raise MigrateError(f"{where}: 无法翻译的 kind={kind!r}（禁止猜译）")
    rect = _check_rect(a.get("rect"), where)
    param: dict[str, Any] = {"mode": kind, "rect": rect}
    # plan §6「默认即可彩色」：模板识别默认改 rgb（彩色），gray 仅按需显式声明。
    # 引擎默认虽已是 rgb，显式落真源消除隐式依赖、供审计/离线脚本读取一致值。
    if kind == "template":
        param.setdefault("colorspace", "rgb")
    templates = a.get("templates") or []
    if kind == "template" and not templates:
        raise MigrateError(f"{where}: template 锚点无模板")
    if templates:
        for t in templates:
            if t not in images:
                raise MigrateError(f"{where}: 模板文件缺失 {t}")
        param["templates"] = list(templates)
    if a.get("threshold") is not None:
        param["threshold"] = a["threshold"]
    if a.get("arbitration") is not None:
        param["arbitration"] = a["arbitration"]
    guard_name = a.get("guarded_by")
    if guard_name is not None:
        param["guarded_by"] = _node_name(guard_name) if guard_name in KNOWN_ANCHORS else guard_name
        g = (anchors_raw or {}).get(guard_name)
        if g is None:
            raise MigrateError(f"{where}: guarded_by 引用悬空 {guard_name!r}")
        g_rect = _check_rect(g.get("rect"), f"guard {guard_name}")
        g_templates = g.get("templates") or []
        if not g_templates:
            raise MigrateError(f"{where}: 守卫锚点 {guard_name!r} 无模板，保险丝失效")
        for t in g_templates:
            if t not in images:
                raise MigrateError(f"{where}: 守卫模板缺失 {t}")
        param["guard"] = {
            "templates": list(g_templates),
            "rect": g_rect,
            **({"threshold": g["threshold"]} if g.get("threshold") is not None else {}),
        }
    if kind == "point":
        # point 的点击目标 = rect 中心（识别走 guard 模板，命中框 ≠ 点击点）
        cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    else:
        cx = cy = None
    MAPPED = ("kind", "rect", "templates", "threshold", "arbitration", "guarded_by",
              "label", "page", "order", "owner")
    residual = {k: v for k, v in a.items() if k not in MAPPED}
    node: dict[str, Any] = {
        "recognition": "Custom",
        "custom_recognition": "MRA_Template",
        "custom_recognition_param": param,
        "action": "Custom",
        "custom_action": "MRA_Click",
    }
    if cx is not None:
        node["custom_action_param"] = {"target": [cx, cy]}
    if a.get("label"):
        node["_label"] = a["label"]
    if a.get("page"):
        node["_page"] = a["page"]
    if a.get("order") is not None:
        node["_order"] = a["order"]
    if a.get("owner"):
        node["_owner"] = a["owner"]
    if residual:
        node["_v3"] = residual
    return node


def migrate_ocr_anchor(name: str, a: dict) -> dict:
    rect = _check_rect(a.get("rect"), f"ocr anchor {name}")
    out: dict[str, Any] = {"rect": rect}
    for k in ("label", "expected", "model", "page", "order"):
        if a.get(k) is not None:
            out[k] = a[k]
    residual = {k: v for k, v in a.items() if k not in ("rect", "label", "expected", "model", "page", "order", "kind", "owner")}
    if residual:
        out["_v3"] = residual
    return out


def collect_images(module: str) -> set[str]:
    root = IMAGE_ROOTS[module]
    return {p.name for p in root.rglob("*.png")} | {p.name for p in root.rglob("*.jpg")}


KNOWN_ANCHORS: set[str] = set()


# ===================== M2：dwell 图展开 / 链 / 全表回判 =====================

def _signal_inline(sig_anchor_name: str, base_nodes: dict) -> dict | None:
    """把信号锚点节点的识别部分抽成 Or 子项内联（复制，不带 action）。"""
    n = base_nodes.get(sig_anchor_name)
    if not n:
        return None
    sub: dict[str, Any] = {
        "recognition": n["recognition"],
        "custom_recognition": n["custom_recognition"],
        "custom_recognition_param": json.loads(json.dumps(n["custom_recognition_param"])),
    }
    return sub


def _dwell_node(stage: str, definition: dict, base_nodes: dict,
                sig_judge: dict[str, set[str]] | None = None) -> dict:
    """stage 驻留节点：识别 = 阶段专属信号 Or 内联（无信号 = DirectHit），动作 DoNothing。

    信号专属化（真机八炸定案）：只保留「transitions 判定阶段含本阶段」的信号，
    未声明判定映射的信号保守保留。专属化后无任何识别信号 = 判定表与锚点集矛盾，
    直接报错（DirectHit dwell 会让 boot 起跑即误命中，必须暴露而非静默）。
    """
    sigs = [f"treasure.{a}" for a in (definition.get("anchors") or [])
            if f"treasure.{a}" in base_nodes
            and (not sig_judge or stage in sig_judge.get(a, {stage}))]
    inlines = [x for x in (_signal_inline(s, base_nodes) for s in sigs) if x]
    if len(inlines) == 1:
        recog = dict(inlines[0])
    elif inlines:
        recog = {"recognition": {"type": "Or", "param": {"any_of": inlines}}}
    elif sig_judge and definition.get("anchors"):
        raise MigrateError(
            f"stage {stage!r} 锚点 {definition['anchors']} 全部不属于本阶段判定（transitions 判定表矛盾）")
    else:
        recog = {"recognition": "DirectHit"}
    node: dict[str, Any] = dict(recog)
    node.update({
        "action": "DoNothing",
        "timeout": -1,
        "rate_limit": 600,
        "_stage": stage,
        "_page": definition.get("page"),
        "_dwell": True,
        "_signals": sigs,
    })
    if definition.get("ocr"):
        node["_ocr_signals"] = definition["ocr"]
    return node


def expand_transitions(doc: dict, base_nodes: dict, stage_order: list[str],
                       dyn_stages: frozenset = frozenset()) -> tuple[dict, dict, dict]:
    """生成 dwell 节点、给可导航锚点接全表回判 next。

    返回 (dwells, anchor_next, anchor_onerror)。
    单脑原则（真机七炸定案）——锚点可点击判据 = 数据驱动：
      clickable = routes.steps.target（显式导航按钮）∪ 非 stage 信号的 transition.on；
      纯信号锚点（hall_session_cards/smart_bid_btn 等）只作 dwell 识别内联，
      点击语义归 policy 决策（图不代点）——否则「点当前高亮卡」这种静态投影
      会替掉动态场次/出价决策，反复点错误元素。
    阶段候选序（boot/回判全表）：决策阶段（dyn_stages）优先——共享信号
    （hall_session_cards 同属大厅/活动页/鉴宝厅信号组）下导航厅会截胡，
    特定阶段前置消歧（真机 run15 取证定案）。
    行为位一致：点击命中后下一帧 = 全阶段信号重判（v3 detector 同款），
    next 稳定序 = 该锚点声明目标优先（stages.order 序），其余按 order。
    """
    defs = doc["stages"]["definitions"]
    route_targets = {st.get("target") for r in (doc.get("routes") or {}).values()
                     for st in (r.get("steps") or []) if st.get("target")}
    signal_set = {a for d in defs.values() for a in (d.get("anchors") or [])}

    # 信号→页面判定映射（v3 transitions 语义：on 的 to =「看到该信号=当前在该阶段」，
    # 真机八炸定案）。dwell 识别信号专属化判据：信号只归属其判定阶段——否则
    # 共享信号（hall_session_cards 同属大厅/活动页信号组）让场次页被「活动页面
    # dwell」截胡，纯导航 next 全表识别误判后 timeout=-1 永久静默。
    sig_judge: dict[str, set[str]] = {}
    for tr in doc.get("transitions") or []:
        on, to = tr.get("on"), tr.get("to")
        if not on:
            continue
        if to in (None, "same", "*"):
            judge = set(stage_order)  # 无判定声明 → 保守保留原状
        elif to == "$round":
            judge = {s for s in stage_order if "回合" in s}
        elif to in stage_order:
            judge = {to}
        else:
            raise MigrateError(f"transition on={on!r} to 引用未定义阶段 {to!r}")
        sig_judge.setdefault(on, set()).update(judge)

    def _clickable(tgt: str) -> bool:
        bare = tgt.removeprefix("treasure.")  # signal/route 集是裸名，tgt 是全名
        return bare in route_targets or bare not in signal_set

    dwells = {f"treasure.{s}.dwell": _dwell_node(s, defs[s], base_nodes, sig_judge)
              for s in stage_order}
    dwell_order = [f"treasure.{s}.dwell" for s in _dyn_first(stage_order, dyn_stages)]
    wilds: list[str] = []
    dwell_next: dict[str, list[str]] = {d: [] for d in dwell_order}
    targets_by_anchor: dict[str, list[str]] = {}
    sources_by_anchor: dict[str, list[str]] = {}
    for tr in doc.get("transitions") or []:
        src, on, to = tr.get("stage"), tr.get("on"), tr.get("to")
        if not on:
            raise MigrateError(f"transition 无 on（M2 不支持无锚点阶段跳转）: {tr!r}")
        tgt = _node_name(on)
        if tgt not in base_nodes:
            raise MigrateError(f"transition.on 引用悬空: {on}")
        if not _clickable(tgt):
            continue  # 纯信号：dwell 识别内联已覆盖，不进点击位
        if src == "*":
            wilds.append(tgt)
            # 通配锚点的声明 to 仍是点击后的优先目标（任意处命中 → 该阶段）
            for s_all in stage_order:
                for tname in _resolve_to(to, s_all, stage_order):
                    targets_by_anchor.setdefault(tgt, []).append(f"treasure.{tname}.dwell")
            continue
        dwell_next[f"treasure.{src}.dwell"] = list(
            dict.fromkeys(dwell_next[f"treasure.{src}.dwell"] + [tgt]))
        for tname in _resolve_to(to, src, stage_order):
            targets_by_anchor.setdefault(tgt, []).append(f"treasure.{tname}.dwell")
        sources_by_anchor.setdefault(tgt, []).append(f"treasure.{src}.dwell")
    for d in dwell_order:  # 通配置后（O1' 缓解①，静态 v3 声明序）
        dwells[d]["next"] = list(dict.fromkeys(dwell_next[d] + wilds))
    anchor_next: dict[str, list[str]] = {}
    anchor_err: dict[str, list[str]] = {}
    for tgt, tnames in targets_by_anchor.items():
        anchor_next[tgt] = list(dict.fromkeys(tnames + dwell_order))
        anchor_err[tgt] = list(dict.fromkeys(sources_by_anchor.get(tgt, [])))
    return dwells, anchor_next, anchor_err


def _resolve_to(to: str | None, src: str, stage_order: list[str]) -> list[str]:
    if to in (None, "same", "*"):
        return [src]
    if to.startswith("$"):
        if to == "$round":
            return [s for s in stage_order if "回合" in s]
        raise MigrateError(f"未支持的动态目标 {to!r}（禁止猜译）")
    if to not in stage_order:
        raise MigrateError(f"transition.to 引用未定义阶段 {to!r}")
    return [to]


def expand_routes(doc: dict, base_nodes: dict, dwell_names: dict,
                  stage_of_anchor: dict[str, str],
                  exit_from_trans: dict[str, set[str]]) -> tuple[dict, dict]:
    """routes → 链：step/confirm 节点为基锚点的自包含复制（plan §2 复制哲学）。

    返回 (chain, starts)，starts = {start_stage: [链头节点名]}，由 build_full 挂进
    对应 dwell.next —— 非 entry 链也由此从阶段驻留可达。
    """
    chain: dict[str, Any] = {}
    starts: dict[str, list[str]] = {}
    for rid, route in (doc.get("routes") or {}).items():
        steps = route.get("steps") or []
        if not steps:
            raise MigrateError(f"route {rid} 无 steps")
        start_stage = route.get("start_stage")
        if start_stage not in dwell_names:
            raise MigrateError(f"route {rid}.start_stage 未定义: {start_stage!r}")
        ids: list[str] = []
        for i, st in enumerate(steps):
            tgt, confirm = st.get("target"), st.get("confirm")
            if st.get("action") != "click":
                raise MigrateError(f"route {rid}.step{i} 非 click 动作未支持: {st.get('action')!r}")
            for key in ("timeout_ms", "rate_limit_ms"):
                if st.get(key) is None:
                    raise MigrateError(f"route {rid}.step{i} 缺 {key}")
            base = base_nodes.get(_node_name(tgt))
            if not base:
                raise MigrateError(f"route {rid}.step{i} target 悬空 {tgt}")
            sid = f"treasure.{tgt}.r{rid}.{i}"
            node = json.loads(json.dumps(base))
            node.pop("next", None)
            node.pop("on_error", None)
            node["_route"] = f"{rid}:{i}"
            node["timeout"] = st["timeout_ms"]
            node["rate_limit"] = st["rate_limit_ms"]
            ids.append(sid)
            chain[sid] = node
            if confirm:
                cbase = base_nodes.get(_node_name(confirm))
                if not cbase:
                    raise MigrateError(f"route {rid}.step{i} confirm 悬空 {confirm}")
                cid = f"treasure.__confirm.{rid}.{i}"
                cnode = {
                    **_signal_inline(_node_name(confirm), base_nodes),
                    "action": "DoNothing",
                    "_route": f"{rid}:{i}.confirm",
                    "_page": cbase.get("_page"),
                }
                ids.append(cid)
                chain[cid] = cnode
        for j in range(len(ids) - 1):
            chain[ids[j]]["next"] = [ids[j + 1]]
        last = ids[-1]
        last_confirm_anchor = (steps[-1].get("confirm") or "")
        exits = exit_from_trans.get(last_confirm_anchor) or set()
        if len(exits) == 1:
            stage = next(iter(exits))
        elif len(exits) > 1:
            raise MigrateError(f"route {rid} 链尾 confirm {last_confirm_anchor!r} "
                               f"transition 出口歧义: {sorted(exits)}")
        else:
            stage = stage_of_anchor.get(last_confirm_anchor, "")
        chain[last]["next"] = [dwell_names[stage]] if stage in dwell_names else []
        if stage not in dwell_names:
            raise MigrateError(f"route {rid} 链尾 confirm {last_confirm_anchor!r} 无法定位出口阶段")
        if route.get("entry"):
            chain[ids[0]]["_entry"] = True
        starts.setdefault(start_stage, []).append(ids[0])
    return chain, starts


def build_v4(module: str, page_filter: str | None) -> tuple[dict, dict]:
    src = V3_ASSETS[module]
    with src.open(encoding="utf-8") as f:
        doc = json.load(f)
    anchors = doc.get("anchors") or {}
    global KNOWN_ANCHORS
    KNOWN_ANCHORS = set(anchors)
    images = collect_images(module)
    nodes: dict[str, Any] = {}
    ocr_sensors: dict[str, Any] = {}
    for name in sorted(anchors):
        a = anchors[name]
        if page_filter and a.get("page") != page_filter:
            continue
        if a.get("kind") == "ocr":
            ocr_sensors[f"{module}.{name}"] = migrate_ocr_anchor(name, a)
            continue
        node = migrate_anchor(name, a, images, anchors_raw=anchors)
        if node is not None:
            nodes[_node_name(name)] = node
    return nodes, ocr_sensors


def validate_nodes(nodes: dict) -> list[str]:
    """v4 最小校验（M1 子集：协议形态/白名单/引用随行）。完整版随 M2 校验器 7 条。"""
    problems: list[str] = []
    for name, n in nodes.items():
        if n.get("recognition") == "Custom" and not n.get("custom_recognition"):
            problems.append(f"{name}: Custom 缺 custom_recognition")
        p = n.get("custom_recognition_param") or {}
        gb = p.get("guarded_by")
        if p.get("mode") == "point" and not (isinstance(gb, str) and gb):
            problems.append(f"{name}: point 节点缺 guard（保险丝）")
        if isinstance(gb, str) and gb not in nodes and not page_scope_partial():
            problems.append(f"{name}: guarded_by 引用悬空 {gb}")
    return problems


_partial_page = False


def page_scope_partial() -> bool:
    return _partial_page


GLOBAL_PAGES = {"hall", "activity"}


def split_global(full: dict) -> tuple[dict, dict, list[tuple[str, str]]]:
    """M3：按 `_page` 切分大厅骨架 → global.* 命名空间，引用改写全局一致。

    跨文件 next 引用由 MaaFW「全部 JSON 载入同一张图」原生支持（plan §2）。
    """
    renames = {n: "global." + n.removeprefix("treasure.")
               for n, d in full.items() if d.get("_page") in GLOBAL_PAGES}

    def map_ref(r: Any) -> Any:
        if isinstance(r, dict):
            out = dict(r)
            if isinstance(out.get("name"), str):
                out["name"] = renames.get(out["name"], out["name"])
            return out
        return renames.get(r, r)

    def rewrite(n: dict) -> dict:
        out = dict(n)
        for key in ("next", "on_error"):
            if isinstance(out.get(key), list):
                out[key] = [map_ref(x) for x in out[key]]
        if isinstance(out.get("_signals"), list):
            out["_signals"] = [map_ref(x) for x in out["_signals"]]
        return out

    glob = {renames[n]: rewrite(d) for n, d in full.items() if n in renames}
    trea = {n: rewrite(d) for n, d in full.items() if n not in renames}
    return glob, trea, sorted(renames.items())


def _dyn_first(stage_order: list[str], dyn_stages: frozenset) -> list[str]:
    """决策阶段（动态 source）前置的 stage 稳定序——共享信号下特定阶段优先命中。"""
    return sorted(stage_order, key=lambda s: s not in dyn_stages)


def _boot_node(module: str, stage_order: list[str], dwells: dict,
               dyn_stages: frozenset = frozenset()) -> dict:
    """起跑汇聚节点：v3「任意 stage 起跑」语义的 v4 形态（P2b 真机三炸根修）。

    route 链头是线性硬路径（主大厅→活动页），游戏不在链头画面时永不命中；
    v3 主循环则是每帧检测 stage 自适应。boot = 全部 stage 信号大 Or、next =
    全部 dwell（stage 序），timeout=-1 未知画面无限驻留等待（v3 同款）。
    不设 _page：M3 切分只搬 hall/activity，boot 属模块、留 plugin 文件。
    """
    inlines = []
    seen: set[str] = set()
    for s in stage_order:
        d = dwells[f"{module}.{s}.dwell"]
        r = d.get("recognition")
        if isinstance(r, dict) and r.get("type") == "Or":
            # 多信号 dwell：Or 对象即 recognition 本体
            subs = r["param"]["any_of"]
        elif r == "Custom" and "custom_recognition" in d:
            # 单信号 dwell（信号专属化后多数阶段）：识别参数在节点顶层
            subs = [{k: d[k] for k in
                     ("recognition", "custom_recognition", "custom_recognition_param")
                     if k in d}]
        else:
            continue  # DirectHit 信号（无识别 stage）不进 boot：起跑即命中会破坏汇聚语义
        for sub in subs:  # 共享模板去重（信号专属化后各页信号已唯一，此处只防历史膨胀）
            key = json.dumps(sub, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                inlines.append(sub)
    return {
        "recognition": {"type": "Or", "param": {"any_of": inlines}},
        "action": "DoNothing",
        "timeout": -1,
        "rate_limit": 600,
        "next": [f"{module}.{s}.dwell" for s in _dyn_first(stage_order, dyn_stages)],
        "_boot": True,
        "_entry": True,
        "_label": "起跑汇聚（任意阶段自适应）",
    }


def build_full(module: str) -> tuple[dict, dict, dict]:
    """M2 全图装配：基节点 + dwell 层 + transition 接线 + route 链 + policy 拆表。"""
    with V3_ASSETS[module].open(encoding="utf-8") as f:
        doc = json.load(f)
    nodes, sensors = build_v4(module, None)
    stage_order = doc["stages"]["order"]
    defs = doc["stages"]["definitions"]
    # 决策阶段判据（数据驱动，真机七炸定案）：rules 带动态 decision.source 的页
    # （session_decision/appraiser/bidding）= 生态无法静态表达 → 点击权归 policy，
    # 图不给链头/可点击位；且 boot/回判全表前置（共享信号截胡消歧）。
    pol = doc.get("policies") or {}
    page_of = {v: k for k, v in (pol.get("stage_map") or {}).items()}
    dyn_pages = {(r.get("when") or {}).get("stage") for r in (pol.get("rules") or [])
                 if (r.get("decision") or {}).get("source")}

    def _stage_page(s: str) -> str | None:
        if s.startswith("第") and "回合出价" in s:
            return "bid"  # 第2-5回合共享 bid 页码（stage_map 只列第1回合）
        return page_of.get(s)

    dyn_stages = frozenset(s for s in stage_order if _stage_page(s) in dyn_pages)
    dwells, anchor_next, anchor_err = expand_transitions(doc, nodes, stage_order, dyn_stages)
    for tgt, nx in anchor_next.items():
        nodes[tgt]["next"] = nx
        if anchor_err.get(tgt):
            nodes[tgt]["on_error"] = anchor_err[tgt]
    dwell_names = {s: f"treasure.{s}.dwell" for s in stage_order}
    stage_of_anchor: dict[str, str] = {}
    for s in stage_order:
        for a in defs[s].get("anchors") or []:
            stage_of_anchor.setdefault(a, s)
    # 链尾出口优先取"以 confirm 为 on 的 transition 声明目标"（到达语义唯一），
    # 阶段归属首见不可靠（同一信号可属于多阶段）。
    exit_from_trans: dict[str, set[str]] = {}
    for tr in doc.get("transitions") or []:
        on = tr.get("on")
        if not on:
            continue
        to = tr.get("to")
        if tr.get("stage") != "*" and to not in (None, "same", "*") and not str(to).startswith("$"):
            exit_from_trans.setdefault(on, set()).add(to)
    chain, starts = expand_routes(doc, nodes, dwell_names, stage_of_anchor, exit_from_trans)
    # 决策阶段的 route 链整体摘除：其 steps 点击（如"开始匹配"）是动态决策的
    # 后置动作（先选场次再匹配由 session_decision 编排），留在图里=死节点+诱惑。
    drop: set[str] = set()
    for stage, heads in starts.items():
        if stage not in dyn_stages:
            continue
        for h in heads:
            cur: str | None = h
            while cur in chain and cur not in drop:
                drop.add(cur)
                nxt = chain[cur].get("next") or []
                cur = nxt[0] if nxt and isinstance(nxt[0], str) else None
    for k in drop:
        chain.pop(k, None)
    starts = {s: hs for s, hs in starts.items() if s not in dyn_stages}
    for stage, heads in starts.items():  # 链头从所属阶段 dwell 可达
        if stage in dyn_stages:
            continue  # 决策阶段不挂导航链头（如鉴宝厅的"开始匹配"——先选场次
            # 再匹配是 session_decision 的动态职责，链头直点会跳过决策）
        d = dwell_names[stage]
        # 链头前置（真机八炸定案）：route 链带精确 confirm（点击后等到达信号），
        # 是 v3 权威导航路径——裸锚点是全表回判兜底，不得抢在链头前命中
        # （否则 confirm 语义被绕过，落进共享信号截胡路径）。
        dwells[d]["next"] = list(dict.fromkeys(heads + (dwells[d].get("next") or [])))
    # Q3b：policy 闭环节点——**局内/流程阶段**的 dwell.next 兜底位（锚点/链头/
    # 通配全未命中时执行一帧决策，[JumpBack] 回 dwell 重判）。对象形式 NodeAttr
    # （v5.1，binding pipeline.py _parse_node_attr_list 实证）。桥注册在
    # plugin（宪法 3）。
    # 单脑原则（真机五/七炸定案，P2b）：纯导航阶段（游戏大厅/活动页面）不挂
    # policy_loop——导航归图锚点，v3 决策脑不得在图外点击。鉴宝大厅(选择场次)
    # 挂：场次选择是动态决策（target_session+彩蛋计算），生态无法用静态图表达，
    # 正是 MRA_Policy 的本职（其信号锚点已从可点击位摘除，图不代点）。
    policy_loop = f"{module}.policy_loop"
    _HALL_STAGES = {"游戏大厅", "活动页面"}
    dwells[policy_loop] = {
        "recognition": "DirectHit",
        "action": "Custom",
        "custom_action": "MRA_Policy",
        "custom_action_param": {"table": f"{module}.policy.json#policy"},
        "next": [],
        "timeout": -1,
        "rate_limit": 300,  # v4 模式帧节律（= v3 FRAME_INTERVAL_MS，桥内跑完整帧工作）
        # 决策循环节拍（P2b 延迟实测定案）：桥内 _tick_once 已自带完整帧工作与
        # 内部节奏，不让框架在动作前后再补 200ms 默认 delay——协议 pre/post_delay
        # 默认 200ms，不显式置 0 则每帧决策白付 400ms 空转（真机帧间隔 1s vs
        # 期望 300ms）。「少用硬 delay」生态实践。
        "pre_delay": 0,
        "post_delay": 0,
        "_policy_loop": True,
    }
    for s, d in dwell_names.items():
        if s in _HALL_STAGES:
            continue
        dwells[d]["next"] = (dwells[d].get("next") or []) + [
            {"name": policy_loop, "jump_back": True}]
    # P2b：起跑汇聚节点（v3「任意 stage 起跑」语义）——生产入口用它，不用链头。
    dwells[f"{module}.__boot.dwell"] = _boot_node(module, stage_order, dwells, dyn_stages)
    # 纯导航/链节点兜底（真机八炸定案）：这些节点的 next 是固定候选（本页锚点/
    # confirm/链尾 dwell），画面意外（点击后页面未按预期切换、弹窗遮挡）时 next
    # 全 miss——timeout=-1 会永久静默卡死（MaaFW 协议：timeout 是「识别本节点
    # next 列表」的窗口）。改为默认 20s 超时 + on_error 回 boot 重判自愈（v3
    # timeout_ms 超时重试语义的 v4 形态）。决策阶段 dwell 保留 -1：policy_loop
    # DirectHit 永远兜底命中，永不超时正是决策循环设计。
    boot_name = f"{module}.__boot.dwell"
    for s, d in dwell_names.items():
        if s in dyn_stages:
            continue
        node = dwells[d]
        node.pop("timeout", None)
        node["on_error"] = [boot_name]
    for node in chain.values():
        node["on_error"] = [boot_name]
    # 决策阶段 dwell 节拍对齐（P2b 延迟实测定案）：决策循环的真正节拍 = policy_loop
    # 的 300ms，决策 dwell 若保持导航 dwell 的 600ms rate_limit 就成了卡帧瓶颈
    # （真机帧间隔 1s，超 policy 预期 3 倍）。把 dyn_stages 的 dwell 降到 300，
    # 与 policy_loop 一致；非决策（纯导航）dwell 保持 600（画面稳定重判频率）。
    for s in dyn_stages:
        dwells[dwell_names[s]]["rate_limit"] = 300
    # 孤岛归类：未被任何 next/on_error/信号/链引用的基节点 = 决策闭环执行资产，
    # 移入 policy.actuators 段（参数随行、引擎按名取用），不占画布（A-1 去向）。
    referenced: set[str] = set()
    for n in {**dwells, **nodes, **chain}.values():
        for key in ("next", "on_error"):
            for r in n.get(key) or []:
                name = ref_name(r)
                if name:
                    referenced.add(name)
    # _signals 是元数据（识别已内联进 dwell），不算图引用——纯信号锚点
    # （hall_session_cards/smart_bid_btn 等）归 actuators（决策执行资产，
    # 真机七炸定案：图不代点，参数按名供 v3 决策栈取用）。
    graph_nodes = {k: v for k, v in nodes.items() if k in referenced or k in chain}
    actuators = {k: nodes[k] for k in sorted(set(nodes) - set(graph_nodes))}
    full = {**dwells, **graph_nodes, **chain}
    stage_signals = {s: {"page": d.get("page"), "anchors": d.get("anchors") or [],
                         "ocr": d.get("ocr") or []} for s, d in defs.items()}
    policy_doc = {
        "perception": {"anchors": sensors, "stage_signals": stage_signals,
                       "tuning": (pol.get("tuning") or {}).get("perception") or {}},
        "actuators": actuators,
        "policy": {"stage_map": pol.get("stage_map") or {}, "rules": pol.get("rules") or [],
                   "tuning": {k: v for k, v in (pol.get("tuning") or {}).items()
                              if k != "perception"}},
        "_v3_schema_ver": pol.get("_schema_ver"),
    }
    audit = {
        "v3": {k: (len(v) if isinstance(v, dict) else len(v)) for k, v in
               [("anchors", doc["anchors"]), ("transitions", doc.get("transitions") or []),
                ("routes", doc.get("routes") or {}), ("stages", defs),
                ("rules", pol.get("rules") or [])]},
        "v4": {"total": len(full),
               "dwell": sum(1 for d in full.values() if d.get("_dwell")),
               "boot": sum(1 for d in full.values() if d.get("_boot")),
               "policy_loop": sum(1 for d in full.values() if d.get("_policy_loop")),
               "chain": len(chain),
               "anchor_graph": len(graph_nodes), "actuators": len(actuators),
               "ocr_sensors": len(sensors)},
        "fanout": sorted(((len(d.get("next") or []), n) for n, d in full.items()),
                         reverse=True)[:6],
        "entry": [n for n, d in full.items() if d.get("_entry")],
        "dyn_stages": sorted(dyn_stages),
    }
    return full, policy_doc, audit


def ref_name(r: Any) -> str | None:
    """next/on_error 元素 → 节点名；兼容字符串与对象形式 NodeAttr。"""
    if isinstance(r, str):
        return r
    if isinstance(r, dict) and isinstance(r.get("name"), str):
        return r["name"]
    return None


def validate_graph(full: dict) -> tuple[list[str], list[str]]:
    """校验器 1/5 条（引用闭合、死胡同/不可达）；3/4/模板存在性已在 M1 层。

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


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="treasure")
    ap.add_argument("--page", default=None, help="仅迁某 v3 page（试迁用）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--full", action="store_true", help="M2 全图 + 正式文件名 + 审计")
    ap.add_argument("--split-global", action="store_true",
                    help="M3：与 --full 连用，大厅骨架切至 global.json")
    args = ap.parse_args()
    global _partial_page
    _partial_page = bool(args.page)
    if args.module not in V3_ASSETS:
        print(f"未知模块 {args.module}（当前仅 treasure）")
        return 2
    out_dir = Path(args.out) if args.out else DEFAULT_OUT[args.module]
    if args.full:
        full, policy_doc, audit = build_full(args.module)
        errors, warns = validate_graph(full)
        print(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
        for w in warns:
            print(f"[warn] {w}")
        for e in errors:
            print(f"[error] {e}")
        if args.check_only:
            return 1 if errors else 0
        if args.split_global:
            glob, trea, renames = split_global(full)
            m_err, m_warn = validate_graph({**glob, **trea})  # 合并图闭合校验
            for e in m_err:
                print(f"[error] {e}")
            for w in m_warn:
                print(f"[warn] {w}")
            errors += m_err
            for old, new in renames:
                print(f"[切分] {old} → {new}")
            core_pipeline = REPO / "maaracing_assistant" / "core" / "resources" / "pipeline"
            dump_json(core_pipeline / "global.json", glob)
            dump_json(out_dir / f"{args.module}.json", trea)
            # policy 表不能与节点图同目录：MaaFW 递归加载目录内所有 json，
            # policy 会被当 pipeline 解析致整目录加载失败（Q1 实验实证）。
            dump_json(POLICY_OUT[args.module] / f"{args.module}.policy.json", policy_doc)
            print(f"[写出] core/resources/pipeline/global.json（{len(glob)}）+ "
                  f"{out_dir}/{args.module}.json（{len(trea)}）+ policy({POLICY_OUT[args.module]})")
            return 1 if errors else 0
        dump_json(out_dir / f"{args.module}.json", full)
        dump_json(POLICY_OUT[args.module] / f"{args.module}.policy.json", policy_doc)
        print(f"[写出] {out_dir}/{args.module}.json（{len(full)} 节点）+ {args.module}.policy.json")
        return 1 if errors else 0
    nodes, ocr = build_v4(args.module, args.page)
    problems = validate_nodes(nodes)
    stats = {"nodes": len(nodes), "ocr_sensors": len(ocr), "problems": problems}
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.check_only or problems:
        return 1 if problems else 0
    suffix = f".{args.page}" if args.page else ""
    dump_json(out_dir / f"{args.module}{suffix}.nodes.preview.json", nodes)
    dump_json(out_dir / f"{args.module}{suffix}.policy.preview.json",
              {"_comment": "M1 试产：感知段；决策规则段随 M2 拆表", "perception": ocr})
    print(f"[写出] {out_dir}/{args.module}{suffix}.nodes.preview.json 等（preview 后缀，正式名随 M2 定稿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
