# -*- coding: utf-8 -*-
"""MoGe-2 官方动态 ONNX → 本机静态折叠版（图手术，不依赖 torch）。

病灶（2026-10-01 profiler 实测，@336x598 tokens=1032）：动态图在 DML 上
/encoder/Resize_1 等「值依赖 shape」算子跑出病态慢（单 Resize 104ms/拍），
ViT 本体 MatMul 反而只 ~15ms——与 UniDepth「静态单节点融合才正常」同机理。
num_tokens 是真实输入标量，其派生子树（base_h/base_w → token 网格 →
pos 编码网格）在固定 (H,W,tokens) 下全是常量；本脚本按拓扑序收集
「全部输入为常量或纯派生」的节点，子图求值捕获其值，替换为初始化器并
删除子树，整图变静态。

`--keep-ops` 是折叠闸门（默认 `Conv`）：列出的算子类型永不折叠，其输入仍按纯
子树烘成常量。用途是挡住「尺寸派生但输出随特征图走」的算子——这类节点被折叠会
把上千个参数烘成上千万个稠密常量（`/neck/input_blocks.1~4` 的坐标网格 1×1 卷积
即此形，全折叠时 fp16 权重多出 30.3MB、每帧只省约 0.7% 前向），而它们的每帧
重算量只有 ViT-S 前向的百分之一量级。传空串恢复全折叠（旧配方，产物与历史
`moge2_vits_static_<h>x<w>_t<tok>.onnx` 逐字节一致）。

**`--h/--w` 是模型内部工作分辨率，不是输入尺寸**：它决定输出点图的档位——
按 336×598 折出的模型原生输出 336×598（产线档，再由 `depth_geo.infer_points`
升采样回全幅），按 720×1280 折出的原生输出 720×1280（头部算力约四倍）。
输入仍由会话的 free-dim 覆盖钉成 720×1280，故两档的图输入形状相同。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/moge2_static_fold.py \
        --h 336 --w 598 --tokens 1032
输出：<depth_review>/weights/moge2_vits_static_<h>x<w>_t<tok><tag>.onnx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper, shape_inference

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402

WEIGHTS = eph.OUT.parent / "weights" / "moge2_vits.onnx"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--h", type=int, default=336)
    ap.add_argument("--w", type=int, default=598)
    ap.add_argument("--tokens", type=int, default=1032)
    ap.add_argument("--keep-ops", default="Conv",
                    help="逗号分隔的算子类型，永不折叠；空串=全折叠（旧配方）")
    ap.add_argument("--tag", default="", help="输出文件名后缀（避免覆盖既有产物）")
    args = ap.parse_args()
    keep_ops = {s for s in args.keep_ops.split(",") if s}

    m = onnx.load(str(WEIGHTS))
    g = m.graph

    # 预处理：Constant 节点 → 初始化器（导出器两种混用）
    keep_nodes = []
    for n in g.node:
        if n.op_type == "Constant":
            a = helper.get_attribute_value(
                next(a for a in n.attribute if a.name == "value"))
            g.initializer.append(numpy_helper.from_array(
                numpy_helper.to_array(a), n.output[0]))
        else:
            keep_nodes.append(n)
    del g.node[:]
    g.node.extend(keep_nodes)
    inits = {i.name for i in g.initializer}

    # 拓扑序纯子树判定：全输入 ∈ (初始化器 ∪ 纯派生输出) 才可折叠。
    # Shape(image) 视为纯（本机固定 H×W 下图像 shape 是常量），否则宽高比链断掉。
    pure_outputs = {"num_tokens"}
    pure_nodes: list[onnx.NodeProto] = []
    for n in g.node:  # onnx 图保证拓扑序
        if n.op_type in keep_ops:
            continue
        non_init = [i for i in n.input if i and i not in inits]
        if "image" in n.input and n.op_type != "Shape":
            continue
        if non_init and all(i in pure_outputs or i == "image" for i in non_init):
            pure_nodes.append(n)
            pure_outputs.update(o for o in n.output)
    crossings = sorted({(n.name, i) for n in g.node for i in n.input
                        if i in pure_outputs and n not in pure_nodes})
    print(f"纯子树节点 {len(pure_nodes)}，纯派生张量 {len(pure_outputs)}，边界 {len(crossings)}")
    assert "num_tokens" in pure_outputs

    # 子图求值捕获：纯节点 + 所需初始化器，用 ReferenceEvaluator（numpy 逐算子，
    # 不校验声明类型）跑一拍——infer_shapes 对部分中间张量补不出 value_info。
    # **只烘边界张量**（被非纯节点消费的纯派生输出）与图输出：子树内部的中间
    # 张量烘了也没人消费，全烘会让图白背十几 MB 死权重。
    bake = ({i for _, i in crossings} | {o.name for o in g.output}) - {"num_tokens"}
    capture = sorted(t for t in pure_outputs if t != "num_tokens" and t in bake)
    sub_inits = [i for i in g.initializer
                 if any(i.name in n.input for n in pure_nodes)]
    sub = helper.make_graph(
        [n for n in pure_nodes], "pure_subtree",
        [helper.make_tensor_value_info("num_tokens", TensorProto.INT64, []),
         helper.make_tensor_value_info("image", TensorProto.FLOAT,
                                       [1, 3, args.h, args.w])],
        [helper.make_tensor_value_info(t, TensorProto.FLOAT, None) for t in capture],
        sub_inits)
    m2 = helper.make_model(sub, opset_imports=m.opset_import)
    m2.ir_version = m.ir_version
    from onnx.reference import ReferenceEvaluator
    ref = ReferenceEvaluator(m2)
    outs = ref.run(capture, {"image": np.zeros((1, 3, args.h, args.w), np.float32),
                             "num_tokens": np.array(args.tokens, np.int64)})
    consts = {t: np.asarray(v) for t, v in zip(capture, outs)}

    # 重建：删纯子树节点，派生张量→初始化器；num_tokens 输入随之无人消费
    drop = {n.name for n in pure_nodes}
    new_nodes = [n for n in g.node if n.name not in drop]
    for t, v in consts.items():
        g.initializer.append(numpy_helper.from_array(v, t))
    del g.node[:]
    g.node.extend(new_nodes)
    g.input.remove(next(i for i in g.input if i.name == "num_tokens"))
    vi_keep = [v for v in g.value_info if v.name not in consts]
    del g.value_info[:]
    g.value_info.extend(vi_keep)
    onnx.checker.check_model(m, full_check=False)

    out = WEIGHTS.with_name(
        f"moge2_vits_static_{args.h}x{args.w}_t{args.tokens}{args.tag}.onnx")
    onnx.save(m, str(out))
    print(f"{out}  {out.stat().st_size/1e6:.1f}MB")


if __name__ == "__main__":
    main()
