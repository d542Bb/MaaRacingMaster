#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ROI 统一配置底座（模块开发模式统一计划 · P1a）。

纯新增，不接入任何运行时代码。目标：把「ROI 如何定义 / 阶段如何被描述 / 坐标
如何解释」收敛成一份与内容无关的公共配置，供 racing / treasure 两模块共用骨架、
尺子和仪表盘，而保留各自领域的「何时/为何转阶段、转阶段执行什么动作」。

本模块交付三样契约（详见 .trae/documents/module-dev-model-unification-plan.md §五/§八）：

1. 坐标契约（exclusive, top-left, normalized）
   NormalizedROI：`[x1, y1, x2, y2]`，top-left 原点，归一化到 [0,1]，x2/y2 为
   **排他(exclusive)** 边界。像素转换统一为：
       x1_px = floor(x1 * W),  y1_px = floor(y1 * H)
       x2_px = ceil(x2  * W),  y2_px = ceil(y2  * H)
   保证 `0 <= x1 < x2 <= 1`、`0 <= y1 < y2 <= 1`，越界在**构造期报错**（非静默 clamp）。

2. ROIConfig（阶段三段式 + 感知与转移分离）
   `stages` 三段式配置：order / global_anchors / definitions。
   公共访问语义干净的三个 getter：
       get_active_rois(stage)     → definitions[stage].active_rois
       get_global_anchors()       → 恒全量，不受阶段裁剪影响（架构 invariant）
       get_detection_rois(stage)  → global_anchors + active_rois(stage)
   配置只负责「当前允许/需要感知什么」，阶段转移条件与副作用不进配置。

3. schema 版本契约
   `_schema_ver` 管**配置格式版本**（JSON 结构）；capability（能力校验：ROI 是否
   存在）由后续阶段（P2a)加入并独立于格式版本，避免「格式对但能力缺」拖到运行期。

按计划，P2b 将提供 module.config loader 契约；本模块只做纯数据解析与校验，
不假定加载来源（dict 直接构造，方便单测与后续接入）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

# 当前配置格式版本。结构不兼容时进位，勿与引用方字段混淆。
SCHEMA_VER = 1


@dataclass(frozen=True)
class NormalizedROI:
    """归一化矩形（坐标契约载体）。

    语义：top-left 原点，矩形边与两条坐标轴平行，坐标值归一化到 [0,1]；
    x2/y2 为 **排他(exclusive)** 边界（含 x1 不含 x2）。构造期校验
    `0 <= x1 < x2 <= 1`、`0 <= y1 < y2 <= 1`，越界即抛 ValueError（启动期报错，
    而非静默 clamp——避免用错误坐标掩盖真实配置问题）。
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        for name in ("x1", "y1", "x2", "y2"):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"NormalizedROI.{name} 必须为数字，收到: {val!r}")
            object.__setattr__(self, name, float(val))
        if not (0.0 <= self.x1 < self.x2 <= 1.0 and 0.0 <= self.y1 < self.y2 <= 1.0):
            raise ValueError(
                f"NormalizedROI 越界或退化，需满足 0<=x1<x2<=1 且 0<=y1<y2<=1，"
                f"收到: ({self.x1}, {self.y1}, {self.x2}, {self.y2})"
            )

    @classmethod
    def from_list(cls, rect: Iterable[float]) -> "NormalizedROI":
        """从长度为 4 的序列 `[x1, y1, x2, y2]` 构造。"""
        vals = list(rect)
        if len(vals) != 4:
            raise ValueError(f"NormalizedROI 需长度为 4 的 [x1,y1,x2,y2]，收到: {vals!r}")
        return cls(x1=vals[0], y1=vals[1], x2=vals[2], y2=vals[3])

    def to_pixel(self, width: int, height: int) -> tuple[int, int, int, int]:
        """按坐标契约把归一化 rect 换算为像素坐标。

        返回 (x1_px, y1_px, x2_px, y2_px)，其中：
            x1_px = floor(x1 * W);  y1_px = floor(y1 * H)
            x2_px = ceil(x2 * W);   y2_px = ceil(y2 * H)   （exclusive 边界，帧左闭右开）
        结果被夹紧到 [0, width]/[0, height] 内，保证切片不越出画布。
        """
        x1 = min(max(math.floor(self.x1 * width), 0), width)
        y1 = min(max(math.floor(self.y1 * height), 0), height)
        x2 = min(max(math.ceil(self.x2 * width), 0), width)
        y2 = min(max(math.ceil(self.y2 * height), 0), height)
        return x1, y1, x2, y2

    def center_norm(self) -> tuple[float, float]:
        """归一化中心点 (cx, cy)，供「静态按钮中心」类用途。"""
        return (self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0

    def as_list(self) -> list[float]:
        """以 [x1, y1, x2, y2] 列表返回（与原 treasure_rois.json rect 段同构）。"""
        return [self.x1, self.y1, self.x2, self.y2]


@dataclass(frozen=True)
class _StageROI:
    """单个 ROI 定义（坐标 + 可选模板/阈值承载体）。

    阈值/模板属于「感知参数」，随感知集合一起由配置给出；转移决策永远不进这里。
    """

    rect: NormalizedROI
    templates: tuple[str, ...] = ()
    threshold: float | None = None


class ROIConfig:
    """阶段三段式 + ROI 定义解析与访问。

    输入为符合 schema 的 dict（`_schema_ver` + `reference_size` + `rois` + `stages`）。
    本类只做：格式/坐标校验、干净的三段式访问。不持有、也不推断任何阶段转移逻辑。

    典型调用（领域代码只读感知集合，转阶段决策留在领域）：
        cfg = ROIConfig.from_dict(schema)
        rois = cfg.get_detection_rois("第1回合")   # global_anchors + 本阶段 active_rois
    """

    def __init__(self, schema: dict | None = None):
        if schema:
            self._load(schema)
            self.validate_schema()

    # ---------------- 只读的干净访问 ----------------

    @property
    def schema_ver(self) -> int | None:
        return self._schema_ver

    @property
    def reference_size(self) -> tuple[int, int]:
        return self._reference_size

    @property
    def stage_order(self) -> tuple[str, ...]:
        """有序阶段序列（stages.order 的元组视图）。"""
        return tuple(self._order)

    @property
    def global_anchors(self) -> tuple[str, ...]:
        """全局锚点集合（恒全量参与，不受阶段裁剪影响）。"""
        return tuple(self._global_anchors)

    def get_global_anchors(self) -> tuple[str, ...]:
        """全局锚点（恒全量）。方法形式，供领域代码显式感知「锚点永远在」。"""
        return tuple(self._global_anchors)

    def get_rect(self, roi_name: str) -> NormalizedROI:
        """取单个 ROI 的归一化矩形；未知 ROI 抛 KeyError（不静默返回空）。"""
        return self._rois[roi_name].rect

    def get_active_rois(self, stage: str) -> tuple[str, ...]:
        """当前阶段的感知集合（definitions[stage].active_rois）。未知阶段抛 KeyError。"""
        return tuple(self._definitions[stage])

    def get_detection_rois(self, stage: str) -> tuple[str, ...]:
        """本阶段实际参与匹配的 ROI = global_anchors + active_rois(stage)。

        global_anchors 恒全量——即使被某阶段引用为普通 active_rois 也永不因切阶段
        而消失，避免「切阶段→大厅锚点也被裁→阶段冻结」（鉴宝 module.py 记录过的
        真实事故）。锚点在前 + 去重，保证顺序稳定。
        """
        seen: set[str] = set()
        out: list[str] = []
        for name in (*self._global_anchors, *self.get_active_rois(stage)):
            if name not in seen:
                seen.add(name)
                out.append(name)
        return tuple(out)

    # ---------------- 校验 ----------------

    def validate_schema(self) -> None:
        """启动期结构校验：格式版本 + 必需段 + 坐标契约；不合法抛 ValueError。

        与 capability（ROI 定义存在性，P2a 交付）分开：前者管格式，后者管能力。
        """
        if not isinstance(self._schema_ver, int):
            raise ValueError(f"_schema_ver 必须为整数，收到: {self._schema_ver!r}")
        if not (isinstance(self._reference_size, tuple) and len(self._reference_size) == 2):
            raise ValueError(f"reference_size 需为 [W, H] 二元组，收到: {self._reference_size!r}")
        if not isinstance(self._rois, dict):
            raise ValueError("必需段 'rois' 缺失或非 object")
        if not isinstance(self._definitions, dict):
            raise ValueError("必需段 'stages.definitions' 缺失或非 object")

    # ---------------- 构造辅助 ----------------

    @classmethod
    def from_dict(cls, schema: dict) -> "ROIConfig":
        cfg = cls()
        cfg._load(schema)
        cfg.validate_schema()
        return cfg

    # ---------------- 内部加载（不对外开放） ----------------

    def _load(self, schema: dict) -> None:
        self._schema_ver = schema.get("_schema_ver")
        ref = schema.get("reference_size")
        if ref is not None:
            if not (isinstance(ref, (list, tuple)) and len(ref) == 2):
                raise ValueError(f"reference_size 需为 [W, H] 二元组，收到: {ref!r}")
            self._reference_size = (int(float(ref[0])), int(float(ref[1])))
        else:
            self._reference_size = (1280, 720)

        self._rois: dict[str, _StageROI] = {}
        rois_raw = schema.get("rois") or {}
        for name, val in rois_raw.items():
            if not isinstance(val, dict) or "rect" not in val:
                raise ValueError(f"ROI '{name}' 定义须为包含 'rect' 的 object")
            th_raw = val.get("threshold")
            self._rois[name] = _StageROI(
                rect=NormalizedROI.from_list(val["rect"]),
                templates=tuple(t for t in (val.get("templates") or []) if isinstance(t, str) and t),
                threshold=(
                    float(th_raw)
                    if isinstance(th_raw, (int, float)) and not isinstance(th_raw, bool)
                    and 0.0 <= th_raw <= 1.0
                    else None
                ),
            )

        stages = schema.get("stages") or {}
        order = stages.get("order") or []
        if not isinstance(order, list) or not all(isinstance(s, str) for s in order):
            raise ValueError("stages.order 须为字符串数组")
        self._order = list(order)

        anchors = stages.get("global_anchors") or []
        if not isinstance(anchors, list) or not all(isinstance(a, str) for a in anchors):
            raise ValueError("stages.global_anchors 须为字符串数组")
        self._global_anchors = list(anchors)

        defs = stages.get("definitions") or {}
        if not isinstance(defs, dict):
            raise ValueError("stages.definitions 须为 object（阶段名 → {active_rois}）")
        self._definitions: dict[str, list[str]] = {}
        for stage_name in order:
            d = defs.get(stage_name) or {}
            active = d.get("active_rois") if isinstance(d, dict) else None
            if active is None:
                active = []  # 缺省 = 空感知集合（对应「大厅: active_rois: [] 」语义）
            if not isinstance(active, list) or not all(isinstance(a, str) for a in active):
                raise ValueError(
                    f"stages.definitions['{stage_name}'].active_rois 须为字符串数组"
                )
            self._definitions[stage_name] = list(active)


def roi_names(rois_like: Iterable[str]) -> tuple[str, ...]:
    """把任意 ROI 名字可迭代对象规整为去重且保序的元组（公共小工具）。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in rois_like:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)