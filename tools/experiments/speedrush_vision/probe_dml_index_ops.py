# -*- coding: utf-8 -*-
"""第 0 步探针：DirectML 对静态索引 Gather / ScatterND 的支持（RESOLUTION_MECHANISMS §3.3/§六）。

要回答的问题（决定家族 1/2/3/5 是「需工程」还是「直接关闭」）：
- DML EP 能否吃下 **静态 Int32 索引** 的 Gather / ScatterND（固定合并表所需的
  全部索引算子形态；索引为编译期常量 initializer，形状全静态）；
- 对照组：Int64 索引同图——ORT #27118（2026-01，ORT 1.23.0）报告 Int64 索引在
  DML 崩 E_INVALIDARG 80070057；本机 ORT 1.24.4 是否已修复，由本探针实证。

判据：DML 输出与 numpy 参照（及 CPU EP）逐位一致 ⇒ 支持；会话创建或 Run 抛异常
⇒ 不支持（记录异常类型）。注意本探针只回答「算子能不能过」，不回答「插入合并
后折叠是否仍是单节点」（那是下一步）。

零新依赖：ONNX 模型文件用 protobuf 手工编码（wire format 内联在本文件），
不安装 onnx 包。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_dml_index_ops.py
"""
from __future__ import annotations

import numpy as np
import onnxruntime as ort

# ── protobuf wire format 最小编码 ─────────────────────────────────────
def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)

def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)

def _fvarint(field: int, v: int) -> bytes:
    return _tag(field, 0) + _varint(v)

def _fbytes(field: int, b: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(b)) + b

_DT = {np.dtype("float32"): 1, np.dtype("int32"): 6, np.dtype("int64"): 7}

def _tensor(name: str, arr: np.ndarray) -> bytes:
    """TensorProto：dims=1(packed) data_type=2 name=8 raw_data=9。"""
    b = _fbytes(1, b"".join(_varint(d) for d in arr.shape))
    b += _fvarint(2, _DT[arr.dtype])
    b += _fbytes(8, name.encode())
    b += _fbytes(9, arr.tobytes())
    return b

def _attr_int(name: str, val: int) -> bytes:
    """AttributeProto：name=1 type=20(INT=2) i=3。"""
    return _fbytes(1, name.encode()) + _fvarint(20, 2) + _fvarint(3, val)

def _node(op: str, ins: list[str], outs: list[str], attrs: tuple[bytes, ...] = ()) -> bytes:
    """NodeProto：input=1 output=2 op_type=4 attribute=5。"""
    b = b"".join(_fbytes(1, i.encode()) for i in ins)
    b += b"".join(_fbytes(2, o.encode()) for o in outs)
    b += _fbytes(4, op.encode())
    b += b"".join(_fbytes(5, a) for a in attrs)
    return b

def _value_info(name: str, elem: int, shape: tuple[int, ...]) -> bytes:
    """ValueInfoProto：name=1 type=2 → TypeProto{tensor_type=1{elem_type=1,
    shape=2{dim=1{dim_value=1}}}}。"""
    shape_b = b"".join(_fbytes(1, _fvarint(1, d)) for d in shape)
    tensor_b = _fvarint(1, elem) + _fbytes(2, shape_b)
    type_b = _fbytes(1, tensor_b)
    return _fbytes(1, name.encode()) + _fbytes(2, type_b)

def _graph(nodes: list[bytes], inits: list[bytes], inputs: list[bytes],
           outputs: list[bytes], name: str) -> bytes:
    """GraphProto：node=1 name=2 initializer=5 input=11 output=12。"""
    b = b"".join(_fbytes(1, n) for n in nodes)
    b += _fbytes(2, name.encode())
    b += b"".join(_fbytes(5, t) for t in inits)
    b += b"".join(_fbytes(11, v) for v in inputs)
    b += b"".join(_fbytes(12, v) for v in outputs)
    return b

def _model(graph: bytes, ir_version: int = 9, opset: int = 18) -> bytes:
    """ModelProto：ir_version=1 graph=7 opset_import=8。"""
    opset_b = _fvarint(2, opset)  # domain 省略 = 默认域 ""
    return _fvarint(1, ir_version) + _fbytes(7, graph) + _fbytes(8, opset_b)

# ── 探针图：x[8,4] → Gather(axis=0, 静态索引 idx[2]) → y1[2,4]
#                    → ScatterND(静态索引 sidx[2,1], upd[2,4]) → y2[8,4] ──
def build_model(index_dtype: np.dtype) -> bytes:
    x = _value_info("x", 1, (8, 4))
    y1 = _value_info("y1", 1, (2, 4))
    y2 = _value_info("y2", 1, (8, 4))
    gidx = _tensor("gidx", np.array([0, 3], index_dtype))
    sidx = _tensor("sidx", np.array([[0], [5]], index_dtype))
    supd = _tensor("supd", np.array([[1, 2, 3, 4], [-1, -2, -3, -4]], np.float32))
    nodes = [
        _node("Gather", ["x", "gidx"], ["y1"], (_attr_int("axis", 0),)),
        _node("ScatterND", ["x", "sidx", "supd"], ["y2"]),
    ]
    return _model(_graph(nodes, [gidx, sidx, supd], [x], [y1, y2], "dml_index_probe"))

def _refs(x: np.ndarray, idx_dtype: np.dtype):
    gidx = np.array([0, 3], idx_dtype)
    sidx = np.array([[0], [5]], idx_dtype)
    upd = np.array([[1, 2, 3, 4], [-1, -2, -3, -4]], np.float32)
    y1 = x[gidx]
    y2 = x.copy()
    y2[sidx[:, 0]] = upd
    return y1, y2

def run_case(idx_dtype: np.dtype, providers: list[str]) -> str:
    x = np.linspace(-4, 4, 32, dtype=np.float32).reshape(8, 4)
    y1_ref, y2_ref = _refs(x, idx_dtype)
    try:
        sess = ort.InferenceSession(build_model(idx_dtype), providers=providers)
        y1, y2 = sess.run(None, {"x": x})
        ok = np.array_equal(y1, y1_ref) and np.array_equal(y2, y2_ref)
        cpu = ort.InferenceSession(build_model(idx_dtype), providers=["CPUExecutionProvider"])
        c1, c2 = cpu.run(None, {"x": x})
        ok_cpu = np.array_equal(y1, c1) and np.array_equal(y2, c2)
        return f"通过（numpy 一致={ok}，CPU 一致={ok_cpu}）"
    except Exception as e:  # noqa: BLE001 —— 探针目的就是捕获失败形态
        msg = str(e).replace("\n", " ")[:160]
        return f"失败：{type(e).__name__}: {msg}"

def main() -> None:
    print(f"ORT {ort.__version__}，providers={ort.get_available_providers()}")
    for dt in (np.int32, np.int64):
        for prov, label in ((["DmlExecutionProvider"], "DML"), (["CPUExecutionProvider"], "CPU")):
            print(f"[{label}] 静态 {dt.__name__} 索引 Gather+ScatterND：{run_case(dt, prov)}")

if __name__ == "__main__":
    main()
