# -*- coding: utf-8 -*-
"""core OCR 引擎回归锁：固定输入 → 逐字节稳定的文本；抠图边界与通道序可机检。

**为什么锁**：引擎段（关 det/cls、ORT 线程数、P-core 绑核、放大与对比度预处理）
从 `plugins/treasure/ocr.py` 搬到 [`core/ocr.py`](../maaracing_master/core/ocr.py)、
使 core 成为该引擎的唯一真源时，硬约束是**识别结果逐字节不变**。搬家用一次性
「重构前/后双跑对拍」证明过（6 张素材 × 23 个 ROI，含退化与越界矩形），此后靠
本文件守住——单次对拍不构成防线。

期望值**不是「正确的识别」，而是搬迁时的实际输出**（2026-09-17 实测）：本文件是
漂移报警器，识别率优劣由 `tools/experiments/` 的探针另行判定，别在此处"顺手纠正"。

**素材来源**：`tools/experiments/egg-claim-coin-read/claim_popup_0609.png` 是仓库里
仅有的入库真帧（720p 录屏帧），既有测试（`test_treasure_egg_claim`）已按「缺失即
skip」用它；ROI 取生产 policy 的 ocr 锚点 rect（值抄自 `treasure.policy.json`，
漂移锁另见 `test_navkit_runtime_golden`）。故本文件不新造任何资产。
"""
from __future__ import annotations

import pathlib
import types

import numpy as np
import pytest

from maaracing_master.core import ocr as core_ocr

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tools" / "experiments" / "egg-claim-coin-read" / "claim_popup_0609.png"

# {ROI 名: (归一化 rect, 实测文本)}；rect 抄自 treasure.policy.json 的 ocr 锚点
GOLDEN = {
    "settle_profit": (
        [0.68, 0.4895473251028807, 0.8992592592592593, 0.5592592592592592], "×250000"),
    "daily_high_score": (
        [0.31899779735682826, 0.5066372980910425, 0.6924559471365639, 0.6123348017621145],
        "2 300 蓝色彩激 黄色彩蛋"),
    # 无字区域：返回非 None、文本为空——调用方（raw_lines=['']）依赖这条语义
    "bid_player1": (
        [0.14956211453744492, 0.27895510141774954, 0.27450440528634357, 0.31130690161527164],
        ""),
}


def test_core_ocr_recognize_golden_on_real_frame():
    """真帧 + 生产 ROI → 文本逐字节一致（引擎搬迁的行为不变锁）。"""
    pytest.importorskip("rapidocr")
    from maaracing_master.core.image_io import read_rgb

    if not FIXTURE.exists():
        pytest.skip("实验真帧 fixtures 未入库")
    frame = read_rgb(FIXTURE)
    assert frame is not None, "真帧解码失败"

    engine = core_ocr.RapidOcrEngine()
    for name, (rect, expected) in GOLDEN.items():
        res = engine.recognize(frame, rect)
        assert res is not None, f"{name}: 矩形含像素时不得返回 None"
        assert res.text == expected, f"{name} 文本漂移: {res.text!r} != {expected!r}"
        # 关 det 时每 ROI 恒为一块；lines 与 text 两种取法须自洽
        assert res.lines == (expected,), f"{name} 逐块文本漂移: {res.lines!r}"


def test_core_ocr_engine_params_locked():
    """硬测出来的调参逐项上锁（搬家只搬值、不改值；改一项都会动性能或识别率）。

    这些数值的实测依据在 core/ocr.py 的常量注释里，不在本文重复；本文只做机检。
    """
    assert (core_ocr.USE_DET, core_ocr.USE_CLS) == (False, False)
    assert (core_ocr.OCR_INTRA_OP_THREADS, core_ocr.OCR_INTER_OP_THREADS) == (4, 1)
    assert core_ocr.PIN_P_CORE_AFFINITY == list(range(8))
    assert (core_ocr.TARGET_ROI_HEIGHT, core_ocr.UPSCALE_MAX,
            core_ocr.UPSCALE_HQ_THRESHOLD, core_ocr.CONTRAST_GAMMA) == (96, 6.0, 3.0, 1.15)


def _fake_engine(monkeypatch):
    """注入假引擎 + 短路预处理，使「交给引擎的数组」可被逐点检查（不加载 RapidOCR）。"""
    seen: list[np.ndarray] = []

    def _call(patch):
        seen.append(patch)
        return types.SimpleNamespace(txts=["x"])

    monkeypatch.setattr(core_ocr, "_preprocess_patch", lambda p: p)
    engine = core_ocr.RapidOcrEngine()
    engine._engine = _call
    return engine, seen


def test_core_ocr_crop_boundary_and_bgr_handoff(monkeypatch):
    """抠图边界（int 截断）与交引擎的通道序（BGR）逐点可机检。

    边界口径属行为保持的一部分：x2/y2 若改用 `roi_config` 的 ceil，ROI 会多吃一列/
    一行像素，识别文本随之漂移。通道序用「R/B 空间模式不同」的构造暴露任何多余翻转
    （灰白素材查不出通道写反，口径见 `test_image_io.py`）。
    """
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    frame[:, :, 0] = 10    # R
    frame[:, :, 2] = 200   # B
    engine, seen = _fake_engine(monkeypatch)

    res = engine.recognize(frame, [0.13, 0.27, 0.54, 0.87])
    assert res is not None
    (patch,) = seen
    # int 截断：x1=2, y1=2, x2=int(10.8)=10, y2=int(8.7)=8（ceil 口径会是 11/9）
    assert patch.shape == (6, 8, 3), f"抠图边界漂移: {patch.shape}"
    assert int(patch[0, 0, 0]) == 200 and int(patch[0, 0, 2]) == 10, "通道序不是 BGR"


def test_core_ocr_degenerate_rect_and_failed_engine_return_none(monkeypatch):
    """退化矩形不进推理、引擎不可用不抛——两条降级语义（调用方按 None 处理）。"""
    engine, seen = _fake_engine(monkeypatch)
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    assert engine.recognize(frame, [0.5, 0.5, 0.5, 0.5]) is None
    assert engine.recognize(frame, [0.0, 0.0, 0.0001, 0.0001]) is None
    assert seen == [], "退化矩形不得进入推理"

    dead = core_ocr.RapidOcrEngine()
    dead._engine_failed = True  # 加载失败后的固化状态
    assert dead.recognize(frame, [0.0, 0.0, 1.0, 1.0]) is None