#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit v4 数据面 loader——policy.json → 运行时消费对象（P4b 引入）。

运行时感知/决策数据的唯一真源：

- `perception.{spec,stages,transitions,match}` → `DetectionPlan`
  （detector 每帧扫描规格；等价性对拍见 commit `24d831a`）
- `policy` 段 → `Policies`（决策引擎 policy.py 原样复用，仅换装配口）

`spec` 同时是 module 私有感知装载器（鉴宝师模板/勾选/场次面板/智能出价/
彩蛋/动作中心点）与 ocr 区域的供料源——消费接口为稳定调用形
（`.rect.as_list()` / `.kind` / `.templates` / `.threshold` / `.order` / `.domain`）。

编译确定性：同输入两次加载产出等值对象。
纯标准库：不 import cv2 / numpy / maa / vgamepad。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from ..roi_config import ROIConfig
from .policy import Policies, parse_engine_contract, parse_policies

__all__ = [
    "ROUND_PHASE_STAGE",
    "Anchor",
    "AnchorSpec",
    "DetectionPlan",
    "NavSource",
    "Rect",
    "load_nav_source",
]

# __round_phase__：出价面板阶段的内部标记。命中后按
# arbitration.round_from_template / _last_round 决定具体「第N回合出价」。
ROUND_PHASE_STAGE = "__round_phase__"


@dataclass(frozen=True)
class Rect:
    """归一化矩形（`.as_list()` 为装载器/检测器的稳定调用形）。"""

    x1: float
    y1: float
    x2: float
    y2: float

    def as_list(self) -> list[float]:
        return [self.x1, self.y1, self.x2, self.y2]

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass(frozen=True)
class Anchor:
    """policy.json `perception.spec` 单条锚点的内存形态。"""

    # 注：policy JSON 里锚点的 `page` 字段是 ROI Studio 编辑器侧的视觉页分组
    # （分组展示 + E09 校验消费，机检见 tools/navkit/check_truth.page_checks），
    # 不是运行时归属真源——「阶段 → 信号/锚点」唯一真源是 definitions[*].active/ocr，
    # 故此处不解析进内存对象（ADR-0002 真源单一）。
    name: str
    kind: str
    label: str | None
    rect: Rect
    templates: tuple[str, ...] = ()
    threshold: float | None = None
    order: int | None = None
    arbitration: dict = field(default_factory=dict)
    guarded_by: str | None = None
    domain: dict | None = None
    # P4c：匹配色彩空间（宪法 §6「按节点声明 colorspace」；python 侧消费形，
    # 图侧同名字段在 pipeline 节点参数里）。默认彩色，灰度按锚点显式声明。
    colorspace: str = "rgb"


@dataclass(frozen=True)
class AnchorSpec:
    """单锚点的可执行规格（detector 每帧按它匹配）。"""

    name: str
    kind: str
    stage: str | None          # 命中后归属阶段；None=仅作信号（不出现在结果里）
    stage_priority: int        # 检测优先级（数值大者先扫）
    rect: tuple[float, float, float, float]
    templates: tuple[str, ...] = ()
    threshold: float | None = None       # None → 用 plan.default_threshold
    scales: tuple[float, ...] | None = None  # None → 用 plan.scales
    arbitration: Mapping = field(default_factory=dict)
    guarded_by: str | None = None
    colorspace: str = "rgb"


@dataclass(frozen=True)
class DetectionPlan:
    """加载产物：帧循环的检测真源。"""

    stage_order: tuple[str, ...]
    global_anchors: tuple[str, ...]
    active: Mapping[str, frozenset[str]]       # stage → 本阶段感知锚点
    ocr_keys: Mapping[str, frozenset[str]]     # stage → OCR keys
    spec: Mapping[str, AnchorSpec]             # 锚点名 → 规格（含 ocr 类）
    scales: tuple[float, ...]                  # 唯一尺度表（G3）
    default_threshold: float                   # 唯一默认阈值
    margin_default: float
    detect_anchors: tuple[str, ...]            # 阶段检测锚点（不含 appraiser/actions 独立匹配）
    dynamic_narrow: Mapping[str, str]          # stage → "code:xxx" 指针
    stage_stage: Mapping[str, str]             # 锚点 → 命中阶段（含 ROUND_PHASE_STAGE 哨兵）
    ocr_stages: Mapping[str, frozenset[str]]   # OCR 信号 → 允许出现的阶段（ocr_keys 反转）

    def active_for(self, stage: str | None) -> frozenset[str] | None:
        """当前阶段的激活集；未登记返回 None（运行时回退全量检测，既有安全兜底）。"""
        if stage is None:
            return None
        return self.active.get(stage)

    def ocr_for(self, stage: str | None) -> frozenset[str] | None:
        """当前阶段的 OCR keys；未登记返回 None（运行时语义=全量）。"""
        if stage is None:
            return None
        return self.ocr_keys.get(stage)

    def stages_for(self, signal: str) -> frozenset[str] | None:
        """OCR 信号的「允许阶段集合」；未登记返回 None（无页面约束，调用方放行）。

        与 ocr_for 互为逆查，同一真源的两面：`definitions[*].ocr` 声明「该阶段扫哪些
        OCR 信号」，反转即得「该信号只允许出现在哪些阶段」。消费侧据此判断某个读数
        是否可能来自当前画面——比阶段的滞后判定更贴近本帧事实。
        """
        return self.ocr_stages.get(signal)


@dataclass(frozen=True)
class NavSource:
    """policy.json 一次加载的完整产物。"""

    path: Path
    spec: Mapping[str, Anchor]         # 感知规格（全锚点，含 ocr/point/template 各类）
    plan: DetectionPlan
    policies: Policies
    tuning: Mapping[str, Any]          # policy.tuning 全量（perception/policy/execution 三段）


def _transition_target_stage(to: str) -> str:
    """transitions.to → 命中后归属阶段。

    `$round` / `same` 表示"命中即进入出价面板"——归属 ROUND_PHASE_STAGE 哨兵，
    由 detector 按回合逻辑实例化具体阶段名。
    """
    if to in ("$round", "same"):
        return ROUND_PHASE_STAGE
    return to


def _anchor_priority(anchor: Anchor) -> int:
    """锚点检测优先级（数值大者先扫；语义承接原 compile_detect._anchor_priority）。

    全局锚点不参与加权——"全局"的语义是"每一帧都并入扫描集合"（不变量 I-1），
    不是"优先扫描"（历史坑：曾给全局锚点 +100 导致大厅压过弹窗）。
    """
    return 1000 - (anchor.order or 0)


def _parse_spec_section(raw: Mapping[str, Any]) -> dict[str, Anchor]:
    anchors: dict[str, Anchor] = {}
    for name, a in raw.items():
        rect = a["rect"]
        anchors[name] = Anchor(
            name=name,
            kind=a["kind"],
            label=a.get("label"),
            rect=Rect(*rect),
            templates=tuple(a.get("templates") or ()),
            threshold=a.get("threshold"),
            order=a.get("order"),
            arbitration=dict(a.get("arbitration") or {}),
            guarded_by=a.get("guarded_by"),
            domain=dict(a.get("domain") or {}),
            colorspace=str(a.get("colorspace") or "rgb"),
        )
    return anchors


def _build_detection_plan(anchors: Mapping[str, Anchor], perception: Mapping[str, Any]) -> DetectionPlan:
    stages = perception["stages"]
    match = perception["match"]
    defs: Mapping[str, Any] = stages["definitions"]
    global_anchors = tuple(stages.get("global_anchors") or [])

    # 坐标契约兜底校验（与原 compile_detection 同款：构造成功即全量成立）
    ROIConfig.from_dict({
        "_schema_ver": 1,
        "reference_size": list(match["reference_size"]),
        "rois": {n: {"rect": list(a.rect.as_list()), "templates": list(a.templates),
                     "threshold": a.threshold} for n, a in anchors.items()},
        "stages": {
            "order": list(stages["order"]),
            "global_anchors": list(global_anchors),
            "definitions": {s: {"active_rois": list(d.get("active") or [])}
                            for s, d in defs.items()},
        },
    })

    stage_stage: dict[str, str] = {}
    for tr in perception.get("transitions") or []:
        stage_stage.setdefault(tr["on"], _transition_target_stage(tr["to"]))

    specs: dict[str, AnchorSpec] = {}
    for name, anchor in anchors.items():
        specs[name] = AnchorSpec(
            name=name,
            kind=anchor.kind,
            stage=stage_stage.get(name),
            stage_priority=_anchor_priority(anchor),
            rect=anchor.rect.as_tuple(),
            templates=anchor.templates,
            threshold=anchor.threshold,
            scales=None,
            arbitration=dict(anchor.arbitration),
            guarded_by=anchor.guarded_by,
            colorspace=anchor.colorspace,
        )

    active: dict[str, frozenset[str]] = {}
    ocr_keys: dict[str, frozenset[str]] = {}
    dynamic: dict[str, str] = {}
    for stage_name, sd in defs.items():
        active[stage_name] = frozenset(sd.get("active") or [])
        ocr_keys[stage_name] = frozenset(sd.get("ocr") or [])
        by = (sd.get("dynamic_narrow") or {}).get("by")
        if by:
            dynamic[stage_name] = str(by)

    # ocr_keys 反转：signal → 允许阶段集合。构建期算一次，运行时只查表（O(1)）。
    _by_signal: dict[str, set[str]] = {}
    for stage_name, keys in ocr_keys.items():
        for key in keys:
            _by_signal.setdefault(key, set()).add(stage_name)
    ocr_stages: Mapping[str, frozenset[str]] = {
        key: frozenset(names) for key, names in _by_signal.items()
    }

    active_names = {name for values in active.values() for name in values}
    detect_anchors = tuple(
        name for name, spec in specs.items()
        if spec.kind == "template" and spec.templates
        and (name in active_names or name in global_anchors)
    )

    return DetectionPlan(
        stage_order=tuple(stages["order"]),
        global_anchors=global_anchors,
        active=active,
        ocr_keys=ocr_keys,
        ocr_stages=ocr_stages,
        spec=specs,
        scales=tuple(match["scales"]),
        default_threshold=float(match["threshold"]),
        margin_default=float(match["margin_default"]),
        detect_anchors=detect_anchors,
        dynamic_narrow=dynamic,
        stage_stage=stage_stage,
    )


def load_nav_source(policy_path: Path) -> NavSource:
    """policy.json → NavSource。结构性错误直接抛（fail-closed）。

    缓存语义（冷生效）：同一路径只读一次，改文件下次
    进程启动生效；测试以不同 tmp 路径注入，不受缓存串扰。
    """
    return _load_nav_source_cached(Path(policy_path))


@lru_cache(maxsize=8)
def _load_nav_source_cached(policy_path: Path) -> NavSource:
    doc = json.loads(policy_path.read_text(encoding="utf-8"))
    perception = doc["perception"]
    anchors = _parse_spec_section(perception["spec"])
    plan = _build_detection_plan(anchors, perception)
    # 引擎契约先行解析（缺失/非法 = 整源 fail-closed，与 policy 段同权）
    contract = parse_engine_contract(doc["engine_contract"])
    pol_raw = doc["policy"]
    policies = parse_policies({
        "_schema_ver": pol_raw.get("schema_ver"),
        "stage_map": pol_raw.get("stage_map"),
        "rules": pol_raw.get("rules"),
        "tuning": pol_raw.get("tuning"),
    }, contract)
    return NavSource(
        path=policy_path,
        spec=anchors,
        plan=plan,
        policies=policies,
        tuning=dict(pol_raw.get("tuning") or {}),
    )
