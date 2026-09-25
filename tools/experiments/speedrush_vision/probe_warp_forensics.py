# -*- coding: utf-8 -*-
"""warp 反直觉结果的帧级法证：同一帧多分辨率视差图 + L 边界 r 剖面对比。

回答维护者的问题：warp 路带采样密度更高，为什么 L 读数反而塌？看三样东西——
1) 四变体全图（共享 log 色标）：base@336 / warp@336(反映射) / @448 / @518；
2) L 边界局部放大（金标 y0 邻域）：台阶在哪个变体里变糊/被晕影吞掉；
3) 过边界行的 r=(d−g)/g 剖面（g=该行非自车列 q20，同产门口径）：台阶幅度、
   晕影隆起与门线（0.05/0.08）的相对关系逐变体可见。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_warp_forensics.py \
        [--frame 000906] [--side L] [--extra 001160]
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
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

from probe_warp_resample import _row_maps, _folded  # noqa: E402
from probe_mech_score import gold_x_at, gold_lane  # noqa: E402

H, W = 720, 1280
PANEL_W = 880
CROP_HW, CROP_HH = 220, 120   # L 边界局部放大半宽/半高（原始像素）
VCOL = {"base@336": (255, 0, 0), "warp@336": (0, 255, 0),
        "full@448": (0, 255, 255), "full@518": (255, 0, 255)}


def row_r(m: np.ndarray, y: int) -> np.ndarray:
    """过 y 行的 r 剖面（g=该行非自车列 q20，口径同产码 EGO_COLS）。"""
    row = m[y]
    g = np.quantile(np.concatenate([row[:dg.EGO_COLS[0]], row[dg.EGO_COLS[1]:]]), 0.20)
    return (row - g) / max(g, 1e-6)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default="000906")
    ap.add_argument("--side", default="L")
    ap.add_argument("--extra", default="001160")
    args = ap.parse_args()

    map_x, (my_warp, my_unwarp) = _row_maps()
    sess336 = dg.load_session(DEPTH_MODEL_FILE)
    import onnxruntime as ort
    s448 = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=_folded(448),
                                providers=["DmlExecutionProvider"])
    s518 = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=_folded(518),
                                providers=["DmlExecutionProvider"])

    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}

    def maps_for(stem: str):
        r = labels[stem]
        rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
        m336 = np.load(pd.OUT / "npy" / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy").astype(np.float32)
        warped = cv2.remap(rgb, map_x, my_warp, cv2.INTER_LINEAR)
        mw = dg.infer_map(sess336, warped, dg.DEFAULT_SHORT)
        m448 = dg.infer_map(s448, rgb, 448)
        m518 = dg.infer_map(s518, rgb, 518)
        return r, rgb, {"base@336": m336,
                        "warp@336": cv2.remap(mw, map_x, my_unwarp, cv2.INTER_LINEAR),
                        "full@448": m448, "full@518": m518}

    def render(stem: str) -> Path:
        r, rgb, maps = maps_for(stem)
        side = args.side.lower()
        glane, y0 = gold_lane(r, side)
        gx = float(gold_x_at(r, side, y0))
        # 共享 log 色标（四图路带）
        logs = np.log(np.clip(np.concatenate(
            [m[340:715].ravel() for m in maps.values()]), 1e-3, None))
        lo, hi = np.percentile(logs, [2, 98])

        def color(m, w=PANEL_W):
            c = cv2.applyColorMap(
                (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6),
                         0, 1) * 255).astype(np.uint8), cv2.COLORMAP_JET)
            return cv2.resize(c, (w, int(720 * w / 1280)))

        row1 = np.hstack([color(m) for m in maps.values()])
        for i, t in enumerate(maps):
            cv2.putText(row1, t, (8 + i * PANEL_W, 24), cv2.FONT_HERSHEY_SIMPLEX,
                        .6, (255, 255, 255), 2)

        # 局部放大：金标线位置叠加（绿=L）
        x0, x1 = int(max(0, gx - CROP_HW)), int(min(W, gx + CROP_HW))
        yy0, yy1 = int(max(0, y0 - CROP_HH)), int(min(H, y0 + CROP_HH))
        zooms = []
        for m in maps.values():
            z = cv2.resize(m[yy0:yy1, x0:x1], ((x1 - x0) * 2, (yy1 - yy0) * 2),
                           interpolation=cv2.INTER_NEAREST)
            z = cv2.applyColorMap(
                (np.clip((np.log(np.clip(z, 1e-3, None)) - lo) / max(hi - lo, 1e-6),
                         0, 1) * 255).astype(np.uint8), cv2.COLORMAP_JET)
            gy = (y0 - yy0) * 2
            gxc = (gx - x0) * 2
            cv2.line(z, (int(gxc), 0), (int(gxc), z.shape[0]), (0, 255, 0), 1)
            cv2.line(z, (0, int(gy)), (z.shape[1], int(gy)), (255, 255, 255), 1)
            zooms.append(z)
        row2 = np.hstack(zooms)
        cv2.putText(row2, f"zoom @y0={y0} (green=gold, white=eval row)",
                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)

        # r 剖面对比（y0 行，x∈[gx±CROP_HW]）
        PW, PH = 3520, 420
        panel = np.zeros((PH, PW, 3), np.uint8)
        RMAX = 0.45
        for t, m in maps.items():
            prof = row_r(m, y0)[x0:x1]
            pts = []
            for i, rv in enumerate(prof):
                px = int(i / max(len(prof) - 1, 1) * (PW - 80)) + 60
                py = int(PH * 0.85 - min(max(float(rv), -0.05), RMAX) / RMAX * PH * 0.75)
                pts.append((px, py))
            cv2.polylines(panel, [np.array(pts)], False, VCOL[t], 2)
        for gate, gcol in ((0.08, (0, 0, 255)), (0.05, (0, 128, 255))):
            py = int(PH * 0.85 - gate / RMAX * PH * 0.75)
            cv2.line(panel, (60, py), (PW - 20, py), gcol, 1)
            cv2.putText(panel, f"gate {gate}", (PW - 150, py - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, .5, gcol, 1)
        py = int(PH * 0.85)
        cv2.line(panel, (60, py), (PW - 20, py), (128, 128, 128), 1)
        for i, (t, c) in enumerate(VCOL.items()):
            cv2.putText(panel, t, (60 + i * 220, 26), cv2.FONT_HERSHEY_SIMPLEX,
                        .6, c, 2)
        cv2.putText(panel, f"r profile @row {y0}  (x {x0}..{x1}, gold at center)",
                    (60 + 4 * 220, 26), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        panel = cv2.resize(panel, (3520, PH))

        g = np.vstack([row1, row2, panel])
        out = pd.OUT / f"forensics_{stem}_{args.side}.jpg"
        cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return out

    def resolve(name: str) -> str | None:
        if name in labels:
            return name
        hits = [s for s in labels if s.endswith(name)]
        return hits[0] if len(hits) == 1 else None

    for raw in (args.frame, args.extra):
        stem = resolve(raw)
        if stem is None:
            print(f"{raw}: 不在金标集，跳过")
        else:
            print(f"已出：{render(stem)}")


if __name__ == "__main__":
    main()
