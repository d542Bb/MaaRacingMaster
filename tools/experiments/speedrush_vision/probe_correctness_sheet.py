# -*- coding: utf-8 -*-
"""正确性人工抽检图（临时）：离线重放 debug 包的找边读数，把每个检出缘
(side, zc, x_m) 反投回游戏帧画圈标米数——维护者人眼验收"程序看的护栏/路缘
位置对不对"。左=标注帧，右=当帧产线 debug 渲染（BEV+热图）。用完即清或转正。"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, r"D:\maaracing_assistant")

from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush.world_model import load_calib

ROOT = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush")
OUT = ROOT / "depth_review"

CASES = [  # (run, seq) —— 覆盖巡航/弯道/近墙/远墙/低 fx 高 fx 各形态
    ("depth_debug_20261010_212306", 6), ("depth_debug_20261010_212306", 19),
    ("depth_debug_20261010_212306", 31), ("depth_debug_20261010_212306", 49),
    ("depth_debug_20261010_212349", 7), ("depth_debug_20261010_212349", 19),
    ("depth_debug_20261010_212349", 31), ("depth_debug_20261010_212349", 49),
    ("depth_debug_20261010_212349", 67),
]

for run, seq in CASES:
    stem = ROOT / "control_traces" / run / f"d{seq:05d}"
    z = np.load(str(stem) + "_evid.npz")
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pn = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pn[..., k], (1280, 720),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (1280, 720),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~vf] = np.nan
    obj = None
    mp_ = Path(str(stem) + "_mask.npy")
    if mp_.exists():
        obj = np.unpackbits(np.load(mp_))[: 1280 * 720] \
            .reshape(720, 1280).astype(bool)
    fx, fy = float(z["fx"]) * 1280, float(z["fy"]) * 720
    rd = dg.reading_from_points(pts, fx, load_calib(), object_mask=obj, fy=fy)
    frame = cv2.imread(str(stem) + "_frame.jpg")
    coef = rd.coef
    for s, zc, x in rd.edge_pts:
        u = int(round(x / zc * fx + 640))
        if coef is not None:
            yg = coef[0] * x + coef[1] * zc + coef[2]
            v = int(round(yg * fy / zc + 360))
        else:
            v = int(round(1.9 * fy / zc + 360))
        color = (0, 200, 255) if s > 0 else (255, 120, 0)   # 右=黄 左=蓝(BGR)
        cv2.circle(frame, (u, v), 14, color, 3)
        cv2.putText(frame, f"{x:+.1f}m@{zc:.0f}", (u - 40, max(20, v - 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2)
    lt = "-" if rd.left_edge_lane is None else f"{rd.left_edge_lane:+.2f}"
    rt = "-" if rd.right_edge_lane is None else f"{rd.right_edge_lane:+.2f}"
    cv2.putText(frame, f"d{seq:05d}  L={lt} R={rt} sides={rd.sides} "
                f"rej={';'.join(rd.rejects)[:40]}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 2)
    dbg = cv2.imread(str(stem) + ".jpg")
    left = cv2.resize(frame, (1280, int(frame.shape[0] * 1280 / frame.shape[1])))
    right = cv2.resize(dbg, (1280, int(dbg.shape[0] * 1280 / dbg.shape[1]))) \
        if dbg is not None else np.full((10, 1280, 3), 30, np.uint8)
    H = max(left.shape[0], right.shape[0])
    sheet = np.full((H, left.shape[1] + right.shape[1], 3), 30, np.uint8)
    sheet[:left.shape[0], :left.shape[1]] = left
    sheet[:right.shape[0], left.shape[1]:] = right
    out = OUT / f"correctness_{run[-6:]}_d{seq:05d}.jpg"
    cv2.imwrite(str(out), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(out.name, f"L={lt} R={rt} sides={rd.sides} coef={'Y' if coef is not None else 'N'}")
