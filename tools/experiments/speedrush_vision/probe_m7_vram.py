"""M7 补测：深度候选显存占用（每模型独立子进程，2026-09-23）。

**为什么重测**：A1 Pareto 矩阵的 M7 在同进程依次跑三个模型，DML arena 跨模型累积
（yolo26n 显示 4.6GB > DA-B，明显污染）——矩阵里记为"未隔离测（开放）"。

**方法**：每个权重一个全新 python 子进程（DML 会话唯一、arena 独立生长）：
  载入前采样 5 次 `nvidia-smi --query-gpu=memory.used` 取中位 → 建会话 → 热身 3 次
  → 跑 10 次到稳态 → 再采 5 次取中位 → 差分 = 该模型显存占用估计。
  另跑一个**空载子进程**（不建会话，纯采样）给出桌面/后台漂移噪声底，读数须显著
  高于噪声底才算数（本机 nvidia-smi 的 compute-apps 全部 [N/A]，无法按进程直读，
  差分是唯一可行口径——绝对值含桌面波动，各模型间相对序与"谁撑爆 8GB"有效）。

用法（仓库根，.venv Python）：
    P=tools/experiments/speedrush_vision/probe_m7_vram.py
    python $P sweep     # 父进程：依次 spawn 5 权重 + 空载对照
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent

# 每权重：(权重文件名, 输入名, (高, 宽))；尺寸对齐 A1 矩阵口径（yolo 固定 768 方形）
MODELS = {
    "da2_small": ("da2_small.onnx", "pixel_values", (518, 924)),
    "da2_base": ("da2_base.onnx", "pixel_values", (518, 924)),
    "yolo26n": ("yolo26n-depth.onnx", "images", (768, 768)),
    "yolo26s": ("yolo26s-depth.onnx", "images", (768, 768)),
    "yolo26m": ("yolo26m-depth.onnx", "images", (768, 768)),
    "segformer": ("segformer-b0-ade.onnx", "pixel_values", (512, 512)),
}


def gpu_used_mb(n: int = 5, gap: float = 0.3) -> float:
    import re
    vals = []
    for _ in range(n):
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True).stdout
        m = re.search(r"\d+", out)
        if m:
            vals.append(int(m.group()))
        time.sleep(gap)
    vals.sort()
    return float(vals[len(vals) // 2]) if vals else float("nan")


def run_one(name: str) -> None:
    """子进程体：测单模型差分并打一行 JSON。name=null 为空载对照。"""
    before = gpu_used_mb()
    if name != "null":
        import numpy as np
        import onnxruntime as ort
        from probe_a2_correctability import WEIGHTS
        fname, in_name, (h, w) = MODELS[name]
        sess = ort.InferenceSession(str(WEIGHTS / fname), providers=["DmlExecutionProvider"])
        blob = np.random.rand(1, 3, h, w).astype(np.float32)
        for _ in range(3):
            sess.run(None, {in_name: blob})
        for _ in range(10):
            sess.run(None, {in_name: blob})
        time.sleep(0.5)
    after = gpu_used_mb()
    total = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True).stdout.strip()
    print(json.dumps({"model": name, "before_mb": before, "after_mb": after,
                      "delta_mb": after - before, "total_str": total}), flush=True)


def cmd_sweep() -> None:
    names = ["null"] + list(MODELS)
    rows = []
    for name in names:
        r = subprocess.run([sys.executable, str(APP / "probe_m7_vram.py"), "run", "--model", name],
                           capture_output=True, text=True)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("{")), None)
        if not line:
            print(f"{name}: 失败 exit={r.returncode} {r.stderr[-200:]}", flush=True)
            continue
        d = json.loads(line)
        rows.append(d)
        print(f"{d['model']:10s} before={d['before_mb']:6.0f} after={d['after_mb']:6.0f} "
              f"Δ={d['delta_mb']:6.0f} MB (total {d['total_str']} MB)", flush=True)
    null = next((d["delta_mb"] for d in rows if d["model"] == "null"), 0.0)
    print(f"[噪声底] 空载漂移 Δ={null:+.0f} MB；各模型 Δ 减去噪声底为净占用估计")
    for d in rows:
        if d["model"] != "null":
            print(f"    {d['model']:10s} 净占用 ≈ {d['delta_mb'] - null:6.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sweep")
    sr = sub.add_parser("run")
    sr.add_argument("--model", choices=list(MODELS) + ["null"], required=True)
    args = ap.parse_args()
    if args.cmd == "sweep":
        cmd_sweep()
    else:
        run_one(args.model)


if __name__ == "__main__":
    main()
