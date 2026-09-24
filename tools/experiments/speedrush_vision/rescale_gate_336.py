"""@336 落档实验：门口径重标 × fp16/q4f16 同卷配对（2026-09-24）。

**为什么有这个文件**：第一关终选判定 @336 现行门口径对 region 不可用——根因
是 g 随分辨率压缩 ~20%、相对门 (M−g)/g 放大 ~2.6×（G=2% 等效 0.8%），块从
路面里起跳、边界成松门交点。本实验回答"仍开放"的问题：**按档重标门后边界
误差能否回到可接受范围**——回到，则 @336 可承担 boundary（时延 p95 24.7ms
双✓）；仍不稳，才轮到怀疑感知/空间采样本身。

**设计**：
  - 变体：fp16（da2_small_fp16.onnx）与 q4f16（oc_model_q4f16.onnx）同卷配对
    ——量化是否可用不继承 @518 旧裁决，在落档档位上用区域口径重测；
  - 门网格：G ∈ {0.02 现行, 0.03, 0.04, 0.045, 0.05, 0.055, 0.06}
    （0.02×2.6≈0.052 为补偿预测点附近展开）；绝对归一 (M−g)/rng 为后备，
    本轮不实现；
  - 判据：金标 54 帧、y=625 单读数口径 dev（与生产复验同口径），对照
    @518 参照成绩（curve_cont2 −24/27px n13 坏 0%）；
  - 守卫（conv/resid/lane/近带/本底）全部按生产实现、门随参数联动。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/rescale_gate_336.py infer   # 先推理缓存
    python tools/experiments/speedrush_vision/rescale_gate_336.py scan    # 门网格扫描
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import probe_depth as pd  # noqa: E402
import probe_step_detect as psd  # noqa: E402
from firstgate_score import gold_rows  # noqa: E402
from gold_score import gold_x_at  # noqa: E402
from tri_exam import DEMOS  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DepthRoadObserver, reading_from_map)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

OUT = psd.OUT
WEIGHTS = pd.WEIGHTS
VARIANTS = {"fp16": WEIGHTS / "da2_small_fp16.onnx",
            "q4f16": WEIGHTS / "oc_model_q4f16.onnx"}
SHORT = 336
PROD_Y = 625
GRID = (0.02, 0.03, 0.04, 0.045, 0.05, 0.055, 0.06)


def frames() -> list[tuple[str, Path]]:
    out = [(f"frames__{Path(r['path']).stem}", Path(r["path"])) for r in gold_rows()]
    for wname, sname, lo, hi in (("straddle", "20260922_113932_p1", 500, 536),
                                 ("wallhug", "20260922_113610_p2", 422, 452)):
        for fid in range(lo, hi + 1):
            p = DEMOS / sname / "frames" / f"{fid:06d}.jpg"
            if p.exists():
                out.append((f"{wname}_{fid}", p))   # key 进文件名，禁用冒号（NTFS ADS）
    return out


def cmd_infer(_args) -> None:
    """两变体 @336 推理 122 帧考卷 → {key}__d336fp16.npy / {key}__d336q4.npy。"""
    npy = OUT / "npy"
    npy.mkdir(parents=True, exist_ok=True)
    sess = {v: ort.InferenceSession(str(w), providers=["DmlExecutionProvider"])
            for v, w in VARIANTS.items()}
    for key, p in frames():
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        for v, s in sess.items():
            cache = npy / f"{key}__d336{v}.npy"
            if cache.exists():
                continue
            m = pd.depth_map(s, rgb, SHORT).astype(np.float32)
            np.save(cache, m.astype(np.float16))
        print(f"\r{key}", end="", flush=True)
    print("\n[infer] done")


def cmd_scan(_args) -> None:
    cal = load_calib()
    ego = DepthRoadObserver._load_ego_mask()
    recs = []
    for key, p in frames():
        row = {"key": key}
        for v in VARIANTS:
            m = np.load(OUT / "npy" / f"{key}__d336{v}.npy").astype(np.float32)
            for g in GRID:
                rd = reading_from_map(m, cal, ego, gate=g)
                row[f"{v}_g{g}_sides"] = rd.sides
                for side in ("L", "R"):
                    row[f"{v}_g{g}_{side}x"] = (
                        rd.left_x if side == "L" else rd.right_x)
        recs.append(row)
    gold = {f"frames__{Path(r['path']).stem}": r for r in gold_rows()}
    print("== @336 门重标扫描（金标 y=625 dev，px；对照 fp32@518："
          "curve_cont2 −24/27 n13 kerb −9/16 n2 obstacle −14/21 n4）==")
    for v in VARIANTS:
        print(f"\n-- {v}@336 --")
        for g in GRID:
            by = defaultdict(list)
            for row in recs:
                if row["key"] not in gold:
                    continue
                r = gold[row["key"]]
                for side, pre in (("L", "l"), ("R", "r")):
                    x = row.get(f"{v}_g{g}_{side}x")
                    if x is None:
                        continue
                    ny, fy = float(r[pre + "_ny"]), float(r[pre + "_fy"])
                    gx = gold_x_at(r, pre, PROD_Y)
                    if gx is not None and min(ny, fy) <= PROD_Y <= max(ny, fy) \
                            and -20 <= gx <= 1299:
                        by[r["stratum"]].append(x - gx)
            cells = []
            tot = []
            for st in ("curve_cont2", "kerb", "obstacle", "wall", "wallhug"):
                vs = by.get(st, [])
                if vs:
                    a = np.array(vs)
                    tot += vs
                    cells.append(f"{st[:6]} {np.median(a):+4.0f}/"
                                 f"{np.percentile(np.abs(a), 90):3.0f}/n{len(a)}")
                else:
                    cells.append(f"{st[:6]} -")
            if tot:
                a = np.array(tot)
                cells.append(f"合计 中位{np.median(a):+4.0f}/"
                             f"p90|{np.percentile(np.abs(a), 90):3.0f}|/"
                             f"坏{(np.abs(a) > 120).mean():.0%}")
            print(f"  G={g:.3f} " + "  ".join(cells))


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"infer": cmd_infer, "scan": cmd_scan}[cmd](None)


if __name__ == "__main__":
    main()
