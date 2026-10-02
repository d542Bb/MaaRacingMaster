# -*- coding: utf-8 -*-
"""depth_debug 证据包离线复算渲染器：evid npz → 新渲染图（锚点画回真实像素）。

背景（2026-10-02 挂账转正）：旧 render_depth_debug 的解析回投把黄圈全体画到
带顶（归一化/原生像素两套焦距单位打架），恰好挡住「找边锚到底锁在哪个结构」
的肉眼复核。产线修成像素真值反查后，历史证据包（只有点云没有原图）用本探针
重放：渲染行②用修后代码重算，行①直接裁旧 jpg 的原图行拼接——锚点即现形。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/replay_debug_evid.py \
        --dir "%APPDATA%/MaaRacingMaster/data/speedrush/control_traces/depth_debug_20261002_211445" \
        --stems d00003 d00009 d00017 d00021 --out out_replay

读数复算与旧 jpg 左上角 L/R 文本逐位对照（复算口径自检）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.speedrush.depth_geo import (
    DIAG_Y1, Y0, reading_from_points, render_depth_debug)
from maaracing_master.plugins.speedrush.world_model import load_calib

FULL_W, FULL_H = 1280, 720


def replay(stem: Path, cal) -> np.ndarray:
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx = float(z["fx"]) * FULL_W      # 归一化焦距 → 全幅像素（复算口径）
    fy = float(z["fy"]) * FULL_H

    ego = obj = None
    ego_p = Path(str(stem) + "_ego.npy")
    if ego_p.exists():
        ego = np.unpackbits(np.load(ego_p))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        mask_p = Path(str(stem) + "_mask.npy")
        if mask_p.exists():
            merged = np.unpackbits(np.load(mask_p))[: FULL_W * FULL_H] \
                .reshape(FULL_H, FULL_W).astype(bool)
            obj = merged & ~ego

    rd = reading_from_points(pts, fx, cal, ego_mask=ego, object_mask=obj, fy=fy)
    print(f"{stem.name}: L={rd.left_edge_lane} R={rd.right_edge_lane} "
          f"sides={rd.sides} rejects={rd.rejects} anchors={len(rd.edge_pts)}")

    old = cv2.imread(str(stem) + ".jpg")             # 旧行①=原图行，直接复用
    frame_rgb = cv2.cvtColor(old[:FULL_H], cv2.COLOR_BGR2RGB)
    img = render_depth_debug(frame_rgb, {"pts": pts, "valid": valid_full,
                                         "fx": np.float64(fx / FULL_W),
                                         "fy": np.float64(fy / FULL_H)},
                             rd, ego, obj, None)
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--stems", nargs="+", required=True)
    ap.add_argument("--out", default="out_replay")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cal = load_calib()
    for s in a.stems:
        stem = Path(a.dir) / s
        cv2.imwrite(str(out / f"{s}_replay.jpg"), replay(stem, cal),
                    [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"输出 → {out.resolve()}")


if __name__ == "__main__":
    main()
