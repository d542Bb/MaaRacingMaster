# -*- coding: utf-8 -*-
"""warp 链路判定：是「链路错误」还是「标尺偏移」？（维护者质疑驱动）

假设分叉：
- H1 链路正确、纯标尺偏移：warp 的 r 剖面与 base 仅差仿射（a·r+b），形状吻合；
  ⇒ 可用仿射校正/形状读法（梯度拐点）救援，warp 信号本身可用。
- H2 warp OOD 退化：模型在竖直拉伸图上估计的剖面形状本身变形；
  ⇒ warp 链路关闭（模型层不可救）。

方法（每帧，y0=金标可见行中位，x∈金标±220）：
1) 映射表 roundtrip 误差（fwd∘inv 应为恒等——链路正确性的先决检查）；
2) 形状仿射拟合：warp≈a·base+b 的残差 RMSE，与 448≈a'·base+b'、518 同型对照
   ——448/518 是同分布族（自然图），其残差是「非 warp 变体间形状差异」的基准；
3) 平滑梯度 argmax（尺度不敏感的边位置估计）逐变体 vs 金标 x——若 warp 的
   梯度拐点与 base 接近，说明信号在、只是标尺移动。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402
import probe_crop_quality as pcq  # noqa: E402

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

from probe_warp_resample import _row_maps, _folded, _warp_fwd, _warp_inv  # noqa: E402
from probe_mech_score import gold_x_at, gold_lane  # noqa: E402
from probe_warp_forensics import row_r  # noqa: E402


def smooth(x: np.ndarray, k: int = 15) -> np.ndarray:
    ker = np.ones(k) / k
    return np.convolve(x, ker, mode="same")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="000906,001160,001220,000500")
    args = ap.parse_args()

    # 1) 映射表 roundtrip
    ys = np.arange(720, dtype=np.float32)
    rt = np.abs(_warp_fwd(_warp_inv(ys)) - ys).max()
    print(f"[先决检查] 映射表 roundtrip max|fwd(inv(y))−y| = {rt:.3f}px（应为 ~0）")

    map_x, (my_warp, my_unwarp) = _row_maps()
    sess336 = dg.load_session(DEPTH_MODEL_FILE)
    import onnxruntime as ort
    s448 = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=_folded(448),
                                providers=["DmlExecutionProvider"])
    s518 = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=_folded(518),
                                providers=["DmlExecutionProvider"])

    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}

    print(f"\n{'帧':<10}{'变体':<10}{'梯度拐点x':>9}{'|拐点−金标|':>10}{'仿射a':>7}{'仿射b':>8}{'形状RMSE':>9}")
    for raw in args.frames.split(","):
        hits = [s for s in labels if s.endswith(raw)]
        if not hits:
            print(f"{raw}: 不在金标集"); continue
        stem = hits[0]
        r = labels[stem]
        rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
        m336 = np.load(pd.OUT / "npy" / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy").astype(np.float32)
        warped = cv2.remap(rgb, map_x, my_warp, cv2.INTER_LINEAR)
        mw = dg.infer_map(sess336, warped, dg.DEFAULT_SHORT)
        maps = {"base@336": m336,
                "warp@336": cv2.remap(mw, map_x, my_unwarp, cv2.INTER_LINEAR),
                "full@448": dg.infer_map(s448, rgb, 448),
                "full@518": dg.infer_map(s518, rgb, 518)}
        side = "l"
        _glane, y0 = gold_lane(r, side)
        gx = float(gold_x_at(r, side, y0))
        x0, x1 = int(max(0, gx - 220)), int(min(1280, gx + 220))

        profs = {t: row_r(m, y0)[x0:x1] for t, m in maps.items()}
        base = profs["base@336"]

        def affine_res(prof):
            A = np.stack([base, np.ones_like(base)], 1)
            (a, b), *_ = np.linalg.lstsq(A, prof, rcond=None)
            return a, b, float(np.sqrt(np.mean((a * base + b - prof) ** 2)))

        for t, prof in profs.items():
            sm = smooth(prof)
            g = np.gradient(sm)
            ix = int(np.argmax(np.abs(g)))
            ex = x0 + ix
            if t == "base@336":
                print(f"{stem:<10}{t:<10}{ex:>9}{abs(ex - gx):>10.0f}{'—':>7}{'—':>8}{'—':>9}")
            else:
                a, b, rmse = affine_res(prof)
                print(f"{stem:<10}{t:<10}{ex:>9}{abs(ex - gx):>10.0f}{a:>7.2f}{b:>8.3f}{rmse:>9.3f}")
    print("\n判读：若 warp 形状RMSE 与 448/518 同量级 ⇒ H1（链路对、纯标尺偏移）；"
          "若显著更大 ⇒ H2（OOD 退化，链路关闭）。梯度拐点接近金标者=该变体边位置可信。")


if __name__ == "__main__":
    main()
