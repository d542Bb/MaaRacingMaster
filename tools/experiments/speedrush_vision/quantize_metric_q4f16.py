# -*- coding: utf-8 -*-
"""Metric ONNX → q4f16 量化（与现役 depth_small_q4f16 同配方：MatMulNBits
4-bit 权重、block 32、激活 fp32；f16 为 DML 内部计算精度）。

需 onnx>=1.23 且 onnxruntime>=1.30（含 onnx_ir）——用临时 torch 环境（da2_ft2）
执行，仓库 .venv 的 ORT 1.24.4 无此量化路径。

用法：
    <da2_ft2 venv>/python.exe tools/experiments/speedrush_vision/quantize_metric_q4f16.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from onnxruntime.quantization.matmul_nbits_quantizer import MatMulNBitsQuantizer

W = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\depth_review\weights")

for src in sys.argv[1:] or [W / "metric_vkitti_vits_336.onnx"]:
    src = Path(src)
    dst = src.with_name(src.stem + "_q4f16.onnx")
    q = MatMulNBitsQuantizer(str(src), bits=4, block_size=32)
    q.process()
    q.model.save_model_to_file(str(dst), use_external_data_format=False)
    print(f"已出：{dst}  ({dst.stat().st_size/1e6:.1f} MB)")
