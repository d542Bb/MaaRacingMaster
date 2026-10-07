# -*- coding: utf-8 -*-
"""MoGe-2 静态图 → 权重-only 量化（bits / block 可调）或纯 fp16。

默认配方对齐 quantize_metric_q4f16.py：fp16 scale 的 MatMulNBits 4-bit
block32 对称量化、Resize 留 fp32、IO fp32；`--bits 3|2` 只改位宽，
`--no-quant` 跳过量化只做半精度（质量上限档）。

MoGe-2 特有坑（2026-10-01 实测）：整图 fp16 后输出全 NaN——MoGe-2 编码器是
DINOv2 式 ViT，LayerNorm 导出为 ReduceMean/Sub/Pow/Sqrt 原语链，激活平方在
fp16（上限 65504）溢出 → inf → NaN。这些原语不在 ORT 默认 fp16 保护清单
（该清单只认 LayerNormalization 融合算子），须显式加 block list 让 norm 链
保持 fp32（norm 链计算量占比可忽略，时延影响可忽略）。

需 onnxruntime>=1.30 + onnx-ir（用临时 venv /d/venvs/q4tool 执行）。

用法：
    /d/venvs/q4tool/Scripts/python.exe tools/experiments/speedrush_vision/quantize_moge_q4f16.py <src.onnx>
    ... <src.onnx> --bits 3      # → <src>_q3f16.onnx
    ... <src.onnx> --no-quant    # → <src>_fp16.onnx
"""
from __future__ import annotations

import argparse
from pathlib import Path

import onnx
from onnxruntime.transformers import float16 as f16
from onnxruntime.quantization.matmul_nbits_quantizer import MatMulNBitsQuantizer

W = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\depth_review\weights")
DEFAULT_SRC = W / "moge2_vits_static_720x1280_t1032.onnx"

# LayerNorm 方差链（激活平方会破 fp16 上限 65504）保持 fp32；
# Sub/Div 数值安全不必挡——宽 block list 会触发转换器重复张量名缺陷
NORM_PRIMS = {"ReduceMean", "Pow", "Sqrt"}
BLOCK = set(f16.DEFAULT_OP_BLOCK_LIST) | {"Resize"} | NORM_PRIMS


def dedup_names(m: onnx.ModelProto) -> None:
    """宽 block list 下转换器对同一 fp32 张量按消费方各插一个 cast_to_fp32，
    输出张量重名（ORT 加载即拒）。各 Cast 输入相同、值恒等，重名输出改唯一名
    即可，无需改下游引用。"""
    seen_tensors: set[str] = set()
    seen_nodes: dict[str, int] = {}
    dup_i = 0
    for n in m.graph.node:
        if n.name in seen_nodes:
            seen_nodes[n.name] += 1
            n.name = f"{n.name}_{seen_nodes[n.name]}"
        else:
            seen_nodes[n.name] = 0
        for j, out in enumerate(n.output):
            if out in seen_tensors:
                dup_i += 1
                n.output[j] = f"{out}_dup{dup_i}"
            else:
                seen_tensors.add(out)


def prune_unused(m: onnx.ModelProto) -> None:
    """删掉无人消费的初始化器（折叠或转换留下的中间张量），避免白背体积。"""
    used: set[str] = set()
    for n in m.graph.node:
        used.update(i for i in n.input if i)
    used.update(o.name for o in m.graph.output)
    keep = [i for i in m.graph.initializer if i.name in used]
    n_drop = len(m.graph.initializer) - len(keep)
    if n_drop:
        del m.graph.initializer[:]
        m.graph.initializer.extend(keep)
        print(f"  剪掉无人消费的初始化器 {n_drop} 张")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=str(DEFAULT_SRC))
    ap.add_argument("--bits", type=int, default=4, help="MatMulNBits 位宽（4/3/2）")
    ap.add_argument("--block", type=int, default=32, help="MatMulNBits block 大小")
    ap.add_argument("--no-quant", action="store_true", help="跳过量化，只做 fp16")
    ap.add_argument("--tag", default=None, help="输出后缀（默认按档位自动命名）")
    args = ap.parse_args()

    src = Path(args.src)
    tag = args.tag if args.tag is not None else (
        "_fp16" if args.no_quant else f"_q{args.bits}f16")
    dst = src.with_name(f"{src.stem}{tag}.onnx")
    assert dst != src, "输出会覆盖源文件"

    if args.no_quant:
        m = onnx.load(str(src))
    else:
        q = MatMulNBitsQuantizer(str(src), bits=args.bits,
                                 block_size=args.block, is_symmetric=True)
        q.process()
        tmp = src.with_name(f"{src.stem}_q{args.bits}b{args.block}tmp.onnx")
        q.model.save_model_to_file(str(tmp), use_external_data_format=False)
        m = onnx.load(str(tmp))
        tmp.unlink()

    m = f16.convert_float_to_float16(m, keep_io_types=True, op_block_list=BLOCK)
    dedup_names(m)
    prune_unused(m)
    onnx.save(m, str(dst))
    print(f"已出：{dst}  ({dst.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
