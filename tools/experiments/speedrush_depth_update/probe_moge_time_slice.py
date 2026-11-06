#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G3+G1 离线探针：MoGe-2 ViT-S 能否沿时间切片（保持 DML 融合/数值等价）。

自包含：只依赖 onnx / onnxruntime / cv2 / numpy，**不 import maaracing_master 业务码**
（试验区硬约束）。需要复刻的极小能力（preprocess、帧读取）在本脚本内自建。

跑法：.venv/Scripts/python.exe probe_moge_time_slice.py（仓库根）
"""
from __future__ import annotations
import os, sys, time, math, statistics, tempfile
import numpy as np
import cv2
import onnx
from onnx import shape_inference, utils as onnx_utils
import onnxruntime as ort

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MODEL = os.path.join(REPO, "maaracing_master/plugins/speedrush/resources/models/depth/moge2_vits_static_336x598_t1032_q4f16.onnx")
# 可回放的 debug 会话（raw 帧）；不存在则自动挑一个有 raw/ 的会话
DEBUG_SESS = os.path.join(os.environ["APPDATA"], "MaaRacingMaster", "debug",
                          "speedrush", "20261103_153632_p1", "raw")
TMP = os.environ.get("TMP") or tempfile.gettempdir()
N_FRAMES = 40          # 回放帧数
SPLIT_FRACTION = 0.5   # 沿节点拓扑在 ~50% 处切

PROVIDERS = ["DmlExecutionProvider", "CPUExecutionProvider"]


def pctl(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return xs[f] if f == c else xs[f] * (c - k) + xs[c] * (k - f)


def find_frames():
    if os.path.isdir(DEBUG_SESS):
        fs = sorted(os.listdir(DEBUG_SESS))
        if fs:
            return [os.path.join(DEBUG_SESS, x) for x in fs]
    # 兜底：扫 debug/speedrush 找第一个有 raw 的会话
    root = os.path.dirname(os.path.dirname(DEBUG_SESS))
    for d in sorted(os.listdir(root), reverse=True):
        raw = os.path.join(root, d, "raw")
        if os.path.isdir(raw) and os.listdir(raw):
            print(f"[帧源回退] 使用会话 {d}")
            return [os.path.join(raw, x) for x in sorted(os.listdir(raw))]
    raise SystemExit("找不到可回放的 raw 帧")


def preprocess(img_bgr, W, H):
    """复刻 moge_post.preprocess：BGR→RGB→resize→CHW float[0,1]→batch。"""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    im = cv2.resize(rgb, (W, H), interpolation=cv2.INTER_AREA)
    x = im.astype(np.float32) / 255.0
    return x.transpose(2, 0, 1)[None]


def run_all(sess, feeds):
    return sess.run(None, feeds)


def main():
    if not os.path.isfile(MODEL):
        raise SystemExit(f"模型不存在：{MODEL}")
    os.makedirs(TMP, exist_ok=True)
    print("=" * 72)
    print("A. 结构与单次前向（=锁持代理，G1）")
    print("=" * 72)
    m = onnx.load(MODEL)
    g = m.graph
    print("graph inputs:")
    for inp in g.input:
        dims = [d.dim_value if d.HasField("dim_value") else d.dim_param for d in
                inp.type.tensor_type.shape.dim] if inp.type.tensor_type.HasField("shape") else "?"
        print(f"  {inp.name} {inp.type.tensor_type.elem_type} {dims}")
    print("graph outputs:", [o.name for o in g.output])
    n = len(g.node)
    print(f"node 数={n}")
    ops = {}
    for nd in g.node:
        ops[nd.op_type] = ops.get(nd.op_type, 0) + 1
    top = sorted(ops.items(), key=lambda kv: -kv[1])[:12]
    print("op_type 直方（top）:", ", ".join(f"{k}×{v}" for k, v in top))
    attn = ops.get("Attention", 0) + ops.get("MultiHeadAttention", 0)
    ln = ops.get("LayerNormalization", 0)
    mm = ops.get("MatMul", 0)
    print(f"注意力算子={attn} LayerNorm={ln} MatMul={mm}  "
          f"→ 粗略 transformer block 规模由 (LN/Attention 数) 估计")

    # 载入 ORT 会话（DML）
    so = ort.SessionOptions()
    sess = ort.InferenceSession(MODEL, so, providers=PROVIDERS)
    print("实际 provider:", sess.get_providers())
    inames = [i.name for i in sess.get_inputs()]
    img_meta = sess.get_inputs()[0]
    shp = img_meta.shape  # e.g. [1,3,518,518] or dynamic
    try:
        H, W = int(shp[2]), int(shp[3])
    except Exception:
        H, W = 518, 518
    print(f"图像输入名={img_meta.name} 取用 H,W={H},{W}")

    frames = find_frames()
    step = max(1, len(frames) // N_FRAMES)
    sel = frames[::step][:N_FRAMES]
    print(f"回放 {len(sel)} 帧 / 共 {len(frames)}")

    def feeds(bgr):
        f = {inames[0]: preprocess(bgr, W, H)}
        for nm in inames[1:]:  # 可能的 num_tokens 等标量输入
            for i in sess.get_inputs():
                if i.name == nm:
                    dtype = i.type
                    if "int64" in dtype:
                        f[nm] = np.array(1032, np.int64)
                    elif "float" in dtype:
                        f[nm] = np.array(1.0, np.float32)
        return f

    # 预热（排除冷启动/显存分配）
    first = cv2.imread(sel[0])
    for _ in range(3):
        run_all(sess, feeds(first))

    fwd = []
    for fr in sel:
        bgr = cv2.imread(fr)
        if bgr is None:
            continue
        t0 = time.perf_counter()
        _ = run_all(sess, feeds(bgr))
        fwd.append((time.perf_counter() - t0) * 1000.0)
    print(f"单次前向 ms: p50={pctl(fwd,.5):.1f} p95={pctl(fwd,.95):.1f} "
          f"max={max(fwd):.1f} n={len(fwd)}")

    print()
    print("=" * 72)
    print("B. 沿 block 时间切片：劈成两个子模型跑（G3 可行性/代价/等价）")
    print("=" * 72)
    # 选切点：拓扑中段节点的输出张量（要能被 extract_model 当中间接口）
    mi = m
    try:
        mi = shape_inference.infer_shapes(m)
    except Exception as e:
        print("[shape_inference 失败]", repr(e))
    node_out = [o for nd in mi.graph.node for o in nd.output if o]
    # 切点选在中间、且是"张量级"输出（尽量取有 shape 的）
    cut = None
    for cand in node_out[len(node_out)//2::]:
        # 跳过纯标量
        vi = [v for v in mi.graph.value_info if v.name == cand]
        cut = cand
        break
    print(f"尝试切点张量: {cut}")
    input_names = inames
    output_names = [o.name for o in g.output]
    s1 = os.path.join(TMP, "moge_slice_sub1.onnx")
    s2 = os.path.join(TMP, "moge_slice_sub2.onnx")
    split_ok = False
    try:
        onnx_utils.extract_model(MODEL, s1, input_names, [cut])
        onnx_utils.extract_model(MODEL, s2, [cut], output_names)
        print("extract_model 成功：子图已生成")
        split_ok = True
    except Exception as e:
        print(f"[extract_model 失败 → 记录原因，时间切片路线受阻] {type(e).__name__}: {e}")

    if split_ok:
        try:
            sub1 = ort.InferenceSession(s1, so, providers=PROVIDERS)
            sub2 = ort.InferenceSession(s2, so, providers=PROVIDERS)
            s1_in = [i.name for i in sub1.get_inputs()]
            s2_in = [i.name for i in sub2.get_inputs()]
            bgr = cv2.imread(sel[len(sel)//2])
            base_feeds = feeds(bgr)
            # 全图基线
            t0 = time.perf_counter()
            full_out = sess.run(None, base_feeds)
            full_ms = (time.perf_counter() - t0) * 1000.0
            # 切片：sub1 → sub2
            f1 = {k: base_feeds[k] for k in s1_in if k in base_feeds}
            t1 = time.perf_counter()
            inter = sub1.run(None, f1)
            ms1 = (time.perf_counter() - t1) * 1000.0
            f2 = {s2_in[0]: inter[0]} if cut in s2_in or len(s2_in) == 1 else \
                 {k: v for k, v in zip(s2_in, inter)}
            t2 = time.perf_counter()
            sub_out = sub2.run(None, f2)
            ms2 = (time.perf_counter() - t2) * 1000.0
            slice_ms = ms1 + ms2
            print(f"全图一次 run: {full_ms:.1f}ms")
            print(f"切片两段:   sub1={ms1:.1f}ms sub2={ms2:.1f}ms 合计={slice_ms:.1f}ms "
                  f"(代价 +{slice_ms-full_ms:.1f}ms / +{100*(slice_ms-full_ms)/max(full_ms,1e-6):.0f}%)")
            # 数值等价（逐输出比）
            for i, (a, b) in enumerate(zip(full_out, sub_out)):
                a = np.asarray(a).astype(np.float32); b = np.asarray(b).astype(np.float32)
                if a.shape != b.shape:
                    print(f"  out[{i}] 形状不同 {a.shape} vs {b.shape}"); continue
                fin = np.isfinite(a) & np.isfinite(b)
                mad = float(np.max(np.abs(a[fin] - b[fin]))) if fin.any() else float("nan")
                print(f"  out[{i}] max|Δ|={mad:.3e}  shape={a.shape}")
        except Exception as e:
            print(f"[子图运行失败，DML/自定义算子可能不支持 extract 后的子图] {type(e).__name__}: {e}")
    else:
        print("→ 时间切片路线：extract_model 未通过，按停止条件2，先记录为受阻，不强行推进。")

    for p in (s1, s2):
        try: os.remove(p)
        except OSError: pass


if __name__ == "__main__":
    main()
