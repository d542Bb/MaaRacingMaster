# -*- coding: utf-8 -*-
"""run25b：对真机 raw 帧离线跑与生产完全同配置的 RapidOCR，取证槽位 0 命中。

链上事实：bid_player1-4 rect 裁剪位置正确、数字肉眼清晰，但生产 OCR hits≈0。
本脚本排除「帧/rect」变量，直接复现 recognize_single 全链（抠图→预处理→
rec→金额提取），输出每区原文与解析值——若离线也读不出，问题在引擎输入
（预处理/尺寸）；若离线能读出，问题在生产运行时（帧内容/线程/时序）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_assistant.plugins.treasure.ocr import (  # noqa: E402
    _preprocess_patch, _extract_amount, USE_DET, USE_CLS,
    OCR_INTRA_OP_THREADS, OCR_INTER_OP_THREADS, MIN_AMOUNT,
)

DEBUG_DIR = Path(os.environ["APPDATA"]) / "MaaRacingAssistant" / "debug/treasure/20260909_201348"
RECTS = {
    "bid_result_amount_box": (0.3392592592592592, 0.6505349794238683, 0.5377777777777778, 0.7265020576131688),
    "bid_player1": (0.14956211453744492, 0.27895510141774954, 0.27450440528634357, 0.31130690161527164),
    "bid_player2": (0.14765814977973568, 0.44098739990204877, 0.2753303964757709, 0.4787077826725404),
    "bid_player3": (0.1509881057268722, 0.6068187593170778, 0.2753881057268722, 0.6421001784659194),
    "bid_player4": (0.15016211453744494, 0.7729338182647975, 0.2737621145374449, 0.8099005084652158),
}


def main():
    from rapidocr import RapidOCR
    engine = RapidOCR(params={
        "Global.use_det": USE_DET,
        "Global.use_cls": USE_CLS,
        "EngineConfig.onnxruntime.intra_op_num_threads": OCR_INTRA_OP_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": OCR_INTER_OP_THREADS,
    })
    for fid in (sys.argv[1:] or ["1276"]):
        raw = DEBUG_DIR / "raw" / f"{int(fid):04d}_raw.jpg"
        bgr = cv2.imread(str(raw))
        if bgr is None:
            print(f"[skip] {raw}")
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        print(f"--- 帧#{fid} {W}x{H} (raw jpg, BGR→RGB 与生产一致) ---")
        for key, (x1n, y1n, x2n, y2n) in RECTS.items():
            x1, y1 = int(x1n * W), int(y1n * H)
            x2, y2 = int(x2n * W), int(y2n * H)
            patch = rgb[y1:y2, x1:x2]
            pb = cv2.cvtColor(patch, cv2.COLOR_RGB2BGR)
            pb = _preprocess_patch(pb)
            out = engine(pb)
            txts = getattr(out, "txts", None) or []
            text = "".join(str(t) for t in txts)
            amt = _extract_amount(text)
            print(f"  {key}: patch={patch.shape[1]}x{patch.shape[0]} "
                  f"预处理后={pb.shape[1]}x{pb.shape[0]} text={text!r} amount={amt} "
                  f"(raw_lines={[str(t) for t in txts]})")


if __name__ == "__main__":
    main()
