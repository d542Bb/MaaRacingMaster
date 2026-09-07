#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 迁移器——v2 只读判定、v2 → v3 草稿迁移、逐字段等价比对。

纯标准库：不 import cv2 / numpy / maa / vgamepad。

背景
----
v2（`plugins/treasure/resources/config/treasure_rois.json`）是"按用途分段"的扁平结构：
`stage` / `appraisers` / `ocr` / `eggs` / `actions`，每段一个 `rect` + `templates` + 可选
`threshold`。它没有"这块属于哪个页面"、"这个目标靠什么证明画面存在"、"点完去哪"
这三件事——这三件恰好是 v3 的骨架，v2 里一个字都没有。

因此迁移**不可能**是无损自动的。本模块的原则（§7.1）：

    推得出来的照搬（rect/threshold/templates 逐位不动）；
    推不出来的**进缺口清单**，绝不静默造默认值。

`migrate_v2_to_v3` 的第二返回值就是缺口清单，非空是常态，空才是异常——
它是给人过目的，不是给机器吞掉的。

结构
----
    schema_of(doc)                       → 版本号（2 走本模块，3 走 assets.py）
    inspect_v2(doc, image_dirs)          → V2Report（只读体检：段/项/冲突/悬空/孤儿）
    migrate_v2_to_v3(doc, semantic)      → (v3 草稿, 缺口清单)
    diff_v2_v3(old, new)                 → 逐字段差异列表（空 = 等价）

`semantic` 是"人提供的那部分"，唯一输入源是 §2.2 的 Python 常量
（`STAGE_ORDER` / `_GLOBAL_ANCHORS` / `_ROI_STAGE` / `_STAGE_PERCEPTION` / `_STAGE_OCR_KEYS` 等）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .assets import OWNER_GLOBAL, SCHEMA_V3, TEMPLATE_SUFFIXES

__all__ = [
    "V2_SEGMENTS",
    "SEGMENT_META_KEYS",
    "V2Item",
    "V2Report",
    "Gap",
    "schema_of",
    "inspect_v2",
    "migrate_v2_to_v3",
    "diff_v2_v3",
    "load_json",
]

V2_SEGMENTS: tuple[str, ...] = ("stage", "appraisers", "ocr", "eggs", "actions")
"""v2 的五个用途段。"""

SEGMENT_META_KEYS: frozenset[str] = frozenset({"_comment"})
"""段级元数据键（不是 ROI 条目）：`eggs` 与 `appraisers` 段各有一个 `_comment`。

`eggs` 段另有 `_count_dx_norm` / `_count_dy_norm` / `_count_w_norm` / `_count_h_norm`
四个领域参数，它们**不是** ROI，迁移时进 `domain` 袋，不进 anchors。
"""

_EGG_DOMAIN_KEYS: tuple[str, ...] = (
    "_count_dx_norm",
    "_count_dy_norm",
    "_count_w_norm",
    "_count_h_norm",
)

# v2 段的默认 kind 推断规则（仅当 semantic 未显式给出时使用）。
# 凡走推断的一律进缺口清单，标记为「推断待确认」。
_SEGMENT_DEFAULT_KIND: dict[str, str] = {
    "stage": "template",
    "appraisers": "template",
    "eggs": "template",
    "ocr": "ocr",
    "actions": "point",  # actions 段绝大多数无模板图，按「面板内件」处理 → 必须有担保人
}

# 缺口分组前缀。报告里按前缀分组呈现，便于逐批消化。
G_KIND = "未确认/kind"
G_OWNER = "未确认/owner"
G_PAGE = "未确认/page"
G_LABEL = "未确认/label"
G_GUARD = "缺失/guarded_by"
G_COLLISION = "冲突/同名"
G_DANGLING = "悬空/模板"
G_ORPHAN = "孤儿/模板"
G_UNMAPPED = "未映射/语义项"
G_UNKNOWN_REF = "未知引用"
G_MATCH = "缺失/match口径"
G_TRANSITION = "缺失/迁移边"


# ------------------------------------------------------------------
# 载体
# ------------------------------------------------------------------


@dataclass(frozen=True)
class V2Item:
    """v2 的一个 ROI 条目。"""

    segment: str
    key: str
    rect: tuple[float, float, float, float] | None = None
    templates: tuple[str, ...] = ()
    threshold: float | None = None
    prio: int | None = None
    comment: str = ""

    @property
    def qualified(self) -> str:
        """限定名 `段.键`，用于消解跨段同名（如 stage 与 actions 都有 session_start_match_btn）。"""
        return f"{self.segment}.{self.key}"


@dataclass(frozen=True)
class V2Report:
    """v2 只读体检报告（不改动任何东西）。"""

    schema_ver: int
    reference_size: tuple[int, int] | None
    items: tuple[V2Item, ...] = ()
    segment_meta: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    name_collisions: tuple[tuple[str, tuple[str, ...]], ...] = ()
    dangling_templates: tuple[str, ...] = ()
    orphan_templates: tuple[str, ...] = ()

    def items_in(self, segment: str) -> tuple[V2Item, ...]:
        return tuple(i for i in self.items if i.segment == segment)

    def summary(self) -> str:
        lines = [
            f"schema_ver = {self.schema_ver}",
            f"reference_size = {self.reference_size}",
            f"条目合计 {len(self.items)}："
            + "，".join(f"{seg} {len(self.items_in(seg))}" for seg in V2_SEGMENTS),
        ]
        if self.name_collisions:
            lines.append(
                "跨段同名 " + str(len(self.name_collisions)) + " 组："
                + "；".join(f"{k} ← {list(v)}" for k, v in self.name_collisions)
            )
        if self.dangling_templates:
            lines.append(f"悬空模板 {len(self.dangling_templates)}: {list(self.dangling_templates)}")
        if self.orphan_templates:
            lines.append(f"孤儿模板 {len(self.orphan_templates)}: {list(self.orphan_templates)}")
        return "\n".join(lines)


@dataclass(frozen=True, order=True)
class Gap:
    """结构化缺口条目。

    `kind` 是分组前缀（见模块级 `G_*` 常量），`target` 是被指的对象，
    `detail` 说明缺什么、为什么推不出来、需要人做什么。
    """

    kind: str
    target: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.kind}] {self.target}: {self.detail}"


# ------------------------------------------------------------------
# 只读判定与体检
# ------------------------------------------------------------------


def schema_of(doc: Mapping[str, Any]) -> int:
    """读 `_schema_ver`。缺失或非法返回 0（调用方据此拒绝处理，不猜）。"""
    raw = doc.get("_schema_ver")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def inspect_v2(
    doc: Mapping[str, Any],
    *,
    image_dirs: Sequence[Path] = (),
) -> V2Report:
    """v2 只读体检：抽条目、查跨段同名、查模板悬空与孤儿。

    不修改 `doc`，不写任何文件。
    """
    items: list[V2Item] = []
    segment_meta: dict[str, dict[str, Any]] = {}

    for seg in V2_SEGMENTS:
        bucket = doc.get(seg)
        if not isinstance(bucket, Mapping):
            continue
        meta: dict[str, Any] = {}
        for key, val in bucket.items():
            if key.startswith("_"):
                meta[str(key)] = val
                continue
            if not isinstance(val, Mapping):
                continue
            items.append(
                V2Item(
                    segment=seg,
                    key=str(key),
                    rect=_as_rect4(val.get("rect")),
                    templates=_as_str_tuple(val.get("templates")),
                    threshold=_as_float(val.get("threshold")),
                    prio=(val.get("prio") if isinstance(val.get("prio"), int) else None),
                    comment=str(val.get("comment", "")),
                )
            )
        if meta:
            segment_meta[seg] = meta

    # 跨段同名：v2 靠段名消歧，v3 的 anchors 是扁平 map → 必须报出来给人决定怎么改名
    by_key: dict[str, list[str]] = {}
    for item in items:
        by_key.setdefault(item.key, []).append(item.segment)
    collisions = tuple(
        (key, tuple(segs)) for key, segs in sorted(by_key.items()) if len(segs) > 1
    )

    referenced = {t for item in items for t in item.templates}
    on_disk = _scan_templates(image_dirs)

    ref = doc.get("reference_size")
    reference_size = (
        (int(ref[0]), int(ref[1]))
        if isinstance(ref, (list, tuple)) and len(ref) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in ref)
        else None
    )

    return V2Report(
        schema_ver=schema_of(doc),
        reference_size=reference_size,
        items=tuple(items),
        segment_meta=segment_meta,
        name_collisions=collisions,
        dangling_templates=tuple(sorted(referenced - set(on_disk))),
        orphan_templates=tuple(sorted(set(on_disk) - referenced)),
    )


# ------------------------------------------------------------------
# v2 → v3 迁移
# ------------------------------------------------------------------


def migrate_v2_to_v3(
    doc: Mapping[str, Any],
    *,
    semantic: Mapping[str, Any],
) -> tuple[dict, list[str]]:
    """把 v2 文档迁成 v3 草稿，并返回缺口清单。

    参数
    ----
    doc
        v2 JSON（`_schema_ver == 2`）。
    semantic
        人提供的那部分。唯一输入源是 §2.2 的 Python 常量。支持这些键：

            module         : str，模块名（必填）
            order          : list[str]，阶段顺序（STAGE_ORDER）
            global_anchors : list[str]
            pages          : dict[str, {"label": str}]
            match          : dict，唯一匹配口径 {scales, threshold, margin_default}
            anchors        : dict[str, dict]，键用 `段.键`（精确）或 `键`（模糊，遇同名会报冲突）
                             值可含 kind / owner / page / label / guarded_by / order /
                             arbitration / domain / rename（重命名后的 v3 anchor id）
            stage_defs     : dict[阶段名, {"page","anchors","ocr","dynamic_narrow"}]
            transitions    : list[{"stage","on","to","when"}]
            routes         : dict[route 名, {...}]

    返回
    ----
    `(v3_draft, gaps)`：`gaps` 是给人过目的清单（字符串形式，每条 `[分组] 目标: 说明`），
    **非空是常态**。草稿不落运行时路径（§7.2 第 1 条）。
    """
    report = inspect_v2(doc, image_dirs=tuple(semantic.get("image_dirs") or ()))
    gaps: list[Gap] = []

    module = str(semantic.get("module") or "")
    if not module:
        gaps.append(
            Gap(G_UNMAPPED, "semantic.module", "未提供模块名，无法生成 _module")
        )

    semantic_anchors: Mapping[str, Any] = semantic.get("anchors") or {}
    used_semantic_keys: set[str] = set()

    pages = dict(semantic.get("pages") or {})
    anchors: dict[str, dict[str, Any]] = {}
    # v3 anchor id → v2 限定名，供 diff_v2_v3 追溯与人工核对
    provenance: dict[str, tuple[str, str]] = {}
    collision_keys = {k for k, _ in report.name_collisions}

    for item in report.items:
        sem = _lookup_semantic(semantic_anchors, item, collision_keys, gaps)
        if sem is not None:
            used_semantic_keys.add(item.qualified if item.qualified in semantic_anchors else item.key)

        anchor_id = str((sem or {}).get("rename") or item.key)
        if anchor_id in anchors:
            gaps.append(
                Gap(
                    G_COLLISION,
                    f"anchors.{anchor_id}",
                    f"v3 锚点 id 已被 {provenance.get(anchor_id)} 占用，"
                    f"当前条目 {item.qualified} 需通过 semantic 的 rename 改名",
                )
            )
            anchor_id = f"{item.segment}__{item.key}"

        entry: dict[str, Any] = {
            "kind": _resolve_kind(item, sem, gaps),
            "owner": _resolve_owner(item, sem, module, gaps),
            "page": _resolve_page(item, sem, pages, gaps),
            "label": _resolve_label(item, sem, gaps),
            "rect": list(item.rect) if item.rect else None,
        }
        if item.templates:
            entry["templates"] = list(item.templates)
        if item.threshold is not None:
            entry["threshold"] = item.threshold

        # order：semantic 显式给的优先，其次 v2 的 prio（仅 appraisers 段有）。
        # 两者都没有时不写 order（而不是补 0）——顺序是给人排的，编一个等于替人决定。
        sem_order = (sem or {}).get("order")
        if sem_order is not None:
            entry["order"] = int(sem_order)
        elif item.prio is not None:
            entry["order"] = int(item.prio)

        # comment：semantic 的中文说明优先，其次 v2 注释（semantic 通常写得更完整）
        sem_comment = (sem or {}).get("comment")
        if sem_comment:
            entry["comment"] = str(sem_comment)
        elif item.comment:
            entry["comment"] = item.comment

        if entry["kind"] == "point":
            guard = (sem or {}).get("guarded_by")
            if guard:
                entry["guarded_by"] = str(guard)
            else:
                gaps.append(
                    Gap(
                        G_GUARD,
                        f"anchors.{anchor_id}",
                        f"v2 项 {item.qualified} 无模板图、归为 point，"
                        f"但 v2 里没有任何字段能推出它的画面归属，必须由人指定 guarded_by",
                    )
                )
        elif (sem or {}).get("guarded_by"):
            entry["guarded_by"] = str(sem["guarded_by"])

        if (sem or {}).get("arbitration"):
            entry["arbitration"] = dict(sem["arbitration"])
        if (sem or {}).get("domain"):
            entry["domain"] = dict(sem["domain"])

        # 追溯字段：草稿专用元数据，人工复核后由控制台删除；
        # assets._parse_anchors 只取已知字段，会忽略它，不会污染运行时。
        entry["_v2"] = {"segment": item.segment, "key": item.key}

        anchors[anchor_id] = entry
        provenance[anchor_id] = (item.segment, item.key)

    # eggs 段的四个计数偏移是领域参数，挂在 egg 锚点的 domain 上
    _attach_egg_domain(report, anchors, provenance)

    # semantic 里给了但 v2 里没有的锚点语义 → 说明常量与配置已经不同步
    for key in sorted(set(semantic_anchors) - used_semantic_keys):
        gaps.append(
            Gap(G_UNMAPPED, f"semantic.anchors.{key}", "语义项在 v2 中找不到对应条目")
        )

    known = set(anchors)

    # guarded_by 的闭合性：semantic 里指定的担保人必须真存在。
    # 不在这里拦住的话，草稿里会留一条悬空担保，等 S1 接入后由 E12 才发现——
    # 那时已经和"搬迁"混在一起，回归失败无从归因。
    for anchor_id, entry in sorted(anchors.items()):
        guard = entry.get("guarded_by")
        if guard and guard not in known:
            gaps.append(
                Gap(
                    G_UNKNOWN_REF,
                    f"anchors.{anchor_id}.guarded_by",
                    f"担保人 {guard!r} 在迁移结果中不存在"
                    f"（semantic 指向了 v2 里没有的锚点，常量与配置已不同步）",
                )
            )

    stages = _build_stages(semantic, known, gaps)
    transitions = _build_transitions(semantic, known, gaps)
    routes = _build_routes(semantic, known, gaps)
    match = _build_match(semantic, gaps)

    for key, names in (
        ("stages.global_anchors", stages.get("global_anchors") or ()),
    ):
        for name in names:  # type: ignore[union-attr]
            if name not in known:
                gaps.append(
                    Gap(G_UNKNOWN_REF, key, f"指向 v3 中不存在的锚点 {name!r}")
                )

    for tpl in report.dangling_templates:
        gaps.append(
            Gap(G_DANGLING, "templates", f"模板图 {tpl!r} 被 v2 引用但不在模板目录中")
        )
    for tpl in report.orphan_templates:
        gaps.append(
            Gap(G_ORPHAN, "templates", f"模板图 {tpl!r} 在目录中但未被任何 v2 条目引用")
        )
    for key, segs in report.name_collisions:
        gaps.append(
            Gap(
                G_COLLISION,
                f"v2.{key}",
                f"同名条目出现在多个段 {list(segs)}，v3 anchors 是扁平 map，"
                f"必须由人决定重命名或合并",
            )
        )

    v3: dict[str, Any] = {
        "_schema_ver": SCHEMA_V3,
        "_module": module,
        "_generated_by": "navkit.legacy.migrate_v2_to_v3（草稿，待人工复核）",
        "reference_size": list(report.reference_size or [1280, 720]),
        "match": match,
        "pages": pages,
        "anchors": anchors,
        "stages": stages,
        "transitions": transitions,
        "routes": routes,
    }

    return v3, [str(g) for g in sorted(set(gaps))]


# ------------------------------------------------------------------
# 逐字段等价比对
# ------------------------------------------------------------------


def diff_v2_v3(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """逐字段比对 v2 与 v3 草稿：**rect / threshold / templates 必须逐位相同**。

    返回差异描述列表；空列表表示等价。这是 §7.2"纯搬迁，不得改 rect/threshold"
    的机器保证——搬迁提交若顺手改了数值，这里立刻现形，回归失败才能归因。

    对应关系靠 v3 锚点上的 `_v2` 追溯字段（草稿专用）建立；找不到追溯信息的锚点
    按 id 直接匹配 v2 同名条目，仍找不到则跳过（并在结果里说明）。
    """
    report = inspect_v2(old)
    v2_index: dict[tuple[str, str], V2Item] = {
        (i.segment, i.key): i for i in report.items
    }
    anchors: Mapping[str, Any] = new.get("anchors") or {}

    diffs: list[str] = []
    matched: set[tuple[str, str]] = set()

    for anchor_id, entry in anchors.items():
        if not isinstance(entry, Mapping):
            continue
        trace = entry.get("_v2")
        if isinstance(trace, Mapping):
            seg, key = str(trace.get("segment")), str(trace.get("key"))
        else:
            seg, key = "", anchor_id

        item = v2_index.get((seg, key))
        if item is None and key in {k for _, k in v2_index}:
            candidates = [i for i in report.items if i.key == key]
            item = candidates[0] if len(candidates) == 1 else None
        if item is None:
            diffs.append(
                f"anchors.{anchor_id}: v3 锚点在 v2 中找不到对应条目"
                f"（追溯 _v2={seg}.{key}），无法比对"
            )
            continue
        matched.add((item.segment, item.key))

        if _rects_differ(item.rect, entry.get("rect")):
            diffs.append(
                f"anchors.{anchor_id}.rect: v2={item.rect} 与 v3={entry.get('rect')} 不一致"
            )
        v3_th = entry.get("threshold")
        if not _thresholds_equal(item.threshold, v3_th):
            diffs.append(
                f"anchors.{anchor_id}.threshold: v2={item.threshold!r} "
                f"与 v3={v3_th!r} 不一致"
            )
        v3_tpls = tuple(entry.get("templates") or ())
        if tuple(item.templates) != v3_tpls:
            diffs.append(
                f"anchors.{anchor_id}.templates: v2={list(item.templates)} "
                f"与 v3={list(v3_tpls)} 不一致（顺序亦须相同）"
            )

    for (seg, key), item in v2_index.items():
        if (seg, key) in matched:
            continue
        diffs.append(f"v2.{seg}.{key}: v3 草稿中缺失对应锚点")

    return diffs


def load_json(path: str | Path, *, encoding: str = "utf-8") -> dict:
    """读 JSON 文件（纯便捷封装，统一编码与异常文案）。"""
    with open(path, "r", encoding=encoding) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 根节点须为 object，收到 {type(data).__name__}")
    return data


# ------------------------------------------------------------------
# 内部：解析辅助
# ------------------------------------------------------------------


def _as_rect4(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        return None
    return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(v) for v in raw if isinstance(v, str) and v)


def _as_float(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _scan_templates(image_dirs: Sequence[Path]) -> dict[str, Path]:
    """按目录顺序首次命中扫描模板图（与 `Assets.template_files` 同规则）。"""
    out: dict[str, Path] = {}
    for d in image_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in TEMPLATE_SUFFIXES and p.name not in out:
                out[p.name] = p
    return out


# ------------------------------------------------------------------
# 内部：字段求解（推不出来就记缺口）
# ------------------------------------------------------------------


def _lookup_semantic(
    semantic_anchors: Mapping[str, Any],
    item: V2Item,
    collision_keys: set[str],
    gaps: list[Gap],
) -> Mapping[str, Any] | None:
    """按 `段.键` 精确查、再按 `键` 模糊查。

    同名冲突项如果用模糊键命中，记一条提示：v3 里这两个条目已经分家，
    共用一份语义大概率是错的。
    """
    exact = semantic_anchors.get(item.qualified)
    if exact is not None:
        return exact if isinstance(exact, Mapping) else {}
    vague = semantic_anchors.get(item.key)
    if vague is not None:
        if item.key in collision_keys:
            gaps.append(
                Gap(
                    G_COLLISION,
                    f"semantic.anchors.{item.key}",
                    f"该键在 v2 中跨段同名，却只提供了一份模糊语义；"
                    f"请改用限定键 {item.qualified!r} 分别为每个段指定",
                )
            )
        return vague if isinstance(vague, Mapping) else {}
    return None


def _resolve_kind(
    item: V2Item, sem: Mapping[str, Any] | None, gaps: list[Gap]
) -> str:
    given = (sem or {}).get("kind")
    if given:
        return str(given)
    # v2 里唯一的客观线索：actions 段里有模板图的可当 template，否则按段默认值
    if item.segment == "actions" and item.templates:
        inferred = "template"
        why = "actions 段但自带模板图"
    else:
        inferred = _SEGMENT_DEFAULT_KIND.get(item.segment, "template")
        why = f"v2 无 kind 字段，按段 {item.segment!r} 默认值推断"
    gaps.append(
        Gap(G_KIND, f"v2.{item.qualified}", f"{why} → {inferred!r}（推断待确认）")
    )
    return inferred


def _resolve_owner(
    item: V2Item, sem: Mapping[str, Any] | None, module: str, gaps: list[Gap]
) -> str:
    given = (sem or {}).get("owner")
    if given:
        return str(given)
    gaps.append(
        Gap(
            G_OWNER,
            f"v2.{item.qualified}",
            f"v2 无归属概念；默认归本模块 {module!r}，"
            f"若属跨模块共用（如大厅/设置页）须改 {OWNER_GLOBAL!r} 并移动模板图",
        )
    )
    return module


def _resolve_page(
    item: V2Item,
    sem: Mapping[str, Any] | None,
    pages: Mapping[str, Any],
    gaps: list[Gap],
) -> str:
    given = (sem or {}).get("page")
    if given:
        page = str(given)
        if page not in pages:
            gaps.append(
                Gap(G_PAGE, f"v2.{item.qualified}", f"指定 page {page!r} 未在 semantic.pages 中定义")
            )
        return page
    gaps.append(
        Gap(
            G_PAGE,
            f"v2.{item.qualified}",
            "v2 只有用途段（stage/ocr/actions…），没有页面概念；"
            "页面是树的分组层，必须由人划分",
        )
    )
    return ""


def _resolve_label(
    item: V2Item, sem: Mapping[str, Any] | None, gaps: list[Gap]
) -> str:
    given = (sem or {}).get("label")
    if given:
        return str(given)
    gaps.append(
        Gap(
            G_LABEL,
            f"v2.{item.qualified}",
            f"v2 无中文名（只有注释 {item.comment[:24] + '…' if item.comment else '空'}），"
            f"控制台与树视图需要一个中文显示名",
        )
    )
    return item.key


def _attach_egg_domain(
    report: V2Report, anchors: dict[str, dict[str, Any]], provenance: dict[str, tuple[str, str]]
) -> None:
    """把 `eggs` 段的 `_count_*` 领域参数挂到 egg 锚点的 `domain` 上。

    它们是彩蛋计数区偏移（HSV/NMS 之后的「×N」OCR 区），navkit 不解释、只透传。
    """
    meta = report.segment_meta.get("eggs") or {}
    domain = {k: meta[k] for k in _EGG_DOMAIN_KEYS if k in meta}
    if not domain:
        return
    for anchor_id, (seg, key) in provenance.items():
        if seg == "eggs":
            anchors[anchor_id].setdefault("domain", {}).update(domain)


def _build_stages(
    semantic: Mapping[str, Any], known: set[str], gaps: list[Gap]
) -> dict[str, Any]:
    order = [str(s) for s in (semantic.get("order") or [])]
    global_anchors = [str(a) for a in (semantic.get("global_anchors") or [])]
    if not order:
        gaps.append(
            Gap(G_TRANSITION, "stages.order", "semantic 未提供阶段顺序（v2 中不存在该信息）")
        )

    definitions: dict[str, Any] = {}
    for stage, raw in (semantic.get("stage_defs") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        anchors_used = [str(a) for a in (raw.get("anchors") or [])]
        ocr_used = [str(a) for a in (raw.get("ocr") or [])]
        for ref in (*anchors_used, *ocr_used):
            if ref not in known:
                gaps.append(
                    Gap(G_UNKNOWN_REF, f"stages.definitions.{stage}", f"引用了不存在的锚点 {ref!r}")
                )
        definitions[str(stage)] = {
            "page": raw.get("page"),
            "anchors": anchors_used,
            "ocr": ocr_used,
            **({"dynamic_narrow": dict(raw["dynamic_narrow"])} if raw.get("dynamic_narrow") else {}),
        }

    return {"order": order, "global_anchors": global_anchors, "definitions": definitions}


def _build_transitions(
    semantic: Mapping[str, Any], known: set[str], gaps: list[Gap]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(semantic.get("transitions") or []):
        if not isinstance(raw, Mapping):
            continue
        on = str(raw.get("on", ""))
        if on and on not in known:
            gaps.append(
                Gap(G_UNKNOWN_REF, f"transitions[{i}].on", f"引用了不存在的锚点 {on!r}")
            )
        out.append(
            {
                "stage": str(raw.get("stage", "")),
                "on": on,
                "to": str(raw.get("to", "")),
                **({"when": str(raw["when"])} if raw.get("when") else {}),
            }
        )
    if not out:
        gaps.append(
            Gap(
                G_TRANSITION,
                "transitions",
                "v2 完全没有「命中 A → 去阶段 B」的声明（该信息只存在于 Python 常量的"
                "_ROI_STAGE 与 _STAGE_PERCEPTION 里），迁移后必须由人逐条上纸",
            )
        )
    return out


def _build_routes(
    semantic: Mapping[str, Any], known: set[str], gaps: list[Gap]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in (semantic.get("routes") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        steps: list[dict[str, Any]] = []
        for j, s in enumerate(raw.get("steps") or []):
            if not isinstance(s, Mapping):
                continue
            for field_name in ("target", "confirm"):
                ref = s.get(field_name)
                if ref is not None and str(ref) not in known:
                    gaps.append(
                        Gap(
                            G_UNKNOWN_REF,
                            f"routes.{name}.steps[{j}].{field_name}",
                            f"引用了不存在的锚点 {ref!r}",
                        )
                    )
            step: dict[str, Any] = {"target": str(s.get("target", ""))}
            if s.get("action"):
                step["action"] = str(s["action"])
            if s.get("confirm"):
                step["confirm"] = str(s["confirm"])
            for field_name in ("timeout_ms", "rate_limit_ms"):
                if s.get(field_name) is not None:
                    step[field_name] = s[field_name]
            steps.append(step)
        out[str(name)] = {
            **({"entry": bool(raw["entry"])} if raw.get("entry") else {}),
            **({"start_stage": str(raw["start_stage"])} if raw.get("start_stage") else {}),
            "steps": steps,
        }
    if not out:
        gaps.append(
            Gap(
                G_TRANSITION,
                "routes",
                "v2 没有跨页面跳转链概念，"
                "routes 需由人按 §4.5 另行声明",
            )
        )
    return out


def _build_match(semantic: Mapping[str, Any], gaps: list[Gap]) -> dict[str, Any]:
    given = semantic.get("match")
    if isinstance(given, Mapping) and given.get("scales") and given.get("threshold"):
        return dict(given)
    gaps.append(
        Gap(
            G_MATCH,
            "match",
            "v2 没有统一匹配口径：scales 只存在于 Python 常量（鉴宝师/场次/对勾各一套），"
            "threshold 散落在各 ROI 上且缺省值在代码里（MATCH_THRESHOLD）。"
            "v3 要求唯一口径，必须人工拍板 scales/threshold/margin_default；"
            "此处先占位，占位值不得直接进运行时",
        )
    )
    return {"scales": [1.0], "threshold": 0.8, "margin_default": 0.0}


# ------------------------------------------------------------------
# 内部：比对辅助
# ------------------------------------------------------------------


def _rects_differ(a: Sequence[float] | None, b: Any) -> bool:
    """逐位比对 rect：两个都为空视为一致，只有一个为空视为不同。"""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return True
    return tuple(float(v) for v in a) != tuple(float(v) for v in b)


def _thresholds_equal(a: float | None, b: Any) -> bool:
    """threshold 比对：None 与缺失等价。"""
    if b is None:
        return a is None
    if a is None:
        return False
    if isinstance(b, bool) or not isinstance(b, (int, float)):
        return False
    return float(a) == float(b)
