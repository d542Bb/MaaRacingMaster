#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
module.config loader 契约（模块开发模式统一计划 · P2b）。

纯新增，不接入任何运行时代码。负责把 `module.config.json` 读成一份**已校验的、
已分类的**配置对象——把 JSON 里相互独立的 `rois/stages`（→ ROIConfig）与
`render`（→ RenderPlan）组装成单一 `ModuleConfig` 门面。

要点：
- 单一加载入口 `load_module_config()`：接受 dict 或文件路径。
- 加载即校验：`_schema_ver` 格式 + 坐标契约（ROIConfig）+ 图层能力（RenderPlan）。
- `ModuleConfig` 只组装已有底座，不重复实现解析逻辑（复用 ROIConfig / RenderPlan）。
- 不假定 JSON 来源：dict 或路径都行；路径由调用方（P5 迁移）决定如何定位。

JSON 结构与计划 §五/§六/§八 对齐：
```
{
  "_schema_ver": 1,
  "reference_size": [1280, 720],
  "rois": { "<roi>": {"rect": [...], "templates": [...], "threshold": ...} },
  "stages": {
    "order": [...], "global_anchors": [...],
    "definitions": {"<stage>": {"active_rois": [...]}}
  },
  "render": {
    "debug": [...], "peep": [...], "hud_fields": [...]
  }
}
```
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maaracing_master.core.render_plan import RenderPlan
from maaracing_master.core.roi_config import ROIConfig


class ModuleConfigError(ValueError):
    """module.config 加载/校验失败（聚合底层 ROIConfig/RenderPlan 校验错误）。"""


@dataclass(frozen=True)
class ModuleConfig:
    """已加载并校验好的模块配置门面。

    领域代码经此处取「感知集合」(roi_config) 与「渲染图画什么」(render_plan)，
    不再直接面对裸 dict。转移决策仍留在领域，本类只做统一读取。
    """

    schema_ver: int
    reference_size: tuple[int, int]
    roi_config: ROIConfig
    render_plan: RenderPlan

    @classmethod
    def from_dict(cls, data: dict) -> "ModuleConfig":
        return load_module_config(data)


def _load_from_obj(schema: dict) -> ModuleConfig:
    # ROIConfig 已负责 _schema_ver / reference_size / rois / stages + 坐标契约
    roi = ROIConfig.from_dict(schema)

    render_raw = schema.get("render") or {}
    plan = RenderPlan(
        debug_layers=tuple(render_raw.get("debug") or ()),
        peep_layers=tuple(render_raw.get("peep") or ()),
        hud_fields=tuple(render_raw.get("hud_fields") or ()),
    )
    # 能力校验：render 引用的图层是否存在，由后续 registry（P2a）启动期验证图层是否注册；
    # 这里只保证字段类型正确。空 render 允许（某些模块不需要 Debug 渲染）。
    _validate_render_types(render_raw, plan)

    return ModuleConfig(
        schema_ver=roi.schema_ver or 0,
        reference_size=roi.reference_size,
        roi_config=roi,
        render_plan=plan,
    )


def _validate_render_types(render_raw: dict, plan: RenderPlan) -> None:
    for field_name, names in (
        ("debug", plan.debug_layers), ("peep", plan.peep_layers), ("hud_fields", plan.hud_fields),
    ):
        # 字段可选：缺失时 RenderPlan 默认空；给非字符串项即提示（防手滑写错类型）
        for n in names:
            if not isinstance(n, str):
                raise ModuleConfigError(
                    f"render.{field_name} 须为字符串数组，收到非字符串元素: {n!r}"
                )


def load_module_config(source: dict | Path | str) -> ModuleConfig:
    """加载并校验 module.config 配置。

    source 支持：dict（直接）、str/Path（JSON 文件路径）。
    任何结构/坐标/类型错误都会以 ModuleConfigError 抛出（启动期报错，非静默忽略）。
    """
    if isinstance(source, dict):
        data: dict[str, Any] = source
    else:
        path = Path(source)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ModuleConfigError(f"读取 module.config 失败: {path} ({e})") from None
        if not isinstance(data, dict):
            raise ModuleConfigError(f"module.config 顶层须为 object，收到: {type(data).__name__}")
    try:
        return _load_from_obj(data)
    except ValueError as e:
        raise ModuleConfigError(f"module.config 校验失败: {e}") from None