#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
渲染计划底座（模块开发模式统一计划 · P2a）。

纯新增，不接入任何运行时代码。目标：把「Debug 按模块画哪些图层」收敛成一份
**能力选择器**（配置决定「画不画 × 哪些层」，Python 决定「怎么画」），并配一个
**通用调度器**（layer registry：模块自注册，core 不做图层内容判断）。

三样契约（详见计划 §六）：
1. RenderPlan —— 能力选择器，**不是能力定义器**。只持有
   `debug_layers` / `peep_layers` / `hud_fields` 三个字符串列表，不含任何
   "if layer == 'lane': ..."式领域判断。明确禁止造 JSON 绘图 DSL / 条件表达式 /
   数据绑定。
2. LayerRegistry —— 模块 renderer 自注册图层，core 只做 dispatch：
       renderer.register("lane", renderer.draw_lane)   # 模块自己声明
       renderer.draw("lane", context)                  # core 调用，不认识图层内容
   保证模块自由增删图层，core 零改动。
3. capability validation —— `_schema_ver` 只验格式版本；图层合法性在**启动期**验：
   `renderer.validate_layers(render_plan)`，未知图层启动即报错，不拖到运行期 KeyError /
   完全空白。与格式版本分开（format vs capability 两层）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class UnknownLayerError(KeyError):
    """调度到未注册图层（或校验到配置里引用了未注册图层）时抛出。"""

    def __init__(self, layer: str, known: tuple[str, ...]):
        self.layer = layer
        self.known = known
        super().__init__(
            f"未知渲染图层: {layer!r}（已注册: {', '.join(known) or '无'}）"
        )


@dataclass(frozen=True)
class RenderPlan:
    """Debug 渲染的能力选择器（配置决定画哪些层，Python 决定怎么画）。

    三个字段均为字符串列表 / 元组：
      - debug_layers：主调试视图 _render_full 的图层序列
      - peep_layers ：精简视图 _render_peep 的图层序列
      - hud_fields  ：HUD 文本字段（stage/round/h/bid/rank 等）

    本类**不认识**任何 layer 的名字含义（zones/lane/nav_candidates 等对 core 而言
    只是字符串），也不解释 `hud_fields` 的具体取值——那是模块 renderer 的事。
    """

    debug_layers: tuple[str, ...] = ()
    peep_layers: tuple[str, ...] = ()
    hud_fields: tuple[str, ...] = ()

    def all_layers(self) -> tuple[str, ...]:
        """去重保序的图层全集（debug+peep），供 registry 校验用。"""
        seen: set[str] = set()
        out: list[str] = []
        for name in (*self.debug_layers, *self.peep_layers):
            if name not in seen:
                seen.add(name)
                out.append(name)
        return tuple(out)


class LayerRegistry:
    """通用渲染图层注册表：模块自注册，core 只 dispatch，不做图层内容判断。

    「核心：不出现 if layer == 'lane': ... elif layer == 'zones':」——那会把 racing
    领域污染进 core。registry 保证模块自由增删图层，core 零改动。
    """

    def __init__(self, render_plan: RenderPlan | None = None):
        self._layers: dict[str, Callable] = {}
        self._plan = render_plan
        if render_plan is not None:
            self.validate_layers(render_plan)

    # ---------------- 模块注册（模块 renderer 在 __init__ 自声明） ----------------

    def register(self, layer: str, draw_fn: Callable) -> None:
        """注册一个图层：图层名 → 绘制函数。可重复注册（后者覆盖）。"""
        if not isinstance(layer, str) or not layer:
            raise ValueError(f"图层名须为非空字符串，收到: {layer!r}")
        if not callable(draw_fn):
            raise ValueError(f"图层 {layer!r} 的绘制函数不可调用: {draw_fn!r}")
        self._layers[layer] = draw_fn

    def unregister(self, layer: str) -> None:
        """注销一个图层（不存在时静默）。"""
        self._layers.pop(layer, None)

    @property
    def registered_layers(self) -> tuple[str, ...]:
        """当前已注册的所有图层名（保序）。"""
        return tuple(self._layers)

    # ---------------- core dispatch（不认识图层内容） ----------------

    def draw(self, layer: str, *args, **kwargs):
        """调度 payload 到已注册的绘制函数；未注册图层抛 UnknownLayerError。"""
        fn = self._layers.get(layer)
        if fn is None:
            raise UnknownLayerError(layer, self.registered_layers)
        return fn(*args, **kwargs)

    def draw_all(self, layers: tuple[str, ...], *args, **kwargs) -> None:
        """按给定图层序列依次 dispatch（供 _render_full/_render_peep 复用）。"""
        for layer in layers:
            self.draw(layer, *args, **kwargs)

    # ---------------- capability validation（启动期，非运行期） ----------------

    def validate_layers(self, render_plan: RenderPlan) -> None:
        """启动期校验 render_plan 引用的每个图层都已注册；未知图层抛 UnknownLayerError。

        独立于 `_schema_ver`（格式版本）——这里只验「配置要求画的层，renderer 认不认
        得」，避免「配置写了 lane_v2 但 renderer 无此图层」拖到运行期空白/KeyError。
        """
        unknown = [n for n in render_plan.all_layers() if n not in self._layers]
        if unknown:
            raise UnknownLayerError(unknown[0], self.registered_layers)

    # ---------------- 与 plan 的关联 ----------------

    def bind_plan(self, render_plan: RenderPlan) -> RenderPlan:
        """把 render_plan 绑到本 registry 并做启动期 capability 校验，返回该 plan。"""
        self.validate_layers(render_plan)
        self._plan = render_plan
        return render_plan


def make_layer_factory() -> "LayerRegistry":
    """构造一个空 LayerRegistry（供模块 renderer 在 __init__ 期间 register 后使用）。"""
    return LayerRegistry()