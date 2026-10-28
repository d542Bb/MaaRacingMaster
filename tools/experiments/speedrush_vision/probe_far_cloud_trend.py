# -*- coding: utf-8 -*-
"""远区点云趋势探针（C 类，只读实帧，不碰真机/生产）。

回答一个问题：产线现役 MoGe（q4f16）的点云在远区（z=15~50m）还能不能读出
路面左右趋势（路宽、路心走向）——「远区不是没办法看出趋势」这个假设的
直接验证。方法：实帧 → MoGe 点云 → 路面平面摆正 → 按 z 切片，量每片的路宽
（x 分位）与路心中位偏移；趋势在，路宽应保持 ~13m 量级、路心走向连续。

只读 debug 产物，不改生产。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg          # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds   # noqa: E402
from tools.experiments.speedrush_vision import moge2_post as mp         # noqa: E402

import os

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
FRAMES = [
    ("P1 直道 t=15s", DBG / "20261020_202758_p1" / "raw" / "0040_raw.jpg"),
    ("P1 变道 t=22s", DBG / "20261020_202758_p1" / "raw" / "0060_raw.jpg"),
    ("P2 变道 t=26s", DBG / "20261020_202843_p2" / "raw" / "0070_raw.jpg"),
]
Z_BINS = [(4, 8), (8, 14), (14, 20), (20, 30), (30, 40), (40, 50)]


def main() -> None:
    wpath = (Path(__file__).resolve().parents[3]
             / "maaracing_master/plugins/speedrush/resources/onnx/depth"
             / "moge2_vits_static_336x598_t1032_q4f16.onnx")
    sess = cds._moge_session(0, wpath=wpath)
    for label, path in FRAMES:
        frame = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB).astype(np.uint8)
        blob = mp.preprocess(frame, dg.MOGE_IN_W, dg.MOGE_IN_H)
        points, mask, mscale = mp.forward(sess, blob, 1032)
        res = mp.reconstruct(points, mask, mscale)
        pts = res["pts"].astype(np.float32)
        valid = res["valid"]
        if pts.shape[:2] != (720, 1280):
            pts = np.stack([cv2.resize(pts[..., k], (1280, 720),
                                       interpolation=cv2.INTER_LINEAR)
                            for k in range(3)], -1)
            valid = cv2.resize(valid.astype(np.float32), (1280, 720),
                               interpolation=cv2.INTER_NEAREST).astype(bool)
        pts[~valid] = np.nan
        # 相机轴系：pts[...,0]=X(右), pts[...,1]=Y(下), pts[...,2]=Z(前)
        X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
        sel = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 3) & (Z < 20)
        coef = cds.eph.fit_road_plane(np.where(sel, X, np.nan),
                                      np.where(sel, Y, np.nan),
                                      np.where(sel, Z, np.nan), None)
        if coef is None:
            print(f"\n== {label}: 平面拟合失败")
            continue
        ht = Y - (coef[0] * X + coef[1] * Z + coef[2])
        # 路面点：贴合摆正平面（不设 z 上限——拟合在 3~20m，切片看全量）
        road = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (np.abs(ht) < 0.3)
        print(f"\n== {label}  平面 a={coef[0]:+.3f} b={coef[1]:+.3f} c={coef[2]:+.2f}")
        print(f"   {'z带(m)':>10} {'点数':>7} {'路宽(m)':>8} {'路心x(m)':>9} {'左缘':>7} {'右缘':>7}")
        for z0, z1 in Z_BINS:
            m = road & (Z >= z0) & (Z < z1)
            n = int(m.sum())
            if n < 50:
                print(f"   {z0:>4}-{z1:<4} {n:>7}    点太少")
                continue
            xs = np.sort(X[m])
            p2, p98 = xs[int(len(xs) * .02)], xs[int(len(xs) * .98)]
            med = float(np.median(xs))
            print(f"   {z0:>4}-{z1:<4} {n:>7} {p98 - p2:>8.1f} {med:>+9.1f} {p2:>+7.1f} {p98:>+7.1f}")


if __name__ == "__main__":
    main()
