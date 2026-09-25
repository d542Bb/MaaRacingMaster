# -*- coding: utf-8 -*-
"""Metric ONNX → q4f16 量化（对齐现役 depth_small_q4f16 结构：fp16 scale 的
MatMulNBits 4-bit block32、对称量化无 zero_points；Resize 留 fp32；IO fp32）。

三个坑（2026-09-25 实测定位）：
① fp32 scale 直接量化：DML 走慢路径（55ms vs 14ms），且体积 27.6MB——
  必须整体 fp16 转换让 scale 随图变 fp16；
② ORT 1.24.4 CPU 无 fp16 cubic Resize 核 → fp16 转换必须把 Resize 排除
  （否则常量折叠失败、pos_embed 插值每帧落 CPU，222ms 级灾难）；
③ 量化器默认 is_symmetric=False 出 zero_points → DML 对「fp16 scale+
  zero_points」组合整图输出 NaN（CPU 正常）——必须 is_symmetric=True，
  与现役结构一致（现役无 zero_points 输入）。

需 onnxruntime>=1.30（含 onnx_ir、transformers.float16）——用临时 torch
环境（da2_ft2）执行，仓库 .venv 的 1.24.4 无此量化路径。

用法：
    <da2_ft2 venv>/python.exe tools/experiments/speedrush_vision/quantize_metric_q4f16.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import onnx
from onnxruntime.transformers import float16 as f16
from onnxruntime.quantization.matmul_nbits_quantizer import MatMulNBitsQuantizer

W = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\depth_review\weights")

BLOCK = set(f16.DEFAULT_OP_BLOCK_LIST) | {"Resize"}

for src in sys.argv[1:] or [W / "metric_vkitti_vits_336.onnx"]:
    src = Path(src)
    stem = src.stem
    # 1) fp32 图上对称 4-bit 量化（无 zero_points，结构对齐现役）
    q = MatMulNBitsQuantizer(str(src), bits=4, block_size=32, is_symmetric=True)
    q.process()
    q.model.save_model_to_file(str(src.with_name(f"{stem}_symq4tmp.onnx")),
                               use_external_data_format=False)
    # 2) 整体 fp16 转换（scale 随图转 fp16；Resize 留 fp32 保常量折叠）
    m = onnx.load(str(src.with_name(f"{stem}_symq4tmp.onnx")))
    m = f16.convert_float_to_float16(m, keep_io_types=True, op_block_list=BLOCK)
    dst = src.with_name(f"{stem}_q4f16.onnx")
    onnx.save(m, str(dst))
    src.with_name(f"{stem}_symq4tmp.onnx").unlink()
    print(f"已出：{dst}  ({dst.stat().st_size/1e6:.1f} MB)")

