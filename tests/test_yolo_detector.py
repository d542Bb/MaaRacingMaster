# -*- coding: utf-8 -*-
"""yolo_detector 类别参数化的回归锁（阶段 B 前置改造）。

锁住三件容易错的事：
1. **类别表来源**——默认从模型 ONNX 元数据（Ultralytics 导出的 names）读取，
   core 不再内置任何游戏的类别名；classes 参数可显式覆盖；
2. **class_conf 按键校验**——按类别名给阈值，含未知类名立即报错（防止静默无效）；
3. **返回形状契约**——(by_class, detections, all_raw_dets) 三元组，
   by_class 对每个已声明类别都有键（空结果为空列表，调用方不必判缺键）。

_to_dets 的分组与跳过逻辑用裸实例直测（不经 session）；
构造/元数据测试需要 archive 内的 racing 模型与 onnxruntime，缺则整文件跳过。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

try:
    from maaracing_master.core.yolo_detector import YOLODetector
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺 onnxruntime 的机器上整文件 SKIP
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"yolo_detector 导入失败: {_ERR}")

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "archive" / "racing" / "resources" / "onnx" / "model.onnx"

needs_model = pytest.mark.skipif(
    not MODEL.exists(), reason="racing 归档模型不在树内（或资源目录未部署）")


def _bare_detector(classes: dict[int, str]) -> YOLODetector:
    """绕过 __init__（不加载 session）构造只够 _to_dets 用的实例"""
    det = YOLODetector.__new__(YOLODetector)
    det.classes = classes
    return det


class TestToDets:
    def test_groups_by_class_name(self):
        det = _bare_detector({0: "coin", 1: "car", 2: "bonus_car"})
        xyxy = np.array([[10.0, 20.0, 30.0, 40.0], [0.0, 0.0, 50.0, 50.0]])
        scores = np.array([0.9, 0.8])
        cls = np.array([0, 2])
        by_class, dets = det._to_dets(
            xyxy, scores, cls, 0, 0, 1.0, 0, 0, 100, 100, [0, 1])
        assert by_class == {"coin": [(20, 30, 20, 20)], "car": [],
                            "bonus_car": [(25, 25, 50, 50)]}
        assert [d["class_name"] for d in dets] == ["coin", "bonus_car"]
        assert dets[0]["box"] == (10, 20, 30, 40)
        assert dets[0]["confidence"] == pytest.approx(0.9)

    def test_unknown_class_id_skipped(self):
        """模型输出类别维多于声明类别：未声明 id 不进任何结果"""
        det = _bare_detector({0: "coin"})
        xyxy = np.array([[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]])
        scores = np.array([0.9, 0.9])
        cls = np.array([0, 5])
        by_class, dets = det._to_dets(
            xyxy, scores, cls, 0, 0, 1.0, 0, 0, 100, 100, [0, 1])
        assert by_class == {"coin": [(2, 3, 2, 2)]}
        assert len(dets) == 1

    def test_min_score_filters(self):
        det = _bare_detector({0: "coin", 1: "car"})
        xyxy = np.array([[0.0, 0.0, 10.0, 10.0], [0.0, 0.0, 4.0, 4.0]])
        scores = np.array([0.9, 0.3])
        cls = np.array([0, 1])
        by_class, dets = det._to_dets(
            xyxy, scores, cls, 0, 0, 1.0, 0, 0, 100, 100, [0, 1], min_score=0.5)
        assert by_class["car"] == []
        assert len(dets) == 1


# onnxruntime-directml（1.24.4）原生缺陷：任一 session 析构后再对另一 session
# run() 会段错误（CPU provider 无此问题，多 session 全存活亦无）。测试必然创建
# 多个 detector，故全部保活至进程结束，杜绝 run 之前的任何析构。
_KEEP_ALIVE: list = []


@needs_model
class TestModelDrivenContract:
    @pytest.fixture(scope="module")
    def detector(self):
        det = YOLODetector(str(MODEL), conf=0.4)
        _KEEP_ALIVE.append(det)
        return det

    def test_classes_from_metadata(self, detector):
        assert detector.classes == {0: "coin", 1: "car", 2: "bonus_car"}
        assert detector._conf_by_id == {}

    def test_class_conf_keyed_by_name(self):
        det = YOLODetector(str(MODEL), conf=0.5, class_conf={"coin": 0.2})
        _KEEP_ALIVE.append(det)
        assert det._conf_by_id == {0: 0.2}

    def test_class_conf_unknown_name_raises(self):
        # 显式 classes 时校验在模型加载前 fail-fast：不产生 session，无析构风险
        with pytest.raises(ValueError, match="未知类别名"):
            YOLODetector(str(MODEL), classes=["coin", "car", "bonus_car"],
                         class_conf={"nope": 0.1})

    def test_empty_classes_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            YOLODetector(str(MODEL), classes={})

    def test_explicit_classes_override(self):
        det = YOLODetector(str(MODEL), conf=0.4, classes=["a", "b", "c"])
        _KEEP_ALIVE.append(det)
        assert det.classes == {0: "a", 1: "b", 2: "c"}

    def test_call_returns_three_tuple(self, detector):
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        by_class, dets, raw = detector(img)
        assert set(by_class) == {"coin", "car", "bonus_car"}
        assert all(isinstance(v, list) for v in by_class.values())
        assert isinstance(dets, list) and isinstance(raw, list)
