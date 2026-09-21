# -*- coding: utf-8 -*-
"""阶段 B 物品感知层：驾驶帧 → 候选框流（街车 / 金币 / 奖励车）。

**分层归属**：类别语义（car/coin/bonus_car 的名字、阈值、模型资产）是**本插件**的业务
声明——模型由本插件自带（``resources/onnx/model.onnx``，许可声明见该目录 README），
推理复用 ``core.yolo_detector``（方向 plugin→core，红线 1）。core 不知道任何类别名。

**输出契约**（供世界模型层消费，control-route §一 分层表「物品感知」行）：
- 只给**原始像素框**（cx/cy/w/h + conf），不做归一化、不排距离——「框按 y 排距离、
  按 x 排横向」是世界模型层的职责（A1 尺子口径见 ``CODE_WIKI.md`` §6），本层不越层；
- 全帧推理、不设 ROI：街车重测（2026-09-21，28329 帧）实证框 92–99% 自然落在路面带、
  自车区零误检，硬编码 ROI 反而挡住远处目标；
- ``infer_ms`` 随结果产出：控制回路 P50/P95 记账的数据源（验收判据 §二.3）。

**阈值口径**：conf=0.35 = 旧 racing 栈 CLASS_CONF 生效值；街车充分性重测与 τ 到场轮
素材均在此口径下成立（证据 commit `8f59837` / `3847fae`，过程记录在实验 README）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np

from maaracing_master.core.yolo_detector import YOLODetector

# 模型类别语义（与 resources/onnx/model.onnx 的 names 元数据一致；
# 构造时校验，防止换模型后静默错映射）
CLASS_CARS = "car"
CLASS_COINS = "coin"
CLASS_BONUS = "bonus_car"

CONF = 0.35   # 旧栈 CLASS_CONF 生效口径，见模块 docstring
IOU = 0.45


class Detection(NamedTuple):
    """单个检测框（像素坐标，全帧系）。"""

    cx: int
    cy: int
    w: int
    h: int
    conf: float


@dataclass
class PerceptionResult:
    """一帧的物品感知输出。"""

    frame_id: int
    ts_ns: int
    cars: list[Detection] = field(default_factory=list)
    coins: list[Detection] = field(default_factory=list)
    bonuses: list[Detection] = field(default_factory=list)
    infer_ms: float = 0.0


def _to_detections(dets: list[dict], class_name: str) -> list[Detection]:
    out = []
    for d in dets:
        if d["class_name"] != class_name:
            continue
        x1, y1, x2, y2 = d["box"]
        out.append(Detection(
            cx=(x1 + x2) // 2, cy=(y1 + y2) // 2,
            w=x2 - x1, h=y2 - y1, conf=d["confidence"]))
    return out


class StreetPerception:
    """街面物品感知器。构造即加载模型（秒级），故只在进入驾驶阶段时创建一次。"""

    def __init__(self, model_path: str):
        self.det = YOLODetector(model_path, conf=CONF, iou=IOU)
        missing = {CLASS_CARS, CLASS_COINS, CLASS_BONUS} - set(self.det.classes.values())
        if missing:
            raise ValueError(
                f"模型类别表缺感知层必需类别 {sorted(missing)}；"
                f"实际为 {self.det.classes}")

    def detect(self, frame_rgb: np.ndarray, frame_id: int = 0, ts_ns: int = 0) -> PerceptionResult:
        """frame_rgb：驾驶帧（RGB，与采集/录制同一像素序）。"""
        t0 = time.perf_counter()
        _by_class, dets, _raw = self.det(frame_rgb)
        infer_ms = (time.perf_counter() - t0) * 1000.0
        return PerceptionResult(
            frame_id=frame_id, ts_ns=ts_ns,
            cars=_to_detections(dets, CLASS_CARS),
            coins=_to_detections(dets, CLASS_COINS),
            bonuses=_to_detections(dets, CLASS_BONUS),
            infer_ms=infer_ms)
