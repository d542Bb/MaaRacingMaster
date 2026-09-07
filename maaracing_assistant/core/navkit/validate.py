#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 校验器——把 §3.3 的规则表（E01-E20 / W01-W07）变成可执行代码。

纯标准库：不 import cv2 / numpy / maa / vgamepad。

规则编号以 docs/NAVKIT_PLAN.md §3.3 为**唯一权威**：本文件里每条检查都在注释里
标了编号，改规则必须同时改文档，反之亦然。

分工（重要）
------------
`assets.py` 负责"文档连对象都构造不出来"的**结构性错误**，构造期即抛 `NavKitError`：

    E01 版本门禁 / E03 reference_size / E04 match 唯一口径 / E06 rect 越界

本模块负责需要跨对象引用才能判定的**语义错误**，收集为 `Issue` 一次性返回：

    E02 / E05 / E07 / E08 / E09 / E10 / E11 / E12 / E13 / E14 / E15
    E16 / E17 / E18 / E19 / E20 / W01-W07

为了让控制台能输出"一张清单"，`safe_load()` 会把第一类错误也转成 Issue，
与语义错误同构。

启动期用法（D1：纸码不一致 → 模块拒绝启动并逐条打印）：

    from .validate import safe_load, assert_valid
    assets, report = safe_load(path)
    assert_valid(assets, report, code_edges=CODE_EDGES)   # 失败抛 NavKitValidationError
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .assets import (
    ANCHOR_KINDS,
    ANY_STAGE,
    OWNER_GLOBAL,
    ROUTE_ACTIONS,
    SPECIAL_TRANSITION_TARGETS,
    TEMPLATE_SUFFIXES,
    Assets,
    NavKitError,
)
from .policy import DEFAULT_FALLBACK_KEY, validate_policy_document

__all__ = [
    "LEVEL_ERROR",
    "LEVEL_WARNING",
    "Issue",
    "Report",
    "NavKitValidationError",
    "validate_assets",
    "validate_compiled",
    "validate_merged",
    "safe_load",
    "assert_valid",
]

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

# 模板名里不允许出现的片段：模板名是"资源键"，不是路径（E07）。
_TEMPLATE_NAME_FORBIDDEN = ("/", "\\", "..")


# ------------------------------------------------------------------
# 结果载体
# ------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Issue:
    """单条校验结果。

    `code` 即 §3.3 的规则编号；`path` 定位到具体字段；`level` 区分 error/warning
    （warning 不阻断启动，但会在控制台与 CI 报告里可见）。
    """

    code: str
    level: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.code}] {self.path}: {self.message}"


@dataclass(frozen=True)
class Report:
    """校验报告。"""

    issues: tuple[Issue, ...] = ()

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.level == LEVEL_ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.level == LEVEL_WARNING)

    @property
    def ok(self) -> bool:
        """无 error 即视为通过（warning 不阻断）。"""
        return not self.errors

    def text(self, *, include_warnings: bool = True) -> str:
        """人类可读报告。无问题时返回一行 OK 摘要。"""
        items: list[Issue] = list(self.issues) if include_warnings else list(self.errors)
        if not items:
            return "校验通过：无问题。"
        lines = [
            f"{i.level.upper():7s} [{i.code}] {i.path}: {i.message}" for i in items
        ]
        head = f"共 {len(self.errors)} 项错误 / {len(self.warnings)} 项告警："
        return head + "\n" + "\n".join(lines)


class NavKitValidationError(RuntimeError):
    """启动期校验失败。D1：纸码不一致 → 模块拒绝启动。"""

    def __init__(self, report: Report) -> None:
        super().__init__(report.text())
        self.report = report


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------


def validate_assets(
    assets: Assets,
    *,
    code_edges: Iterable[tuple[str, str]] | None = None,
    global_assets: Assets | None = None,
) -> Report:
    """校验一份 schema v3 资产集。

    参数
    ----
    assets
        待校验的资产集（模块自己的）。
    code_edges
        运行时阶段处理器声明"我实现了哪些边"的集合，形如 `{("匹配中", "appraiser_title")}`。
        传入后启用 E17/E18 纸码双向互查（D1 机制）；为 `None` 时跳过这两条，
        保持校验器对运行时的零依赖。
    global_assets
        `owner=global` 的资产集。传入后启用 W07（模块同名覆盖 global 但未显式声明）。
    """
    issues: list[Issue] = []
    known = set(assets.anchors)

    issues.extend(_check_module(assets))                      # E02
    issues.extend(_check_anchors(assets))                     # E05 E07 E08 E09
    issues.extend(_check_guards(assets, known))               # E10 E13
    issues.extend(_check_stages(assets, known))               # E12 E14 E15 E16
    issues.extend(_check_transitions(assets, known))          # E12 E19
    issues.extend(_check_routes(assets, known))               # E11 E12
    issues.extend(_check_code_edges(assets, code_edges))      # E17 E18
    issues.extend(_check_compiled(assets))                    # E20
    issues.extend(_check_templates(assets))                   # W01 W02 W04
    issues.extend(_check_warnings(assets, global_assets))     # W03 W05 W06 W07
    issues.extend(_check_policies(assets))                    # P01-P09

    return Report(issues=tuple(sorted(set(issues))))


def validate_compiled(
    compiled: Mapping[str, Any],
    assets: Assets,
) -> Report:
    """校验编译产物与源资产的一致性（§6 可审计性）。

    检查两件事：

    1. 头部必须带 `_generated` 与 `source_hash`，且 `source_hash` 与源资产的 sha256
       前 8 位一致（生成物被手改 → CI 失败）；
    2. 产物里的节点名不得与另一条 route 生成的节点名冲突（E20）。

    源资产 hash 由调用方写入产物头部；本函数只做比对，不重算（重算需要读源文件，
    而编译期与校验期可能不在同一进程）。
    """
    issues: list[Issue] = []

    if not compiled.get("_generated"):
        issues.append(
            Issue("E20", LEVEL_ERROR, "_generated", "编译产物缺少 _generated 标记，疑为手改")
        )
    expected = compiled.get("source_hash")
    actual = assets.source_hash
    if expected is not None and actual is not None and expected != actual:
        issues.append(
            Issue(
                "E20",
                LEVEL_ERROR,
                "source_hash",
                f"产物来源 hash {expected!r} 与源资产 {actual!r} 不一致，产物已过期或被手改",
            )
        )

    names = list(compiled.get("node_names") or [])
    seen: set[str] = set()
    for name in names:
        if name in seen:
            issues.append(
                Issue("E20", LEVEL_ERROR, f"node_names.{name}", "编译后 MAA 节点名冲突")
            )
        seen.add(name)

    return Report(issues=tuple(sorted(set(issues))))


def validate_merged(assets_list: Sequence[Assets]) -> Report:
    """跨模块合并后的节点名查重（E20 的真实触发点）。

    单份资产内 `routes` 是 dict、step 序号在 route 内唯一，因此节点名天然不冲突；
    冲突只发生在**合并编译**时——例如同一模块在两处各有一份资产（错挂/重复加载），
    `module::route::step#::target` 就会四条全同。节点名带模块前缀正是为了防这个，
    所以真撞了说明归属出了问题，必须启动失败而不是在 MAA 里静默覆盖。
    """
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    for assets in assets_list:
        origin = assets.source_path.name if assets.source_path else f"<{assets.module}>"
        for name in assets.compilation_node_names():
            if name in seen:
                issues.append(
                    Issue(
                        "E20",
                        LEVEL_ERROR,
                        f"merged::{name}",
                        f"节点名同时由 {seen[name]} 与 {origin} 生成（跨模块合并冲突）",
                    )
                )
            else:
                seen[name] = origin
    return Report(issues=tuple(sorted(set(issues))))


def safe_load(path: str | Path, **kwargs: Any) -> tuple[Assets | None, Report]:
    """加载 + 校验，把结构性错误也转成 Issue，供控制台输出一张清单。

    返回 `(assets, report)`：加载失败时 `assets is None`，`report` 里是那条 E0x。
    """
    try:
        assets = Assets.load(path, **kwargs)
    except NavKitError as exc:
        return None, Report(
            issues=(Issue(exc.code, LEVEL_ERROR, exc.path, exc.message),)
        )
    return assets, validate_assets(assets, **_validate_kwargs(kwargs))


def assert_valid(
    assets: Assets | None,
    report: Report,
    *,
    code_edges: Iterable[tuple[str, str]] | None = None,
) -> Assets:
    """启动期断言：有 error 即抛 `NavKitValidationError`（逐条打印，D1）。

    `assets is None`（加载就失败）时同样抛，保证调用方无需再判空。
    """
    merged = report
    if assets is not None and code_edges is not None:
        merged = _merge(report, validate_assets(assets, code_edges=code_edges))
    if assets is None or not merged.ok:
        raise NavKitValidationError(merged)
    return assets


# ------------------------------------------------------------------
# 分组检查
# ------------------------------------------------------------------


def _check_module(assets: Assets) -> list[Issue]:
    """E02：`_module` 与所在 `plugins/<id>` 目录不一致（防错挂模块）。"""
    declared = assets.declared_module
    if declared is None:
        return [
            Issue("E02", LEVEL_ERROR, "_module", "缺少 _module 声明，无法确认归属模块")
        ]
    if declared != assets.module:
        return [
            Issue(
                "E02",
                LEVEL_ERROR,
                "_module",
                f"声明为 {declared!r}，但文件位于 plugins/{assets.module}/ 下",
            )
        ]
    return []


def _check_anchors(assets: Assets) -> list[Issue]:
    """E05 kind 合法 / E07 模板名合法 / E08 owner 合法 / E09 page 已定义。"""
    issues: list[Issue] = []
    for aid, anchor in assets.anchors.items():
        base = f"anchors.{aid}"

        # E05：kind 必须在枚举内
        if anchor.kind not in ANCHOR_KINDS:
            issues.append(
                Issue(
                    "E05",
                    LEVEL_ERROR,
                    f"{base}.kind",
                    f"kind 需为 {sorted(ANCHOR_KINDS)} 之一，收到 {anchor.kind!r}",
                )
            )

        # E08：owner 只能是 global 或本模块名
        if anchor.owner not in (OWNER_GLOBAL, assets.module):
            issues.append(
                Issue(
                    "E08",
                    LEVEL_ERROR,
                    f"{base}.owner",
                    f"owner 需为 {OWNER_GLOBAL!r} 或 {assets.module!r}，收到 {anchor.owner!r}",
                )
            )

        # E09：page 必须在 pages 里定义（树分组完整性）
        if anchor.page not in assets.pages:
            issues.append(
                Issue(
                    "E09",
                    LEVEL_ERROR,
                    f"{base}.page",
                    f"page {anchor.page!r} 未在 pages 中定义（已知：{sorted(assets.pages)}）",
                )
            )

        # E07：kind=template 必须给非空模板；模板名必须是"资源键"而非路径
        if anchor.kind == "template" and not anchor.templates:
            issues.append(
                Issue(
                    "E07",
                    LEVEL_ERROR,
                    f"{base}.templates",
                    "kind=template 必须提供非空 templates",
                )
            )
        for tpl in anchor.templates:
            if any(bad in tpl for bad in _TEMPLATE_NAME_FORBIDDEN):
                issues.append(
                    Issue(
                        "E07",
                        LEVEL_ERROR,
                        f"{base}.templates",
                        f"模板名 {tpl!r} 含路径分隔符或 '..'，模板名必须是资源键",
                    )
                )
            elif not tpl.lower().endswith(TEMPLATE_SUFFIXES):
                issues.append(
                    Issue(
                        "E07",
                        LEVEL_ERROR,
                        f"{base}.templates",
                        f"模板名 {tpl!r} 扩展名需为 {TEMPLATE_SUFFIXES} 之一",
                    )
                )

    return issues


def _check_guards(assets: Assets, known: set[str]) -> list[Issue]:
    """E10 point 必须有担保人 / E12 担保人必须存在 / E13 担保人必须是 template。"""
    issues: list[Issue] = []
    for aid, anchor in assets.anchors.items():
        base = f"anchors.{aid}"
        if anchor.kind != "point":
            # 非 point 也可以带 guarded_by（例如给 template 目标加双保险），
            # 但只有 point 是"没有担保人就不许存在"（E10）。
            if anchor.guarded_by and anchor.guarded_by not in known:
                issues.append(
                    Issue(
                        "E12",
                        LEVEL_ERROR,
                        f"{base}.guarded_by",
                        f"指向不存在的锚点 {anchor.guarded_by!r}",
                    )
                )
            continue

        if not anchor.guarded_by:
            issues.append(
                Issue(
                    "E10",
                    LEVEL_ERROR,
                    f"{base}.guarded_by",
                    "kind=point 必须声明 guarded_by（固定坐标目标需证明画面此刻存在）",
                )
            )
            continue

        if anchor.guarded_by not in known:
            issues.append(
                Issue(
                    "E12",
                    LEVEL_ERROR,
                    f"{base}.guarded_by",
                    f"指向不存在的锚点 {anchor.guarded_by!r}",
                )
            )
            continue

        guardian = assets.anchors[anchor.guarded_by]
        if guardian.kind != "template":
            issues.append(
                Issue(
                    "E13",
                    LEVEL_ERROR,
                    f"{base}.guarded_by",
                    f"担保人 {anchor.guarded_by!r} 的 kind 为 {guardian.kind!r}，"
                    f"必须是 template（面板内件不能由另一个面板内件担保）",
                )
            )

    return issues


def _check_stages(assets: Assets, known: set[str]) -> list[Issue]:
    """E12 引用闭合 / E14 防幽灵阶段 / E15 order 唯一非空 / E16 dynamic_narrow 禁伪表达式。"""
    issues: list[Issue] = []

    # E15：order 非空且唯一（GUI 断点与 StageTracker 契约）
    if not assets.stage_order:
        issues.append(
            Issue("E15", LEVEL_ERROR, "stages.order", "order 不得为空")
        )
    seen: set[str] = set()
    for stage in assets.stage_order:
        if stage in seen:
            issues.append(
                Issue("E15", LEVEL_ERROR, "stages.order", f"阶段 {stage!r} 重复出现")
            )
        seen.add(stage)

    # E12：global_anchors 必须指向已存在的锚点
    for name in assets.global_anchors:
        if name not in known:
            issues.append(
                Issue(
                    "E12",
                    LEVEL_ERROR,
                    "stages.global_anchors",
                    f"指向不存在的锚点 {name!r}",
                )
            )

    # E14：definitions 的键必须都在 order 里（防幽灵阶段）。
    # 反向（order 里的阶段缺 definitions）是**允许的**，由 W05 告警，不得升级为 error。
    for name in assets.stage_defs:
        if name not in seen:
            issues.append(
                Issue(
                    "E14",
                    LEVEL_ERROR,
                    f"stages.definitions.{name}",
                    f"阶段 {name!r} 未出现在 stages.order 中",
                )
            )

    for name, stage_def in assets.stage_defs.items():
        base = f"stages.definitions.{name}"
        for ref in (*stage_def.anchors, *stage_def.ocr):
            if ref not in known:
                issues.append(
                    Issue("E12", LEVEL_ERROR, base, f"引用了不存在的锚点 {ref!r}")
                )
        by = stage_def.dynamic_narrow.get("by")
        if by is not None and not str(by).startswith("code:"):
            issues.append(
                Issue(
                    "E16",
                    LEVEL_ERROR,
                    f"{base}.dynamic_narrow.by",
                    f"需以 'code:' 开头（只留指针，禁止在 JSON 里写伪表达式），收到 {by!r}",
                )
            )

    return issues


def _check_transitions(assets: Assets, known: set[str]) -> list[Issue]:
    """E12 on 指向存在锚点 / E19 to 目标合法。"""
    issues: list[Issue] = []
    legal_targets = set(assets.stage_order) | SPECIAL_TRANSITION_TARGETS

    for i, tr in enumerate(assets.transitions):
        base = f"transitions[{i}]"
        if tr.on not in known:
            issues.append(
                Issue("E12", LEVEL_ERROR, f"{base}.on", f"指向不存在的锚点 {tr.on!r}")
            )
        elif assets.anchors[tr.on].kind != "template":
            issues.append(
                Issue(
                    "E12",
                    LEVEL_ERROR,
                    f"{base}.on",
                    f"迁移信号锚点 {tr.on!r} 的 kind 为 "
                    f"{assets.anchors[tr.on].kind!r}，必须是 template",
                )
            )
        if tr.to not in legal_targets:
            issues.append(
                Issue(
                    "E19",
                    LEVEL_ERROR,
                    f"{base}.to",
                    f"目标 {tr.to!r} 不在 stages.order 中，也非 "
                    f"{sorted(SPECIAL_TRANSITION_TARGETS)} 之一",
                )
            )
        if tr.stage != ANY_STAGE and tr.stage not in assets.stage_order:
            issues.append(
                Issue(
                    "E19",
                    LEVEL_ERROR,
                    f"{base}.stage",
                    f"源阶段 {tr.stage!r} 不在 stages.order 中（如需任意阶段请用 {ANY_STAGE!r}）",
                )
            )
    return issues


def _check_routes(assets: Assets, known: set[str]) -> list[Issue]:
    """E11 click/press 必填 confirm / E12 target+confirm 引用闭合。"""
    issues: list[Issue] = []
    for rname, route in assets.routes.items():
        for j, step in enumerate(route.steps):
            base = f"routes.{rname}.steps[{j}]"
            if step.action not in ROUTE_ACTIONS:
                issues.append(
                    Issue(
                        "E11",
                        LEVEL_ERROR,
                        f"{base}.action",
                        f"action 需为 {sorted(ROUTE_ACTIONS)} 之一，收到 {step.action!r}",
                    )
                )
            if step.target not in known:
                issues.append(
                    Issue("E12", LEVEL_ERROR, f"{base}.target", f"指向不存在的锚点 {step.target!r}")
                )
            elif assets.anchors[step.target].kind not in ("template", "point"):
                issues.append(
                    Issue(
                        "E12",
                        LEVEL_ERROR,
                        f"{base}.target",
                        f"跳转目标 {step.target!r} 的 kind 为 "
                        f"{assets.anchors[step.target].kind!r}，必须是 template 或 point",
                    )
                )
            if step.action in ("click", "press") and not step.confirm:
                issues.append(
                    Issue(
                        "E11",
                        LEVEL_ERROR,
                        f"{base}.confirm",
                        f"action={step.action!r} 的步骤必须提供 confirm（跳转必须可证伪）",
                    )
                )
            if step.confirm and step.confirm not in known:
                issues.append(
                    Issue(
                        "E12",
                        LEVEL_ERROR,
                        f"{base}.confirm",
                        f"指向不存在的锚点 {step.confirm!r}",
                    )
                )
    return issues


def _check_code_edges(
    assets: Assets, code_edges: Iterable[tuple[str, str]] | None
) -> list[Issue]:
    """E17 纸上有边代码无 / E18 代码有边纸上无（D1 纸码互查）。

    纸上声明的边取 `(stage, on)`；`stage` 为通配 `*` 时不参与互查——它表示
    "任意阶段"，与代码里某条具体边不是一对一关系，强行比对只会产生噪声。
    """
    if code_edges is None:
        return []
    declared = {(tr.stage, tr.on) for tr in assets.transitions if tr.stage != ANY_STAGE}
    implemented = set(code_edges)
    issues: list[Issue] = []

    for stage, on in sorted(declared - implemented):
        issues.append(
            Issue(
                "E17",
                LEVEL_ERROR,
                "transitions",
                f"纸上有边、代码未实现：({stage!r}, {on!r})",
            )
        )
    for stage, on in sorted(implemented - declared):
        issues.append(
            Issue(
                "E18",
                LEVEL_ERROR,
                "CODE_EDGES",
                f"代码已实现、纸上未声明：({stage!r}, {on!r})",
            )
        )
    return issues


def _check_compiled(assets: Assets) -> list[Issue]:
    """E20：编译后 MAA 节点名冲突（两条 route 生成同名节点）。"""
    issues: list[Issue] = []
    seen: set[str] = set()
    for name in assets.compilation_node_names():
        if name in seen:
            issues.append(
                Issue("E20", LEVEL_ERROR, f"routes::{name}", "编译后 MAA 节点名冲突")
            )
        seen.add(name)
    return issues


def _check_templates(assets: Assets) -> list[Issue]:
    """W01 模板未被引用 / W02 引用了不存在的模板 / W04 归属与物理位置矛盾。"""
    issues: list[Issue] = []
    if not assets.image_dirs:
        return issues  # 未注入模板目录时无法判定，静默跳过（不制造假告警）

    on_disk = assets.template_files()
    referenced = set(assets.referenced_templates())

    # W02：锚点引用的模板文件不存在
    for name in assets.referenced_templates():
        if name not in on_disk:
            issues.append(
                Issue(
                    "W02",
                    LEVEL_WARNING,
                    "templates",
                    f"锚点引用的模板图 {name!r} 在 image_dirs 中不存在（悬空引用）",
                )
            )

    # W01：模板存在但未被任何锚点引用
    for name in sorted(set(on_disk) - referenced):
        issues.append(
            Issue(
                "W01",
                LEVEL_WARNING,
                "templates",
                f"模板图 {name!r} 存在但未被任何锚点引用",
            )
        )

    # W04：owner=global 但模板图只存在于模块目录
    if len(assets.image_dirs) >= 2:
        global_dir = assets.image_dirs[0]
        for aid, anchor in assets.anchors.items():
            if anchor.owner != OWNER_GLOBAL:
                continue
            for tpl in anchor.templates:
                resolved = assets.resolve_template(tpl)
                if resolved is not None and global_dir not in resolved.parents:
                    issues.append(
                        Issue(
                            "W04",
                            LEVEL_WARNING,
                            f"anchors.{aid}.templates",
                            f"owner=global 但模板图 {tpl!r} 只存在于模块目录"
                            f"（{resolved.parent}），归属与物理位置矛盾",
                        )
                    )
    return issues


def _check_warnings(assets: Assets, global_assets: Assets | None) -> list[Issue]:
    """W03 global_anchors 为空 / W05 阶段无 definitions / W06 同页 order 重复 / W07 隐式覆盖。"""
    issues: list[Issue] = []

    # W03：global_anchors 为空（阶段冻结事故，不变量 I-1）
    if not assets.global_anchors:
        issues.append(
            Issue(
                "W03",
                LEVEL_WARNING,
                "stages.global_anchors",
                "global_anchors 为空：异常掉回大厅时可能检测不到回退信号，导致阶段冻结",
            )
        )

    # W05：order 里的阶段缺 definitions（允许，但告警；不得升级为 error）
    for stage in assets.stage_order:
        if stage not in assets.stage_defs:
            issues.append(
                Issue(
                    "W05",
                    LEVEL_WARNING,
                    f"stages.definitions.{stage}",
                    "该阶段无 definitions，运行时回退全量检测（既有安全兜底，但感知未被裁剪）",
                )
            )

    # W06：同一 page 内锚点 order 重复（检测与展示顺序歧义）
    per_page: dict[str, dict[int, list[str]]] = {}
    for anchor in assets.anchors.values():
        if anchor.order is None:
            continue
        per_page.setdefault(anchor.page, {}).setdefault(anchor.order, []).append(anchor.id)
    for page, by_order in per_page.items():
        for order_val, ids in sorted(by_order.items()):
            if len(ids) > 1:
                issues.append(
                    Issue(
                        "W06",
                        LEVEL_WARNING,
                        f"anchors(order={order_val})",
                        f"页面 {page!r} 内 order={order_val} 被多个锚点占用：{sorted(ids)}",
                    )
                )

    # W07：模块覆盖了 global 同名资产但未显式声明 _override（OPEN-6 已定 b：
    # 同名规则通电为 E 级——未声明的隐式覆盖属真源冲突，阻断启动而非告警）。
    # 生产侧在接线 global_assets 前命中数为 0，升级零爆炸半径。
    if global_assets is not None:
        for aid, anchor in assets.anchors.items():
            if aid in global_assets.anchors and not anchor.override:
                issues.append(
                    Issue(
                        "W07",
                        LEVEL_ERROR,
                        f"anchors.{aid}",
                        f"与 global 同名资产冲突但未声明 _override: true",
                    )
                )

    return issues


# ------------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------------


def _check_policies(assets: Assets) -> list[Issue]:
    """P01-P09：policies 决策策略校验（结构错误 P1 期即抛，这里补语义层）。

    字典序问题注意：`validate_policy_document` 返回 (code, level, path, message)，
    P06-P09 默认告警级，`strict` 未开启不阻断启动。
    """
    if assets.policies is None:
        return []
    issues: list[Issue] = []
    for code, level, path, message in validate_policy_document(
        assets.policies, assets.anchors
    ):
        issues.append(Issue(code, level, path, message))
    return issues


def _validate_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """从 `safe_load` 的 kwargs 里挑出 `validate_assets` 认识的那些。"""
    out: dict[str, Any] = {}
    for key in ("code_edges", "global_assets"):
        if key in kwargs:
            out[key] = kwargs[key]
    return out


def _merge(a: Report, b: Report) -> Report:
    return Report(issues=tuple(sorted(set(a.issues) | set(b.issues))))
