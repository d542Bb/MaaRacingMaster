# -*- coding: utf-8 -*-
"""钉焦距判别实验（2026-10-11）：病根在「每帧猜焦距」还是在「网络深度」？

仪器化拟合已证（probe_wet_table 后续）：拒帧的重建连贯但整体被放歪+压低
（种子即歪 roll −8°，路面点集体贴合歪平面，cam_h 1.5 vs 物理 1.9~2.0m）——
affine-invariant 点图 + 每帧现场猜参数的失效形态。本探针对同一批帧用
moge_post.reconstruct(focal=F) 钉死焦距只解 shift 重放：
  - 若拒帧回正（cam_h≈1.9、残差回 1~2cm）→ 网络深度无恙，病根=每帧焦距恢复，
    修法=冻结/约束焦距（零模型改动）；
  - 若仍歪 → 网络深度本身带形变，只剩模型线。

对照帧用 evid 自带 frame.jpg（d00002 干净 / d00066 干净大滚转 / d00075、
d00080 湿地贴右栏拒帧）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_pin_focal.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush import moge_post  # noqa: E402

MODEL = (Path(ROOT) / "maaracing_master" / "plugins" / "speedrush" / "resources"
         / "onnx" / "depth" / "moge2_vits_static_336x598_t1032_q4f16.onnx")
DD = os.path.join(str(Path.home()), "AppData", "Roaming", "MaaRacingMaster",
                  "data", "speedrush", "control_traces", "depth_debug_20261010_212349")

FRAMES = ["d00002", "d00066", "d00075", "d00080"]


def fit_stats(pts, fy):
    """产线域拟合 + 门判据复算，返回 (coef, roll, cam_h, rate, resid_cm)。"""
    X, Y, Z = pts[dg.Y0:dg.DIAG_Y1, :, 0], pts[dg.Y0:dg.DIAG_Y1, :, 1], pts[dg.Y0:dg.DIAG_Y1, :, 2]
    coef = dg._fit_road_plane(X, Y, Z, fy=fy)
    if coef is None:
        return None, float("nan"), float("nan"), float("nan"), float("nan")
    a, b, c = map(float, coef)
    hgt = Y - (a * X + b * Z + c)
    dig = ((np.abs(X) < dg.EGO_ABS_X) & (Z < dg.EGO_ABS_Z)) & (hgt > dg.OBJ_KNIFE_HGT)
    road = (np.isfinite(hgt) & (np.abs(X) < dg.ROAD_X_MAX)
            & (Z >= dg.ROAD_Z_LO) & (Z < dg.PLANE_QUALITY_Z_HI) & (~dig))
    inl = road & (np.abs(hgt) < dg.PLANE_TOL)
    rate = float(inl.sum()) / max(int(road.sum()), 1)
    resid = float(np.nanmedian(np.abs(hgt[road]))) * 100
    return coef, math.degrees(math.atan(a)), abs(c), rate, resid


def main() -> None:
    sess = dg.load_session(MODEL)
    for pkg in FRAMES:
        frame = cv2.cvtColor(cv2.imread(os.path.join(DD, f"{pkg}_frame.jpg")),
                             cv2.COLOR_BGR2RGB)
        blob = moge_post.preprocess(frame, dg.MOGE_IN_W, dg.MOGE_IN_H)
        points, mask, ms = moge_post.forward(sess, blob, 1032)
        H, W = points.shape[:2]

        def run(focal):
            res = moge_post.reconstruct(points, mask, ms, focal=focal)
            pts = np.stack([cv2.resize(np.ascontiguousarray(res["pts"][..., k]),
                                       (1280, 720), interpolation=cv2.INTER_LINEAR)
                            for k in range(3)], -1)
            valid = cv2.resize(res["valid"].astype(np.float32), (1280, 720),
                               interpolation=cv2.INTER_NEAREST).astype(bool)
            pts[~valid] = np.nan
            fy_full = res["fy"] * (720 / H)
            coef, roll, cam_h, rate, resid = fit_stats(pts, fy_full)
            sol = res["solver"]
            sol_s = (f"shift={sol.get('shift'):+.3f} foc={sol.get('focal'):.3f}"
                     if sol.get("focal") is not None else f"shift={sol.get('shift'):+.3f} foc=钉死")
            v = "None " if coef is None else (
                f"roll={roll:+6.2f} cam_h={cam_h:.2f} rate={rate:.2f} resid={resid:4.1f}cm")
            print(f"    focal={'自估' if focal is None else format(focal, '.3f')}: {v}   [{sol_s}]")

        print(f"== {pkg}")
        run(None)
        # 钉死值：干净帧 d00002 自估 focal 为基准 + 扫描
        for f in (0.742, 0.784, 0.830, 0.887):
            run(f)
        print()


if __name__ == "__main__":
    main()
