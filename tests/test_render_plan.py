# -*- coding: utf-8 -*-
"""RenderPlan + LayerRegistry（统一计划 P2a）单测。

覆盖：RenderPlan 三层字段与 all_layers 去重；LayerRegistry 模块自注册 + core 纯
dispatch（不判断图层内容）+ 未注册 KeyError；capability validation（validate_layers
启动期报错未知图层，独立于格式版本）。只依赖 core.render_plan，不拉入重依赖。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.render_plan import (
    LayerRegistry,
    RenderPlan,
    UnknownLayerError,
)

_PLAN = RenderPlan(
    debug_layers=("zones", "nav_candidates", "button", "cursor", "templates",
                  "raw_dets", "yolo_dets", "lane", "racing_hud"),
    peep_layers=("zones", "yolo_dets", "lane", "cursor", "button", "racing_hud"),
    hud_fields=("stage", "round", "h", "bid", "rank"),
)


# ---------------- RenderPlan：能力选择器 ----------------

class TestRenderPlan:
    def test_fields(self):
        assert _PLAN.debug_layers[0] == "zones"
        assert "racing_hud" in _PLAN.peep_layers
        assert _PLAN.hud_fields == ("stage", "round", "h", "bid", "rank")

    def test_all_layers_dedups_preserving_order(self):
        # debug+peep 并集去重保序（zones 在 debug 首、peep 也含，只出现一次）
        assert _PLAN.all_layers()[0] == "zones"
        assert _PLAN.all_layers().count("zones") == 1
        assert "lane" in _PLAN.all_layers()

    def test_layers_are_just_strings(self):
        # core 不解释图层名含义——任意未知字符串也合法（校验归 registry）
        p = RenderPlan(debug_layers=("whatever",), peep_layers=(), hud_fields=())
        assert p.all_layers() == ("whatever",)

    def test_empty_plan(self):
        p = RenderPlan()
        assert p.all_layers() == ()


# ---------------- LayerRegistry：模块自注册 + core dispatch ----------------

_BUILT = {layer: f"fn:{layer}" for layer in _PLAN.all_layers()}


class _FakeRenderer(LayerRegistry):
    """模拟模块 renderer：在 __init__ 期间自注册各图层（draw 返回标记）。"""

    def __init__(self):
        super().__init__()
        for layer, tag in _BUILT.items():
            self.register(layer, self._make_draw(tag))

    @staticmethod
    def _make_draw(tag: str):
        return lambda *a, **k: tag


class TestLayerRegistry:
    def test_register_and_dispatch(self):
        r = LayerRegistry()
        r.register("zones", lambda *a, **k: "z")
        assert r.draw("zones") == "z"
        assert r.registered_layers == ("zones",)

    def test_dispatch_unknown_raises(self):
        r = LayerRegistry()
        with pytest.raises(UnknownLayerError):
            r.draw("nope")

    def test_register_invalid_rejected(self):
        r = LayerRegistry()
        with pytest.raises(ValueError):
            r.register("", lambda: None)          # 空名
        with pytest.raises(ValueError):
            r.register("foo", "not-callable")     # 不可调用

    def test_re_register_overrides(self):
        r = LayerRegistry()
        r.register("a", lambda: 1)
        r.register("a", lambda: 2)
        assert r.draw("a") == 2

    def test_unregister(self):
        r = LayerRegistry()
        r.register("a", lambda: 1)
        r.unregister("a")
        assert r.registered_layers == ()
        r.unregister("missing")  # 静默

    def test_draw_all_sequence(self):
        r = LayerRegistry()
        calls = []
        r.register("a", lambda **k: calls.append("a"))
        r.register("b", lambda **k: calls.append("b"))
        r.draw_all(("a", "b", "a"))
        assert calls == ["a", "b", "a"]

    def test_core_does_not_judge_layer_content(self):
        # 模块自注册任意名字图层，core 纯 dispatch——不认识内容也照常调用
        r = LayerRegistry()
        r.register("lane", lambda tag=None: tag)
        assert r.draw("lane", "ctx") == "ctx"


# ---------------- capability validation（启动期） ----------------

class TestCapabilityValidation:
    def test_fake_renderer_dispatch_all_plan_layers(self):
        # 模块自注册后，plan.debug 的每层都能被调度（不报错）
        r = _FakeRenderer()
        for layer in _PLAN.debug_layers:
            assert r.draw(layer).startswith("fn:")
        for layer in _PLAN.peep_layers:
            assert r.draw(layer).startswith("fn:")

    def test_validate_layers_all_registered_passes(self):
        r = _FakeRenderer()
        # 不抛错
        r.validate_layers(_PLAN)

    def test_validate_layers_unknown_fails_at_startup(self):
        r = _FakeRenderer()
        bad = RenderPlan(debug_layers=("zones", "lane_v2"), peep_layers=(), hud_fields=())
        with pytest.raises(UnknownLayerError):
            r.validate_layers(bad)

    def test_validate_message_lists_known(self):
        r = _FakeRenderer()
        bad = RenderPlan(debug_layers=("missing_layer",), peep_layers=(), hud_fields=())
        with pytest.raises(UnknownLayerError) as exc:
            r.validate_layers(bad)
        assert "missing_layer" in str(exc.value)
        assert "zones" in str(exc.value)  # 已知图层在错误信息里，便于定位

    def test_constructor_with_plan_validates_immediately(self):
        with pytest.raises(UnknownLayerError):
            LayerRegistry(RenderPlan(debug_layers=("ghost",), peep_layers=(), hud_fields=()))

    def test_bind_plan_returns_and_validates(self):
        r = LayerRegistry()
        r.register("a", lambda: 1)
        plan = RenderPlan(debug_layers=("a",), peep_layers=(), hud_fields=())
        assert r.bind_plan(plan) is plan
        with pytest.raises(UnknownLayerError):
            r.bind_plan(RenderPlan(debug_layers=("b",), peep_layers=(), hud_fields=()))