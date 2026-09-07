#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 资产模型（schema v3）——文档解析、内存对象与编译映射。

纯标准库：不 import cv2 / numpy / maa / vgamepad，`tests/` 与 CI 只装 pytest 即可运行。
坐标契约复用 `core.roi_config.NormalizedROI`（同为纯标准库，E06 明确要求与之同规则）。

职责边界
--------
本模块只做三件事：

1. `schema v3` 文档 → 内存对象（构造期即抛结构性错误，见下方"错误分层"）。
2. 为校验器提供查询能力（引用闭合、模板落盘情况、编译节点名冲突）。
3. v3 → MAA pipeline 的**节点名**映射规则（编译产物本身由 S2 的 `compile.py` 产出，
   这里只提供节点名这一唯一命名权威，避免两处各写一套导致 E20 失去意义）。

不做识别、不做点击、不读环境变量、不写文件。

错误分层
--------
- **结构性错误（构造期抛 `NavKitError`）**：E01 版本门禁、E03 reference_size、
  E04 match 唯一口径、E06 rect 越界。这些是"文档连对象都构造不出来"的错误，
  方案 §3.3 对 E06 明确写了"构造期即抛"，故一律在 `from_document` 抛，
  不做静默 clamp（与 `NormalizedROI` 的设计哲学一致：宁可启动失败，不用错坐标掩盖问题）。
- **语义错误（由 `validate.py` 收集为 Issue）**：E02/E05/E07-E20/W01-W07。
  这些需要跨对象引用才能判定，必须等全部对象构造完才检查。

`validate.safe_load()` 会把第一类错误也转成 Issue，供控制台输出"一条清单"。

字段语义、校验编号见 docs/NAVKIT_PLAN.md §4（字段表）与 §3.3（规则表）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..roi_config import NormalizedROI
from .policy import Policies, PolicyError, parse_policies

__all__ = [
    "SCHEMA_V3",
    "ANY_STAGE",
    "OWNER_GLOBAL",
    "ANCHOR_KINDS",
    "ROUTE_ACTIONS",
    "TEMPLATE_SUFFIXES",
    "SPECIAL_TRANSITION_TARGETS",
    "NavKitError",
    "MatchPolicy",
    "Arbitration",
    "Anchor",
    "StageDef",
    "Transition",
    "RouteStep",
    "Route",
    "Assets",
    "route_node_name",
]


# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

SCHEMA_V3 = 3
"""schema v3 版本号。`legacy.py` 读到 2 时走只读适配，不进本模块。"""

ANY_STAGE = "*"
"""transitions.stage 的通配值：任意阶段都生效。"""

OWNER_GLOBAL = "global"
"""全局归属标识。`owner` 的合法取值只有 `global` 与所在模块名（E08）。"""

CORE_IMAGE_DIR = Path(__file__).resolve().parents[1] / "resources" / "image"
"""core 侧公共模板图目录（包常量定位，不变量 I-2 的 `image_dirs[0]`）。

navkit 即 core 内包，`parents[1]` 恒为 `core/`，对位于 core 路径的 global 资产文档
同样成立——不依赖"路径含 plugins/<id>/"的推断（那对 global 必然落空）。
`image_dirs` 中 core 段恒定前置，W04 的归属判定依赖此序。
"""

ANCHOR_KINDS: frozenset[str] = frozenset({"template", "ocr", "point"})
"""锚点种类。

- `template`：模板认图，可作为迁移信号与 `guarded_by` 担保人
- `ocr`：只读取值区（读字/读数），不参与点击，不可作担保人
- `point`：固定坐标目标，**必须**配 `guarded_by`（E10）
"""

ROUTE_ACTIONS: frozenset[str] = frozenset({"click", "press", "do_nothing"})
"""route step 的动作类型。"""

TEMPLATE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg")
"""允许的模板图扩展名（E07）。"""

SPECIAL_TRANSITION_TARGETS: frozenset[str] = frozenset({"same", "$round"})
"""transitions.to 的合法特殊值：`same` 原地停留 / `$round` 按模板解析回合号实例化。"""


class NavKitError(ValueError):
    """schema v3 文档的结构性错误（构造期即抛）。

    携带 `code`（E01/E03/E04/E06）与 `path`，以便 `validate.safe_load`
    把它转成与语义错误同构的 Issue，输出成一张清单。
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"[{code}] {path}: {message}")
        self.code = code
        self.path = path
        self.message = message


# ------------------------------------------------------------------
# 值对象
# ------------------------------------------------------------------


@dataclass(frozen=True)
class MatchPolicy:
    """唯一匹配口径（§4.6）。

    `scales` 与 `threshold` 是"唯一口径"：任何锚点要么显式覆盖，要么继承这里，
    不存在第三处默认值。E04 保证这两个字段非空且合法。
    """

    scales: tuple[float, ...]
    threshold: float
    margin_default: float = 0.0


@dataclass(frozen=True)
class Arbitration:
    """多模板互斥时的裁决参数（§4.2），取代 v2 的 `_ROI_STAGE` 余下字段。

    - `margin`：多模板互斥时，最高分需领先次高分 ≥ margin 才算命中
      （鉴宝 `round_big_banner` 实测 0.03）
    - `round_from_template`：回合号从命中的模板文件名解析（round3_banner.png → 3）
    - `template_thresholds`：per-template 阈值覆盖（鉴宝中标横幅因彩条特效放宽到 0.60）
    """

    margin: float = 0.0
    round_from_template: bool = False
    template_thresholds: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Anchor:
    """识别/点击目标（§4.2）。

    `rect` 的语义随 `kind` 变化：`template` 是搜索区、`ocr` 是取值区、`point` 是点击区。
    `templates` 对 `kind=template` 必填非空（E07），对其它 kind 应为空。
    `guarded_by` 对 `kind=point` 必填（E10），且指向的锚点必须是 `kind=template`（E13）。
    """

    id: str
    kind: str
    owner: str
    page: str
    label: str
    rect: NormalizedROI
    templates: tuple[str, ...] = ()
    threshold: float | None = None
    scales: tuple[float, ...] | None = None
    order: int | None = None
    arbitration: Arbitration | None = None
    guarded_by: str | None = None
    domain: Mapping[str, Any] = field(default_factory=dict)
    comment: str = ""
    override: bool = False

    @property
    def is_global(self) -> bool:
        return self.owner == OWNER_GLOBAL


@dataclass(frozen=True)
class StageDef:
    """单个阶段定义（§4.3）。

    `anchors` 是本阶段的感知集合；`global_anchors` 恒并入、不写在这里（不变量 I-1：
    漏并入会导致"结算弹窗关闭后回不了鉴宝大厅、阶段永久冻结"的实测事故）。
    """

    name: str
    page: str | None = None
    anchors: tuple[str, ...] = ()
    ocr: tuple[str, ...] = ()
    dynamic_narrow: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    """阶段迁移声明（§4.4）——决定"树"是否有走向。

    `stage` 可为具体阶段名或 `ANY_STAGE`；`on` 必须是已存在且 `kind=template` 的锚点；
    `to` 是阶段名 / `same` / `$round`。
    """

    stage: str
    on: str
    to: str
    when: str = ""


@dataclass(frozen=True)
class RouteStep:
    """跨页面跳转链的单步（§4.5）。

    `confirm` 对 `click`/`press` 必填（E11）：点击进入下一步的判据即"该锚点出现"，
    末步的 `confirm` 即整条 route 的 `reached` 终点节点。
    """

    target: str
    action: str = "click"
    confirm: str | None = None
    timeout_ms: int | None = None
    rate_limit_ms: int | None = None
    press: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    """跨页面跳转链（§4.5）。`entry=true` 表示可从 UI 直达。"""

    name: str
    steps: tuple[RouteStep, ...] = ()
    entry: bool = False
    start_stage: str | None = None


# ------------------------------------------------------------------
# 顶层容器
# ------------------------------------------------------------------


@dataclass
class Assets:
    """schema v3 资产集的只读视图。

    构造请使用 `Assets.from_document()`（内存）或 `Assets.load()`（读文件）。
    `image_dirs` 决定模板图解析顺序：按 §7.3，`global` 目录在前、模块目录在后，
    同名**首次命中**；它同时是 W01/W02/W04 三个告警的判据来源。
    """

    module: str
    """模块标识，由所在 `plugins/<id>` 目录推断（物理位置是归属的真相）。"""

    reference_size: tuple[int, int]
    match: MatchPolicy
    pages: Mapping[str, Mapping[str, Any]]
    anchors: Mapping[str, Anchor]
    stage_order: tuple[str, ...]
    global_anchors: tuple[str, ...]
    stage_defs: Mapping[str, StageDef]
    transitions: tuple[Transition, ...]
    routes: Mapping[str, Route]
    render: Mapping[str, Any] = field(default_factory=dict)
    trace: Mapping[str, Any] = field(default_factory=dict)
    policies: Policies | None = None
    """决策策略（P1）：文档 `policies` 段的一等公民，不另起 json.load。

    缺省为 None（旧资产未声明 policies）；P1e 终局由调用方强制非空。
    """

    # ---- 元信息（不参与语义，仅供追溯与校验）----
    declared_module: str | None = None
    """文档里 `_module` 字段的原值。

    与 `module`（由物理目录推断）分开保存，E02 负责比对二者：
    声明值与物理位置不一致 = 资产错挂模块，属启动期硬失败。
    """

    source_path: Path | None = None
    image_dirs: tuple[Path, ...] = ()

    # ---------------- 构造 ----------------

    @classmethod
    def from_document(
        cls,
        doc: Mapping[str, Any],
        *,
        module: str,
        image_dirs: Sequence[Path] = (),
        source_path: Path | None = None,
    ) -> "Assets":
        """从 schema v3 文档构造。结构性错误抛 `NavKitError`（构造期即抛）。"""
        ver = doc.get("_schema_ver")
        if ver != SCHEMA_V3:
            raise NavKitError(
                "E01",
                "_schema_ver",
                f"需为 {SCHEMA_V3}（v2 请走 legacy.py 只读适配），收到 {ver!r}",
            )

        reference_size = _parse_reference_size(doc.get("reference_size"))
        match = _parse_match(doc.get("match"))
        pages = _parse_pages(doc.get("pages"))
        anchors = _parse_anchors(doc.get("anchors"))

        stages_raw = doc.get("stages") or {}
        if not isinstance(stages_raw, Mapping):
            raise NavKitError("E14", "stages", f"stages 须为 object，收到 {type(stages_raw).__name__}")
        stage_order = _parse_str_list(stages_raw.get("order"), "stages.order")
        global_anchors = _parse_str_list(
            stages_raw.get("global_anchors"), "stages.global_anchors"
        )
        stage_defs = _parse_stage_defs(stages_raw.get("definitions"))

        transitions = _parse_transitions(doc.get("transitions"))
        routes = _parse_routes(doc.get("routes"))
        policies = _parse_policies(doc.get("policies"))

        declared = doc.get("_module")
        return cls(
            module=module,
            declared_module=(str(declared) if declared is not None else None),
            reference_size=reference_size,
            match=match,
            pages=dict(pages),
            anchors=dict(anchors),
            stage_order=stage_order,
            global_anchors=global_anchors,
            stage_defs=dict(stage_defs),
            transitions=transitions,
            routes=dict(routes),
            render=dict(doc.get("render") or {}),
            trace=dict(doc.get("trace") or {}),
            policies=policies,
            source_path=source_path,
            image_dirs=tuple(image_dirs),
        )

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        module: str | None = None,
        image_dirs: Sequence[Path] | None = None,
        encoding: str = "utf-8",
    ) -> "Assets":
        """从 JSON 文件加载。

        `module` 缺省时从路径中的 `plugins/<id>/` 推断；推断不到则用文档里的 `_module`。
        `image_dirs` 缺省时按 §7.3 拼 `core/resources/image` + `plugins/<id>/resources/image`；
        显式传入时为**追加语义**：core 段（`CORE_IMAGE_DIR`）恒定前置，传入目录作为
        模块目录跟在后面——替换语义会让显式传参的调用点丢掉 core 段，
        global 模板图一张都解析不到。
        """
        p = Path(path)
        with open(p, "r", encoding=encoding) as f:
            doc = json.load(f)
        if not isinstance(doc, Mapping):
            raise NavKitError("E01", str(p), f"文档根节点须为 object，收到 {type(doc).__name__}")

        resolved_module = module or _infer_module(p) or str(doc.get("_module") or "")
        if not resolved_module:
            raise NavKitError(
                "E02",
                str(p),
                "_module 缺失，且无法从路径推断所属插件目录（未找到 plugins/<id>/）",
            )

        if image_dirs is None:
            image_dirs = _default_image_dirs(p, resolved_module)
        else:
            # 追加语义（I-2）：显式传入的是模块目录，core 段恒定前置。
            core = (CORE_IMAGE_DIR,) if CORE_IMAGE_DIR.is_dir() else ()
            image_dirs = core + tuple(image_dirs)
        return cls.from_document(
            doc, module=resolved_module, image_dirs=image_dirs, source_path=p
        )

    # ---------------- 查询 ----------------

    @property
    def source_hash(self) -> str | None:
        """源资产文件的 sha256 前 8 位（§6 可审计性：编译产物头部记录来源 hash）。

        无源文件（内存构造）或文件不可读时返回 None，让校验器跳过该项比对，
        而不是造一个假 hash 让产物永远"看起来一致"。
        """
        if self.source_path is None or not self.source_path.is_file():
            return None
        try:
            digest = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        except OSError:
            return None
        return digest[:8]

    @property
    def anchor_ids(self) -> tuple[str, ...]:
        """全部锚点 id（文档顺序）。"""
        return tuple(self.anchors)

    def referenced_templates(self) -> tuple[str, ...]:
        """全部被引用的模板图名（去重、文档顺序）。"""
        seen: set[str] = set()
        out: list[str] = []
        for anchor in self.anchors.values():
            for name in anchor.templates:
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        return tuple(out)

    def template_files(self) -> dict[str, Path]:
        """`image_dirs` 下实际存在的模板图 `{文件名: 路径}`。

        按 §7.3 不变量：按 `image_dirs` 顺序**首次命中**，同名不覆盖。
        目录内按文件名排序，保证结果确定（同一输入两次调用字节相同）。
        """
        out: dict[str, Path] = {}
        for d in self.image_dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if (
                    p.is_file()
                    and p.suffix.lower() in TEMPLATE_SUFFIXES
                    and p.name not in out
                ):
                    out[p.name] = p
        return out

    def resolve_template(self, name: str) -> Path | None:
        """按 `image_dirs` 首次命中解析模板图；不存在返回 None（W02 判据）。"""
        for d in self.image_dirs:
            candidate = d / name
            if candidate.is_file():
                return candidate
        return None

    def compilation_node_names(self) -> list[str]:
        """按编译映射表（§6）生成全部 MAA 节点名，供 E20 查重。"""
        names: list[str] = []
        for route in self.routes.values():
            for index, step in enumerate(route.steps):
                names.append(route_node_name(self.module, route.name, index, step.target))
        return names

    def anchors_on_page(self, page: str) -> tuple[Anchor, ...]:
        """某页面下的全部锚点（文档顺序）。"""
        return tuple(a for a in self.anchors.values() if a.page == page)


# ------------------------------------------------------------------
# 编译映射（§6）：节点名的唯一权威
# ------------------------------------------------------------------


def route_node_name(module: str, route: str, step_index: int, target: str) -> str:
    """MAA pipeline 节点名：`<module>::<route>::<step#>::<target>`。

    加模块前缀是为了避免跨模块重名（§6 明确"避免跨模块重名"）。
    S2 的 `compile.py` 必须复用本函数，否则 E20 的查重失去意义。
    """
    return f"{module}::{route}::{step_index}::{target}"


# ------------------------------------------------------------------
# 内部解析
# ------------------------------------------------------------------


def _parse_reference_size(raw: Any) -> tuple[int, int]:
    if not (isinstance(raw, (list, tuple)) and len(raw) == 2):
        raise NavKitError("E03", "reference_size", f"需为 [W, H] 二元组，收到 {raw!r}")
    w, h = raw
    for name, val in (("W", w), ("H", h)):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise NavKitError("E03", f"reference_size.{name}", f"需为数字，收到 {val!r}")
        if float(val) <= 0:
            raise NavKitError("E03", f"reference_size.{name}", f"需为正整数，收到 {val!r}")
    return int(w), int(h)


def _parse_match(raw: Any) -> MatchPolicy:
    if not isinstance(raw, Mapping):
        raise NavKitError("E04", "match", f"match 须为 object（唯一匹配口径），收到 {raw!r}")

    scales_raw = raw.get("scales")
    if not isinstance(scales_raw, (list, tuple)) or not scales_raw:
        raise NavKitError("E04", "match.scales", f"scales 须为非空数组，收到 {scales_raw!r}")
    scales: list[float] = []
    for i, s in enumerate(scales_raw):
        if isinstance(s, bool) or not isinstance(s, (int, float)) or float(s) <= 0:
            raise NavKitError("E04", f"match.scales[{i}]", f"需为正数，收到 {s!r}")
        scales.append(float(s))

    th = raw.get("threshold")
    if isinstance(th, bool) or not isinstance(th, (int, float)) or not (0.0 < float(th) <= 1.0):
        raise NavKitError("E04", "match.threshold", f"需落在 (0, 1]，收到 {th!r}")

    margin = raw.get("margin_default", 0.0)
    if isinstance(margin, bool) or not isinstance(margin, (int, float)):
        raise NavKitError("E04", "match.margin_default", f"需为数字，收到 {margin!r}")

    return MatchPolicy(scales=tuple(scales), threshold=float(th), margin_default=float(margin))


def _parse_pages(raw: Any) -> dict[str, Mapping[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise NavKitError("E09", "pages", f"pages 须为 object，收到 {raw!r}")
    out: dict[str, Mapping[str, Any]] = {}
    for key, val in raw.items():
        if not isinstance(val, Mapping):
            raise NavKitError("E09", f"pages.{key}", f"须为 object，收到 {val!r}")
        out[str(key)] = dict(val)
    return out


def _parse_anchors(raw: Any) -> dict[str, Anchor]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise NavKitError("E05", "anchors", f"anchors 须为 object，收到 {raw!r}")
    out: dict[str, Anchor] = {}
    for key, val in raw.items():
        if not isinstance(val, Mapping):
            raise NavKitError("E05", f"anchors.{key}", f"须为 object，收到 {val!r}")
        aid = str(key)
        out[aid] = Anchor(
            id=aid,
            kind=str(val.get("kind", "")),
            owner=str(val.get("owner", "")),
            page=str(val.get("page", "")),
            label=str(val.get("label", "")),
            rect=_parse_rect(val.get("rect"), f"anchors.{key}.rect"),
            templates=_parse_str_list(val.get("templates"), f"anchors.{key}.templates"),
            threshold=_parse_optional_float(val.get("threshold"), f"anchors.{key}.threshold"),
            scales=_parse_optional_float_list(val.get("scales"), f"anchors.{key}.scales"),
            order=_parse_optional_int(val.get("order"), f"anchors.{key}.order"),
            arbitration=_parse_arbitration(
                val.get("arbitration"), f"anchors.{key}.arbitration"
            ),
            guarded_by=(
                str(val["guarded_by"]) if val.get("guarded_by") is not None else None
            ),
            domain=dict(val.get("domain") or {}),
            comment=str(val.get("comment", "")),
            override=bool(val.get("_override", False)),
        )
    return out


def _parse_arbitration(raw: Any, path: str) -> Arbitration | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise NavKitError("E04", path, f"arbitration 须为 object，收到 {raw!r}")
    ths_raw = raw.get("template_thresholds") or {}
    if not isinstance(ths_raw, Mapping):
        raise NavKitError("E04", f"{path}.template_thresholds", f"须为 object，收到 {ths_raw!r}")
    ths: dict[str, float] = {}
    for tpl, val in ths_raw.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise NavKitError(
                "E04", f"{path}.template_thresholds.{tpl}", f"需为数字，收到 {val!r}"
            )
        ths[str(tpl)] = float(val)
    return Arbitration(
        margin=float(raw.get("margin", 0.0)),
        round_from_template=bool(raw.get("round_from_template", False)),
        template_thresholds=ths,
    )


def _parse_stage_defs(raw: Any) -> dict[str, StageDef]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise NavKitError("E14", "stages.definitions", f"须为 object，收到 {raw!r}")
    out: dict[str, StageDef] = {}
    for key, val in raw.items():
        if not isinstance(val, Mapping):
            raise NavKitError("E14", f"stages.definitions.{key}", f"须为 object，收到 {val!r}")
        name = str(key)
        out[name] = StageDef(
            name=name,
            page=(str(val["page"]) if val.get("page") is not None else None),
            anchors=_parse_str_list(val.get("anchors"), f"stages.definitions.{key}.anchors"),
            ocr=_parse_str_list(val.get("ocr"), f"stages.definitions.{key}.ocr"),
            dynamic_narrow=dict(val.get("dynamic_narrow") or {}),
        )
    return out


def _parse_transitions(raw: Any) -> tuple[Transition, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise NavKitError("E12", "transitions", f"transitions 须为数组，收到 {raw!r}")
    out: list[Transition] = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise NavKitError("E12", f"transitions[{i}]", f"须为 object，收到 {item!r}")
        out.append(
            Transition(
                stage=str(item.get("stage", "")),
                on=str(item.get("on", "")),
                to=str(item.get("to", "")),
                when=str(item.get("when", "")),
            )
        )
    return tuple(out)


def _parse_routes(raw: Any) -> dict[str, Route]:
    out: dict[str, Route] = {}
    for key, val in raw.items():
        if not isinstance(val, Mapping):
            raise NavKitError("E11", f"routes.{key}", f"须为 object，收到 {val!r}")
        steps_raw = val.get("steps") or []
        if not isinstance(steps_raw, (list, tuple)):
            raise NavKitError("E11", f"routes.{key}.steps", f"须为数组，收到 {steps_raw!r}")
        steps: list[RouteStep] = []
        for j, s in enumerate(steps_raw):
            if not isinstance(s, Mapping):
                raise NavKitError("E11", f"routes.{key}.steps[{j}]", f"须为 object，收到 {s!r}")
            steps.append(
                RouteStep(
                    target=str(s.get("target", "")),
                    action=str(s.get("action", "click")),
                    confirm=(str(s["confirm"]) if s.get("confirm") is not None else None),
                    timeout_ms=_parse_optional_int(
                        s.get("timeout_ms"), f"routes.{key}.steps[{j}].timeout_ms"
                    ),
                    rate_limit_ms=_parse_optional_int(
                        s.get("rate_limit_ms"), f"routes.{key}.steps[{j}].rate_limit_ms"
                    ),
                    press=dict(s.get("press") or {}),
                )
            )
        out[str(key)] = Route(
            name=str(key),
            steps=tuple(steps),
            entry=bool(val.get("entry", False)),
            start_stage=(str(val["start_stage"]) if val.get("start_stage") is not None else None),
        )
    return out


def _parse_policies(raw: Any) -> Policies | None:
    """`policies` 段 → `Policies` 对象；缺失时返回 None。

    结构性错误（P01-P05）转换为 `NavKitError`，与 E 系列同构，
    便于 `validate.safe_load` 输出一张清单。
    """
    if raw is None:
        return None
    try:
        return parse_policies(raw)
    except PolicyError as exc:
        raise NavKitError(exc.code, exc.path, exc.message) from exc


def _parse_rect(raw: Any, path: str) -> NormalizedROI:
    """E06：与 `NormalizedROI.__post_init__` 同规则，构造期即抛（不静默 clamp）。"""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise NavKitError("E06", path, f"rect 需为 [x1,y1,x2,y2] 四元组，收到 {raw!r}")
    try:
        return NormalizedROI.from_list(raw)
    except ValueError as exc:
        raise NavKitError("E06", path, str(exc)) from exc


def _parse_str_list(raw: Any, path: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise NavKitError("E12", path, f"须为字符串数组，收到 {raw!r}")
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            raise NavKitError("E12", f"{path}[{i}]", f"须为非空字符串，收到 {item!r}")
        out.append(item)
    return tuple(out)


def _parse_optional_float(raw: Any, path: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise NavKitError("E04", path, f"需为数字，收到 {raw!r}")
    return float(raw)


def _parse_optional_float_list(raw: Any, path: str) -> tuple[float, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise NavKitError("E04", path, f"须为数字数组，收到 {raw!r}")
    out: list[float] = []
    for i, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NavKitError("E04", f"{path}[{i}]", f"需为数字，收到 {item!r}")
        out.append(float(item))
    return tuple(out)


def _parse_optional_int(raw: Any, path: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise NavKitError("E04", path, f"需为整数，收到 {raw!r}")
    return raw


def _infer_module(path: Path) -> str | None:
    """从路径中找 `plugins/<id>/` 段，推断模块名。"""
    parts = path.resolve().parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == "plugins" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _default_image_dirs(asset_path: Path, module: str) -> tuple[Path, ...]:
    """按 §7.3 拼默认模板图目录：core 段（`CORE_IMAGE_DIR`）恒在前，模块目录随后。

    core 段由包常量定位、不依赖资产路径——global 文档位于 core 路径、无
    `plugins/` 段，root 推断必然落空，此时返回**仅含 core 段的一元组**，
    禁止返回空（否则 global 的 W01/W02/W04 全部静默失效）。
    模块目录仍从路径中的 `plugins/<id>/` 段推断；只返回实际存在的目录，
    避免把不存在的目录塞进 `image_dirs`（否则 W04 会因目录缺失而误判）。
    """
    dirs: list[Path] = []
    if CORE_IMAGE_DIR.is_dir():
        dirs.append(CORE_IMAGE_DIR)
    parts = asset_path.resolve().parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == "plugins" and i + 1 < len(parts):
            module_dir = Path(*parts[:i + 1]) / module / "resources" / "image"
            if module_dir.is_dir():
                dirs.append(module_dir)
            break
    return tuple(dirs)
