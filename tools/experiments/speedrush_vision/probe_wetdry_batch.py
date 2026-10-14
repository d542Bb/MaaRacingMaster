# -*- coding: utf-8 -*-
"""湿/干判别批量重放（2026-10-11）：对多张原始帧跑产线同链
（load_session → infer_points → reading_from_points 自拟合+质量门），
逐帧输出拟合诊断（平面系数/滚转/俯仰/相机高/门内点率/残差 p50/判定与
拒绝原因）并各生成 pc3d_debug HTML——判别变量「湿/干 vs 弯/直」用。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_wetdry_batch.py \
        "C:/.../raw/0114_raw.jpg" "C:/.../raw/0107_raw.jpg" ...
"""
from __future__ import annotations

import base64
import math
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds  # noqa: E402

OUT = cds.OUT.parent / "pc3d_debug"
MODEL = Path(ROOT) / "maaracing_master" / "plugins" / "speedrush" / "resources" / "onnx" / "depth" / "moge2_vits_static_336x598_t1032_q4f16.onnx"


def diag(coef, pts) -> str:
    """coef + 全幅点图 → 质量门判据复算（与 _plane_plausible 同口径，
    dig=自车 3D 框×刀——重放无 YLO，物体候选区缺席）。"""
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    hgt = Y - (a * X + b * Z + c)
    dig = ((np.abs(X) < dg.EGO_ABS_X) & (Z < dg.EGO_ABS_Z)) & (hgt > dg.OBJ_KNIFE_HGT)
    road = (np.isfinite(hgt) & (np.abs(X) < dg.ROAD_X_MAX)
            & (Z >= dg.ROAD_Z_LO) & (Z < dg.PLANE_QUALITY_Z_HI) & (~dig))
    inl = road & (np.abs(hgt) < dg.PLANE_TOL)
    rate = float(inl.sum()) / max(int(road.sum()), 1)
    resid = float(np.nanmedian(np.abs(hgt[road]))) * 100 if road.sum() else float("nan")
    roll = math.degrees(math.atan(a))
    pitch = math.degrees(math.atan(b))
    ok = dg._plane_plausible(coef, X, Y, Z, dig)
    return (f"roll={roll:+.2f}° pitch={pitch:+.2f}° cam_h={abs(c):.2f}m "
            f"内点率={rate:.2f} 残差p50={resid:.1f}cm 质量门={'过' if ok else '拒'}")


def main() -> None:
    cal = load_calib()
    sess = dg.load_session(MODEL)
    for arg in sys.argv[1:]:
        raw = Path(arg)
        name = raw.parent.parent.name + "_" + raw.stem.split("_")[0]
        frame = cv2.cvtColor(cv2.imread(str(raw)), cv2.COLOR_BGR2RGB)
        pts, fx, fy, evid = dg.infer_points(sess, frame, with_evidence=True)
        reading = dg.reading_from_points(pts, fx, cal, None, fy=fy)
        rej = ";".join(reading.rejects) or "-"
        print(f"\n=== {name}")
        print(f"fx={fx:.1f}px fy={fy:.1f}px ms={evid['ms']:.2f} "
              f"有效占比={evid['valid'].mean():.2f} solver={evid.get('solver')}")
        print(f"产线判定: sides={reading.sides} rejects=[{rej}] "
              f"L={reading.left_edge_lane} R={reading.right_edge_lane}")
        if reading.coef is not None:
            print(f"拟合: {diag(reading.coef, pts)}")
        else:
            print("拟合: coef=None（拟合失败或被门拒）")

        b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
        s = cds._worldize(
            name, pts[..., 0], pts[..., 1], pts[..., 2], frame,
            np.zeros(frame.shape[:2], bool), None, cds.STEP,
            note="产线换装摆正 · 原始帧在线推理（湿/干判别批）",
            fit_fn=lambda X, Y, Z, excl: dg._fit_road_plane(
                X, Y, Z, fy=fy))
        js = ('{name:"' + s["name"] + '",hud:`' + s["hud"] + '`,'
              'xyz:f32(`' + b64(s["xyz"]) + '`),rgb:u8(`' + b64(s["rgb"]) + '`),'
              'ht:f32(`' + b64(s["ht"]) + '`),bd:u8(`' + b64(s["bd"]) + '`)}')
        html = (cds.HTML.replace("__STEM__", name).replace("__SRC__", "[" + js + "]")
                .replace("__TW__", "480"))
        OUT.mkdir(parents=True, exist_ok=True)
        out = OUT / f"{name}_debug.html"
        out.write_text(html, encoding="utf-8")
        print(f"{out}  {len(html)/1e6:.2f} MB")


if __name__ == "__main__":
    main()
