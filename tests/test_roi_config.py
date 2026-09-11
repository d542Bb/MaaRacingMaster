# -*- coding: utf-8 -*-
"""ROIConfig 统一配置底座（P1a）单测。

覆盖三类契约：
1. 坐标契约 NormalizedROI：exclusive/top-left/normalized + floor/ceil 像素换算 + 越界报错
2. ROIConfig 阶段三段式访问：get_active_rois / get_global_anchors / get_detection_rois
3. schema 版本与结构校验

测试只依赖 core.roi_config（纯数据，不拉入 maa/opencv 等重依赖）。
"""
from __future__ import annotations

import math

import pytest

from maaracing_master.core.roi_config import NormalizedROI, ROIConfig, SCHEMA_VER


# ---------------- 坐标契约：NormalizedROI ----------------

class TestNormalizedROI:
    def test_construction_from_list(self):
        r = NormalizedROI.from_list([0.1, 0.2, 0.5, 0.6])
        assert (r.x1, r.y1, r.x2, r.y2) == (0.1, 0.2, 0.5, 0.6)
        assert isinstance(r, NormalizedROI)

    def test_kwarg_construction_and_as_list_roundtrip(self):
        r = NormalizedROI(x1=0.0, y1=0.0, x2=1.0, y2=1.0)
        assert r.as_list() == [0.0, 0.0, 1.0, 1.0]

    @pytest.mark.parametrize("rect", [
        [-0.1, 0.0, 0.5, 0.5],     # x1 < 0
        [0.5, 0.0, 1.1, 0.5],     # x2 > 1
        [0.5, 0.0, 0.1, 0.5],     # x1 >= x2（退化/倒置）
        [0.0, 0.5, 0.5, 0.5],     # y1 >= y2（退化）
        [0.5, 0.5, 0.5, 0.6],     # x1 == x2
        [0.0, -0.2, 0.5, 0.5],    # y1 < 0
        [0.0, 0.0, 0.5, 1.2],     # y2 > 1
    ])
    def test_invalid_rect_rejected(self, rect):
        with pytest.raises(ValueError):
            NormalizedROI.from_list(rect)

    def test_to_pixel_floor_ceil_exclusive(self):
        # W=1280, H=720；x2n=0.75*1280=960 恰整除 → ceil=960（exclusive 右边界）
        r = NormalizedROI(0.25, 0.5, 0.75, 0.75)
        assert r.to_pixel(1280, 720) == (320, 360, 960, 540)

    def test_to_pixel_floor_low_boundary(self):
        # 0.0001*1280=0.128 → floor=0；小矩形即使 <1px 也保证左边缘 0
        r = NormalizedROI(0.0001, 0.0001, 0.001, 0.001)
        x1, y1, x2, y2 = r.to_pixel(1280, 720)
        assert x1 == 0 and y1 == 0
        assert x2 == math.ceil(0.001 * 1280) and y2 == math.ceil(0.001 * 720)

    def test_to_pixel_clamps_to_canvas(self):
        r = NormalizedROI(0.99, 0.99, 1.0, 1.0)
        assert r.to_pixel(100, 100) == (99, 99, 100, 100)

    def test_to_pixel_returns_int_tuple(self):
        r = NormalizedROI(0.1, 0.1, 0.9, 0.9)
        out = r.to_pixel(800, 600)
        assert isinstance(out, tuple) and all(isinstance(v, int) for v in out)
        assert len(out) == 4

    def test_center_norm(self):
        r = NormalizedROI(0.2, 0.3, 0.6, 0.7)
        assert r.center_norm() == pytest.approx((0.4, 0.5))

    def test_from_list_wrong_length_rejected(self):
        with pytest.raises(ValueError):
            NormalizedROI.from_list([0.1, 0.2, 0.3])  # 长度 3
        with pytest.raises(ValueError):
            NormalizedROI.from_list([0.1, 0.2, 0.3, 0.4, 0.5])  # 长度 5

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError):
            NormalizedROI(x1="a", y1=0.0, x2=1.0, y2=1.0)
        with pytest.raises(ValueError):
            NormalizedROI(x1=True, y1=0.0, x2=1.0, y2=1.0)  # bool 视作非法数字


# ---------------- ROIConfig：阶段三段式访问 ----------------

_SCHEMA = {
    "_schema_ver": 1,
    "reference_size": [1280, 720],
    "rois": {
        "hall_peak_appraise_card": {"rect": [0.76, 0.80, 0.90, 0.89]},
        "select_card": {"rect": [0.40, 0.16, 0.60, 0.25], "threshold": 0.8},
        "round_big_banner": {
            "rect": [0.39, 0.42, 0.60, 0.58],
            "templates": ["round1_banner.png", "round2_banner.png"],
        },
    },
    "stages": {
        "order": ["大厅", "选择", "第1回合"],
        "global_anchors": ["hall_peak_appraise_card"],
        "definitions": {
            "大厅": {"active_rois": []},
            "选择": {"active_rois": ["select_card"]},
            "第1回合": {"active_rois": ["round_big_banner"]},
        },
    },
}


class TestROIConfig:
    def test_from_dict_loads(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        assert cfg.schema_ver == 1
        assert cfg.reference_size == (1280, 720)

    def test_stage_order(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        assert cfg.stage_order == ("大厅", "选择", "第1回合")

    def test_get_active_rois(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        assert cfg.get_active_rois("大厅") == ()
        assert cfg.get_active_rois("选择") == ("select_card",)
        assert cfg.get_active_rois("第1回合") == ("round_big_banner",)

    def test_get_global_anchors_constant(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        assert cfg.global_anchors == ("hall_peak_appraise_card",)
        assert cfg.get_global_anchors() == ("hall_peak_appraise_card",)

    def test_get_detection_rois_global_anchors_always_included(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        # 大厅 active_rois 为空，但 global_anchor 恒在
        assert cfg.get_detection_rois("大厅") == ("hall_peak_appraise_card",)
        assert cfg.get_detection_rois("选择") == (
            "hall_peak_appraise_card", "select_card",
        )
        assert cfg.get_detection_rois("第1回合") == (
            "hall_peak_appraise_card", "round_big_banner",
        )

    def test_duplicate_anchor_dedup_keeps_order(self):
        # 同一 ROI 同时作为 global_anchor 与 active_rois 时只出现一次
        schema = {
            "_schema_ver": 1,
            "reference_size": [1280, 720],
            "rois": {"a": {"rect": [0.0, 0.0, 0.1, 0.1]}},
            "stages": {
                "order": ["s1"],
                "global_anchors": ["a"],
                "definitions": {"s1": {"active_rois": ["a"]}},
            },
        }
        cfg = ROIConfig.from_dict(schema)
        assert cfg.get_detection_rois("s1") == ("a",)

    def test_get_rect(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        rect = cfg.get_rect("select_card")
        assert rect == NormalizedROI(0.40, 0.16, 0.60, 0.25)

    def test_get_rect_unknown_raises(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        with pytest.raises(KeyError):
            cfg.get_rect("no_such_roi")

    def test_unknown_stage_raises(self):
        cfg = ROIConfig.from_dict(_SCHEMA)
        with pytest.raises(KeyError):
            cfg.get_active_rois("不存在的阶段")
        with pytest.raises(KeyError):
            cfg.get_detection_rois("不存在的阶段")


# ---------------- schema 校验 ----------------

class TestROIConfigValidation:
    def test_missing_schema_ver_rejected(self):
        schema = dict(_SCHEMA)
        schema.pop("_schema_ver")
        with pytest.raises(ValueError):
            ROIConfig.from_dict(schema)

    def test_non_int_schema_ver_rejected(self):
        schema = dict(_SCHEMA)
        schema["_schema_ver"] = "1"
        with pytest.raises(ValueError):
            ROIConfig.from_dict(schema)

    def test_bad_reference_size_rejected(self):
        schema = dict(_SCHEMA)
        schema["reference_size"] = [1280]  # 长度不足
        with pytest.raises(ValueError):
            ROIConfig.from_dict(schema)

    def test_roi_without_rect_rejected(self):
        schema = dict(_SCHEMA)
        schema["rois"] = {"bad": {"templates": []}}  # 缺 rect
        with pytest.raises(ValueError):
            ROIConfig.from_dict(schema)

    def test_roi_invalid_rect_rejected_at_load(self):
        schema = dict(_SCHEMA)
        schema["rois"] = {"bad": {"rect": [1.5, 0.0, 2.0, 0.5]}}  # 越界
        with pytest.raises(ValueError):
            ROIConfig.from_dict(schema)

    def test_order_not_list_rejected(self):
        schema = dict(_SCHEMA)
        schema["stages"] = dict(_SCHEMA["stages"])
        schema["stages"]["order"] = "大厅,选择"  # 非数组
        with pytest.raises(ValueError):
            ROIConfig.from_dict(schema)

    def test_global_anchors_optional_defaults_empty(self):
        schema = dict(_SCHEMA)
        stages = dict(_SCHEMA["stages"])
        stages.pop("global_anchors")
        schema["stages"] = stages
        cfg = ROIConfig.from_dict(schema)
        assert cfg.global_anchors == ()

    def test_active_rois_optional_defaults_empty(self):
        schema = dict(_SCHEMA)
        stages = dict(_SCHEMA["stages"])
        definitions = {k: dict(v) for k, v in stages["definitions"].items()}
        definitions["大厅"].pop("active_rois")
        stages["definitions"] = definitions
        schema["stages"] = stages
        cfg = ROIConfig.from_dict(schema)
        assert cfg.get_active_rois("大厅") == ()

    def test_threshold_does_not_break_load(self):
        # 合法阈值可正常加载；越界阈值不视为 schema 结构错误（忽略，不抛错）
        schema = dict(_SCHEMA)
        assert ROIConfig.from_dict(schema).get_rect("select_card")  # threshold=0.8 正常
        schema["rois"]["select_card"]["threshold"] = 1.5  # 越界阈值
        assert ROIConfig.from_dict(schema).get_rect("select_card")  # 仍可加载


def test_schema_ver_constant():
    # 当前声明的格式版本须与配置一致（内部一致性护栏）
    assert SCHEMA_VER == 1