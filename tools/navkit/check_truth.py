#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit v4 真源自洽校验（CI 闸门，P4b 起承担图/数据面全部机检）。

读盘三件套真源并校验：
1. 图自洽（校验器第 1/5/6 条）：next/on_error 引用闭合、**And/Or 按名子项引用闭合**、
   入口可达、无出口告警、跨锚点重复识别告警——1/5/6 逻辑自 migrate_v4.validate_graph
   原样迁移，And/Or 闭合是 2026-09-11 新增（协议侧只给 next/on_error 做加载期闭合校验，
   And/Or 子项写错名字要到运行期才 `Bad sub ref` 静默判失败，见 and_or_refs 注释）；
2. 数据面可加载：policy.json 经 v4_source 装配（结构性错误 P01-P09 fail-fast）；
3. 两面交叉一致：图 dwell `attach._signals` 引用的锚点、policy spec 里
   stages/transitions 引用的名字必须互洽（编辑任一面时的防脱钩机械检）；
   并且**同一条识别规格在两面必须是同一张照片**——templates 集合相同的图侧参数与
    spec 锚点，逐字段比对 rect/threshold/arbitration/mode↔kind/colorspace
    （anchor_face_checks，2026-09-11 新增）；
4. 几何合法（校验器第 3 条）：两面一切 rect/roi/box 值域 [0,1] 且有序；
5. 分层红线（校验器第 7 条）：core 真源不得占用、也不得引用模块命名空间的
   节点名——协议层节点名全城唯一（无命名空间），"core/plugin 分离"只能靠
   引用方向单向守住，不靠文件摆放位置。"引用"的口径 = 节点里一切按名字指人的
   位置（next/on_error + And/Or 子项 + anchor 对象 value），只堵 next 会留后门。
6. 输入通道（校验器第 8 条）：真源不得用框架内置输入 action（Click/Swipe/Key…）——
   v4 控制器的输入接口全是成功占位，内置 action 会假成功而屏幕上什么都不发生。
7. 纯锚点形态（attach._anchor_only）：豁免「入口不可达」「无出口节点」两条 WARN，
   但必须零路由且被至少一处引用，否则判孤儿真源。
8. 页面清单等集（stage_face_checks）：dwell 顶层 focus ↔ policy stages.order ↔
   stages.definitions 三方互为等集，且 focus 全局唯一——显示态真源已收敛到「当前
   节点」，这三处是同一事实的三个面，协议层不校验一致，漏改即静默漂移。
9. 页面归属闭合（page_checks）：spec 锚点与 stage definitions 的 page 非空，且
   被阶段表/转移引用的锚点 page 不得逃逸出 stage page 集——page 是 Studio 编辑器
   侧分组真源，运行时归属真源是 definitions[*].active/ocr，两者都不得写了没人验。

用法：python tools/navkit/check_truth.py    （纯标准库，CI 零依赖直接运行）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "maaracing_master"
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

    两条：① core 真源里的节点不得占用模块命名空间；② core 节点**任何按名字指人的
    位置**（next/on_error、And/Or 子项、anchor 对象 value）都不得引用模块命名空间的
    节点。方向唯一合法解 = 业务层引用通用层锚点。
    合并单次 post 是协议约束（节点名全城唯一），所以"分离"只能靠引用方向守住。
    ② 的口径 2026-09-11 从"只看 next/on_error"扩到 all_name_refs：And/Or 按名引用
    是真引用（运行期取被引用节点的识别定义），只堵 next 等于给红线留一条后门。
    """
    problems: list[str] = []
    for name, f in origin.items():
        if CORE_PIPELINE_DIR not in f.parents:
            continue
        ns = name.split(".", 1)[0]
        if ns in MODULE_NS:
            problems.append(f"core 真源占用模块命名空间: {name}（{f.name}）→ 应迁往 plugins/{ns}/")
        for ref in all_name_refs(graph[name]):
            if ref.split(".", 1)[0] in MODULE_NS:
                problems.append(
                    f"core 真源引用模块节点: {name} → {ref}（{f.name}）"
                    f"→ 通用层不得点名业务层，改由模块侧声明该边")
    return problems


def ref_name(r: Any) -> str | None:
    """next/on_error 元素 → 节点名；兼容字符串与对象形式 NodeAttr。"""
    if isinstance(r, str):
        return r
    if isinstance(r, dict) and isinstance(r.get("name"), str):
        return r["name"]
    return None


def and_or_refs(node: dict) -> list[str]:
    """节点识别树里所有 And/Or（any_of/all_of）的**按名引用子项**，v1/v2 两种形态都走。

    为什么必须自己闭合（5.12.3 实测，tools/experiments/pipeline-inheritance/）：
      · `any_of: ["不存在的节点"]` → post_pipeline **不报错**，运行期才
        `PipelineTask collect_ocr_from_sub_recognitions: Bad sub ref` +
        `Recognizer::or_ failed to get pipeline data for node` → 整个 Or 判未命中，
        该节点永不进入、任务静默失败。而 next/on_error 里的同类拼错是加载期就拦。
      · 子项里的 `[Anchor]名` **同样按字面节点名解析**（实测 Bad sub ref 原文带
        `[Anchor]` 前缀），锚点晚绑定在这条路上不可用——出现即判死引用。
    框架给的"引用而不复制识别规格"是省真源行数的正路，但它不带闭合校验，
    所以这道闸是把"复制但一定对"换成"引用且仍然可证"的前提。
    """
    out: list[str] = []

    def take(container: Any) -> None:
        if not isinstance(container, dict):
            return
        for key in ("any_of", "all_of"):
            lst = container.get(key)
            if not isinstance(lst, list):
                continue
            for sub in lst:
                if isinstance(sub, str):
                    out.append(sub)
                elif isinstance(sub, dict):
                    walk(sub)

    def walk(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        take(obj)                       # v1：any_of/all_of 平铺在本层
        reco = obj.get("recognition")
        if isinstance(reco, dict):
            take(reco)                  # v2：参数与类型同级
            take(reco.get("param"))     # v2：参数在 param 里
        elif isinstance(reco, list):
            for r in reco:
                walk(r)

    walk(node)
    return out


def all_name_refs(node: dict) -> list[str]:
    """节点里一切"按名字指人"的位置——分层红线的完整口径。

    不含 `roi`/`target` 的字符串形式：那两类指的是"该节点上次跑出来的框"（运行期
    状态，且合法支持 `[Anchor]`），不是静态可闭合的图边。
    """
    refs: list[str] = []
    for key in ("next", "on_error"):
        for r in node.get(key) or []:
            name = ref_name(r)
            if name:
                refs.append(name)
    refs.extend(and_or_refs(node))
    anch = node.get("anchor")
    if isinstance(anch, dict):
        refs.extend(v for v in anch.values() if isinstance(v, str) and v)
    return refs


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


# 框架内置的**输入类** action：一律不得出现在真源里。
# 依据（一手）：① .venv/Lib/site-packages/maa/define.py 的 ActionEnum（MaaFW 5.12.3
# 实际绑定的动作全集）；② core/nav_graph.py WgcapController——v4 的 Tasker 绑的是它
# （宪法 6：帧只从中心缓存来），而它的 click/swipe/touch_*/click_key/input_text/
# key_down/key_up/start_app/stop_app **全部是 `return True` 占位**，真实输入一律走
# MaaRM_Click / MaaRM_Input → Clicker（platform 层）。
# 后果：写了内置输入 action 的节点会**假成功**——框架报执行成功、日志干净、屏幕上
# 一个输入都没发生。这是最难查的一类故障，所以在这里硬拦。
BANNED_BUILTIN_ACTIONS = frozenset({
    "Click", "LongPress", "Swipe", "MultiSwipe", "ClickKey", "LongPressKey",
    "InputText", "Scroll", "TouchDown", "TouchMove", "TouchUp",
    "KeyDown", "KeyUp", "StartApp", "StopApp",
})


def action_type(node: dict) -> str | None:
    """节点 action 的类型名；兼容 v1 字符串（`"action": "Click"`）与
    v2 对象（`"action": {"type": "Custom", "param": {...}}`）两种形态。"""
    a = node.get("action")
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        t = a.get("type")
        return t if isinstance(t, str) else None
    return None


def action_checks(full: dict) -> list[str]:
    """校验器第 8 条：真源里的 action 只能是 Custom（或无副作用的内置类型）。"""
    problems: list[str] = []
    for name, n in full.items():
        t = action_type(n)
        if t in BANNED_BUILTIN_ACTIONS:
            problems.append(
                f"{name}: action={t} 是框架内置输入动作，但 v4 的控制器输入接口全部"
                f"占位返回成功（WgcapController）——节点会假成功而屏幕上什么都不发生"
                f"→ 改用 MaaRM_Click（点识别框中心）或 MaaRM_Input（固定坐标/手柄按键）")
    return problems


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
    # And/Or 按名子项：框架加载期不校验（实测放行），运行期才静默判该 Or 未命中，
    # 所以闭合只能在这里做——这是"把识别规格改成引用"的前提闸门。
    for name, n in full.items():
        for ref in and_or_refs(n):
            hint = "（And/Or 子项按字面节点名解析，[Anchor] 晚绑定在此不可用）" \
                if ref.startswith("[Anchor]") else ""
            if ref not in full:
                problems.append(f"{name}: And/Or 子项悬空引用 {ref}{hint}")
        anch = n.get("anchor")
        if isinstance(anch, dict):
            for a_key, target in anch.items():
                if isinstance(target, str) and target and target not in full:
                    problems.append(f"WARN {name}.anchor[{a_key}] 指向不存在的节点 {target}")
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
    # 纯锚点节点（attach._anchor_only）= 只被引用的识别规格容器，自身不参与路由。
    # 「沿 next 可达」和「有出口」两条对它没有意义，故豁免；取而代之硬要求它**必须
    # 被引用到**——纯锚点的全部存在理由就是被人抄进墙上清单，没人引用就是孤儿真源
    # （历史上那张 hall_race_btn.png 就是这么躺着的）。
    anchors = {n for n, d in full.items() if att(d).get("_anchor_only")}
    unreachable = sorted(set(full) - seen - anchors)
    for u in unreachable:
        problems.append(f"WARN 入口不可达: {u}")
    referenced: set[str] = set()
    for name, n in full.items():
        referenced.update(all_name_refs(n))
    for name in sorted(anchors):
        n = full[name]
        if n.get("next") or n.get("on_error"):
            problems.append(f"{name}: 标了 attach._anchor_only 却带 next/on_error —— "
                            f"纯锚点必须零路由，否则搬进通用层时会点名业务层")
        if name not in referenced:
            problems.append(f"{name}: attach._anchor_only 但无任何节点引用它 —— "
                            f"纯锚点没人抄就是孤儿真源")
    for name, n in full.items():
        a = att(n)
        if a.get("_anchor_only"):
            continue
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


# 两面同义字段的配对（图侧 Custom 识别参数键 ↔ spec 锚点键）
FACE_FIELDS = (("rect", "rect"), ("threshold", "threshold"),
               ("arbitration", "arbitration"), ("mode", "kind"),
               ("colorspace", "colorspace"))


def _face_norm(field: str, val: Any) -> Any:
    """两面同义值归一：rect 收 6 位小数；colorspace 缺省两侧同为 rgb（图侧引擎读
    `p.get("colorspace", "rgb")`，spec 侧本校验器第 4 条认缺省 rgb 合法）。"""
    if field == "rect" and isinstance(val, list) and len(val) == 4:
        return tuple(round(float(x), 6) for x in val)
    if field == "colorspace" and val is None:
        return "rgb"
    return val


def anchor_face_checks(graph: dict, policy: dict) -> tuple[list[str], list[str]]:
    """图 ↔ 数据面「同一条识别规格必须同一张照片」。

    连接键 = templates 集合，不靠节点名与 spec 名的字面巧合（图侧节点名带
    `.r<路线>.<序号>` 链复制后缀，spec 侧是裸锚点名）。同一条规格在图侧会被多处持有
    （互斥模板族由五个回合 dwell 共享、入口锚点与其 dwell 各持一份），本闸先保证
    "抄的都是同一张"，收敛重复是下一步的事。

    分级：rect/threshold/arbitration/mode/colorspace 任一面不等 = 双真源已分叉 → error；
    图侧有、spec 查无此模板集 → error（从此无从比对，等于新开一条无闸规格）。
    colorspace 的口径 2026-09-11 从告警升为拦：定案「默认 gray，灰度拉不开差距才转
    rgb」，盘上两处不一致（round_big_banner、result_banner）已按此统一，两面零分叉。

    例外：attach._graph_only 的节点声明「这条识别规格只在图侧跑、检测面没有对应物」，
    不参与比对。页面/障碍态判据（待机聊天框、控制器弹窗前景）属于此类——它们进检测面
    反而会被阶段裁剪与优先级抢判。豁免靠显式声明，不靠模板恰好查无。
    """
    spec = policy["perception"]["spec"]
    by_tpl: dict[frozenset, list[str]] = {}
    for a_name, a in spec.items():
        tpls = a.get("templates")
        if isinstance(tpls, list) and tpls:
            by_tpl.setdefault(frozenset(tpls), []).append(a_name)

    graph_face: dict[frozenset, list[tuple[str, dict]]] = {}
    for n_name, node in sorted(graph.items()):
        if att(node).get("_graph_only"):
            continue
        for _cn, p in custom_recognitions(node):
            tpls = p.get("templates")
            if isinstance(tpls, list) and tpls:
                graph_face.setdefault(frozenset(tpls), []).append((n_name, p))

    errs: list[str] = []
    warns: list[str] = []
    for tpls, holders in sorted(graph_face.items()):
        label = "+".join(sorted(tpls))
        matched = by_tpl.get(tpls, [])
        if not matched:
            errs.append(f"图侧规格 {label} 在 policy spec 无登记（持有节点 "
                        f"{sorted(h for h, _ in holders)}）→ 两面无从比对")
            continue
        if len(matched) > 1:
            warns.append(f"WARN 模板集 {label} 对应多个 spec 锚点 {sorted(matched)}，"
                         "跳过逐字段比对")
            continue
        a_name, a = matched[0], spec[matched[0]]
        for n_name, p in holders:
            for g_key, a_key in FACE_FIELDS:
                g_v = _face_norm(g_key, p.get(g_key))
                a_v = _face_norm(g_key, a.get(a_key))
                if g_v != a_v:
                    errs.append(f"{a_name} 两面不一致: 图({n_name}).{g_key}={g_v!r} "
                                f"spec.{a_key}={a_v!r}")
    return errs, warns


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


def stage_face_checks(graph: dict, policy: dict) -> list[str]:
    """页面清单三方等集（防脱钩机检，2026-09-13）。

    同一条「这张图有哪些页面」的事实有三处声明：dwell 的 `attach._stage`（图侧声明位）、
    policy stages.order（清单与显示顺序）、stages.definitions（每页的感知配置）。
    协议层不校验它们一致，漏改任一处都是静默漂移——实证：a1a6424 新增两条 dwell
    （待机/控制器指引弹窗）时阶段表仍是 13 项，新页面在 GUI 无处可显。

    闸口径：
    1. 每个 `_dwell` 节点必须有非空 `attach._stage`，且值全局唯一——两页同名会让
       阶段清单与图侧声明无法对应。
    2. {dwell._stage} 与 stages.order 互为等集（多一页/少一页都判错）。
    3. stages.definitions 键集 = stages.order（新页可 active 为空，但 active 为空 +
       无 transitions 入边 = 该页运行时永远判不出，由第 4 条拦）。
    4. order 里每一页都必须有至少一条 `transitions` 入边（`to` 指向它），否则它是
       「清单上有、检测器认不出」的死格——阶段显示会永远跳过这一格。

    注：`_stage` 只是声明位，运行期的阶段真源是 detector 的锚点判定；本闸防的是
    声明与清单脱钩，不是让图侧参与判定。
    """
    problems: list[str] = []
    stages = policy["perception"]["stages"]
    order = list(stages.get("order") or [])
    defs = stages.get("definitions") or {}
    rounds = tuple(s for s in order if s.startswith("第") and "回合" in s)
    declared_targets: set[str] = set()
    for tr in policy["perception"].get("transitions") or []:
        to = tr.get("to")
        if to in ("$round", "same"):
            declared_targets.update(rounds)   # 回合页由 detector 按横幅号实例化
        elif to is not None:
            declared_targets.add(to)

    seen: dict[str, str] = {}
    for name, node in sorted(graph.items()):
        if not att(node).get("_dwell"):
            continue
        stage = att(node).get("_stage")
        if not isinstance(stage, str) or not stage.strip():
            problems.append(f"{name} 是 dwell 但缺非空 attach._stage（图侧页面声明位）")
            continue
        if stage in seen:
            problems.append(f"_stage 重名：{name} 与 {seen[stage]} 都声明「{stage}」")
        seen[stage] = name

    if len(order) != len(set(order)):
        problems.append("stages.order 含重复项")
    declared = set(seen)
    if declared != set(order):
        problems.append(
            "dwell _stage 与 stages.order 不等集"
            f"（只在图: {sorted(declared - set(order))}；只在 order: {sorted(set(order) - declared)}）"
        )
    if set(defs) != set(order):
        problems.append(
            "stages.definitions 键集与 stages.order 不等"
            f"（只在 definitions: {sorted(set(defs) - set(order))}；"
            f"只在 order: {sorted(set(order) - set(defs))}）"
        )
    undetectable = sorted(s for s in order if s not in declared_targets)
    if undetectable:
        problems.append(
            f"order 这些页面无 transitions 入边，运行时永远判不出: {undetectable}"
        )
    return problems


def page_checks(policy: dict) -> list[str]:
    """页面归属闭合（校验器第 9 条，2026-09-15）。

    口径：锚点与阶段的 `page` 是 ROI Studio 编辑器侧的**视觉页分组**真源
    （分组展示 + 新增锚点 E09 校验消费）；运行时的「阶段 → 信号/锚点」归属
    唯一真源是 stages.definitions[*].active/ocr（ADR-0002 真源单一）。
    两条语义轴允许交叉——同一视觉页可服务多个语义阶段（实证：中标结算与
    领取分红是同一张结算页的两个状态，共用 settle_* 信号族），所以本闸不要求
    「信号 page == 所在阶段 page」，只锁分组闭合三件事：

    1. 每个 spec 锚点必须有非空 page（Studio 侧 E09 的事后回锁：E09 只拦新增，
       不锁历史数据与手改 JSON）；
    2. 每个 stage definition 必须有非空 page；
    3. 凡被阶段表（active/ocr）、global_anchors 或 transitions.on 引用的锚点，
       其 page 必须落在 stage page 集内——引用不得逃逸到未定义页面。
       不被任何阶段表/转移引用的惰性锚点（module 直读 spec，如 egg_task 族）
       可用独立页面，不受 3 约束。
    """
    problems: list[str] = []
    spec = policy["perception"]["spec"]
    stages = policy["perception"]["stages"]
    defs = stages.get("definitions") or {}
    stage_pages = {d.get("page") for d in defs.values() if d.get("page")}
    for s, d in defs.items():
        if not d.get("page"):
            problems.append(f"stages.definitions.{s}：缺 page（stage 侧页面声明位）")
    for a_name, a in spec.items():
        if not a.get("page"):
            problems.append(f"spec.{a_name}：缺 page（Studio 分组/E09 依赖）")
    referenced: set[str] = set(stages.get("global_anchors") or [])
    for d in defs.values():
        referenced |= set(d.get("active") or []) | set(d.get("ocr") or [])
    for tr in policy["perception"].get("transitions") or []:
        if tr.get("on"):
            referenced.add(tr["on"])
    for a_name in sorted(referenced & set(spec)):
        p = spec[a_name].get("page")
        if p and p not in stage_pages:
            problems.append(
                f"spec.{a_name}：被阶段表/转移引用，但 page {p!r} 不在 stage page 集 "
                f"{sorted(stage_pages)}（引用不得逃逸到未定义页面）"
            )
    return problems


def main() -> int:
    graph, origin = load_graph()
    errors, warns = validate_graph(graph)
    errors += action_checks(graph)
    errors += namespace_checks(graph, origin)
    policy_doc = None
    try:
        policy_doc = _load(POLICY_TRUTH)
        errors += cross_checks(graph, policy_doc)
        face_errors, face_warns = anchor_face_checks(graph, policy_doc)
        errors += face_errors
        warns += face_warns
        errors += rect_checks(graph, policy_doc)
        errors += stage_face_checks(graph, policy_doc)
        errors += page_checks(policy_doc)
    except (KeyError, TypeError) as exc:
        errors.append(f"policy.json 段结构非法: {exc}")
    if policy_doc is not None:
        sys.path.insert(0, str(REPO))
        try:
            from maaracing_master.core.navkit.v4_source import load_nav_source
            load_nav_source(POLICY_TRUTH)
        except Exception as exc:
            errors.append(f"policy.json 数据面装配失败: {exc}")
    for w in warns:
        print(f"[warn] {w}")
    for e in errors:
        print(f"[error] {e}")
    if errors:
        return 1
    print(f"[check_truth] OK：图 {len(graph)} 节点自洽，policy 数据面可装配且交叉互洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
