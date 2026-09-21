# -*- coding: utf-8 -*-
"""speedrush 物品感知层（阶段 B 第一块）的契约回归锁。

锁住三件事：
1. **输出契约**——detect 返回 PerceptionResult：按类别分组的 Detection(cx,cy,w,h,conf)，
   box(x1y1x2y2)→中心/宽高的换算正确、类别过滤正确、frame_id/ts_ns 透传；
2. **类别表校验**——模型缺必需类别（car/coin/bonus_car）时构造即报错，
   防止换模型后静默错映射（街车重测/阈值复测的结论都挂在这三个名字上）；
3. **插件声明面**——REQUIRED_ASSETS 与 perception_mode 配置键在位
   （profile 白名单机制：不加进 DEFAULT_MODULE_CONFIG 就不会被保存）。

感知 import 依赖 onnxruntime（core.yolo_detector 顶层 import），CI 轻量依赖集缺失时
整文件跳过（与 test_yolo_detector 同惯例）。
"""

from __future__ import annotations

import numpy as np
import pytest

try:
    from maaracing_master.plugins.speedrush import perception as perc
    from maaracing_master.plugins.speedrush.perception import Detection, StreetPerception
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺重依赖的机器上整文件 SKIP
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"perception 导入失败: {_ERR}")


class FakeDetector:
    """替身：记录入参、回放预置的 (by_class, dets, raw)。"""

    def __init__(self, classes, dets):
        self.classes = classes
        self._dets = dets
        self.calls = []

    def __call__(self, img_rgb, roi=None):
        self.calls.append((img_rgb.shape, roi))
        by_class = {name: [] for name in self.classes.values()}
        for d in self._dets:
            by_class[d["class_name"]].append(
                ((d["box"][0] + d["box"][2]) // 2, (d["box"][1] + d["box"][3]) // 2,
                 d["box"][2] - d["box"][0], d["box"][3] - d["box"][1]))
        return by_class, list(self._dets), []


def _make(monkeypatch, classes, dets) -> StreetPerception:
    fake = FakeDetector(classes, dets)
    monkeypatch.setattr(perc, "YOLODetector", lambda *a, **k: fake)
    return StreetPerception("dummy/model.onnx")


DETS = [
    {"box": (100, 300, 160, 380), "confidence": 0.9, "class_name": "car"},
    {"box": (640, 60, 660, 80), "confidence": 0.5, "class_name": "coin"},
    {"box": (200, 250, 300, 350), "confidence": 0.7, "class_name": "bonus_car"},
]
CLASSES = {0: "car", 1: "coin", 2: "bonus_car"}


class TestDetectContract:
    def test_groups_and_conversion(self, monkeypatch):
        sp = _make(monkeypatch, CLASSES, DETS)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        r = sp.detect(frame, frame_id=42, ts_ns=12345)
        assert r.frame_id == 42 and r.ts_ns == 12345
        assert r.cars == [Detection(cx=130, cy=340, w=60, h=80, conf=0.9)]
        assert r.coins == [Detection(cx=650, cy=70, w=20, h=20, conf=0.5)]
        assert r.bonuses == [Detection(cx=250, cy=300, w=100, h=100, conf=0.7)]
        assert r.infer_ms > 0

    def test_empty_frame_yields_empty_lists(self, monkeypatch):
        sp = _make(monkeypatch, CLASSES, [])
        r = sp.detect(np.zeros((64, 64, 3), dtype=np.uint8))
        assert r.cars == [] and r.coins == [] and r.bonuses == []

    def test_full_frame_no_roi(self, monkeypatch):
        # 契约：全帧推理、不设 ROI（街车重测口径；世界模型层才做 y 过滤）
        sp = _make(monkeypatch, CLASSES, [])
        fake = sp.det
        sp.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
        assert fake.calls == [(((720, 1280, 3)), None)]


class TestClassValidation:
    def test_missing_class_raises(self, monkeypatch):
        with pytest.raises(ValueError, match="bonus_car"):
            _make(monkeypatch, {0: "car", 1: "coin"}, [])


class TestPluginDeclarations:
    """模块 import 链含 maa（ActivityModule 基类），缺依赖的机器上跳过。"""

    def _module(self):
        import importlib
        m = importlib.import_module("maaracing_master.plugins.speedrush.module")
        return m.SpeedRushModule

    def test_required_assets_declares_model(self):
        pytest.importorskip("maa")
        assert self._module().REQUIRED_ASSETS == ("resources/onnx/model.onnx",)

    def test_perception_mode_in_config_whitelist(self):
        pytest.importorskip("maa")
        cfg = self._module().DEFAULT_MODULE_CONFIG
        assert cfg["perception_mode"] is False
        assert cfg["record_mode"] is False
