# -*- coding: utf-8 -*-
"""BEV 朝向取证探针：raw 帧离线复跑产线栅格，定案 z 轴朝向与路心读数。

**问题**（2026-10-05 实机调试图目检）：hud 右联 BEV 的视锥看着上下颠倒
（近场 3m 行横跨 ±9m，物理上不可能——z=3m 处视锥只容 |X|≤u_max·z≈2m）；
且橙色挖洞区呈上窄下宽楔形。两个候选：① 仅 debugview 渲染朝向画反
（py() 把 z=3 映顶部，与 depth_geo 取证面板 z3(bottom)..z16(top) 约定相反）；
② 栅格数据本身 z 翻转。本探针逐行中位 Z + 逐行 drivable 横向范围直证，
另出双朝向渲染图与 hud 图目检比对。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_bev_orientation.py
    ... --frame <raw.jpg 路径>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

OUT = Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush/bev_check"
SCALE = 10  # 每格 10px


def _render(st: np.ndarray, dig: np.ndarray | None, ro: float | None,
            lwm: float, flip: bool, path: Path) -> None:
    """栅格渲染（flip=False 行序同 state：iz0 在顶；flip=True 上下翻转）。"""
    img = np.full((dg.GRID_NZ, dg.GRID_NX, 3), 64, np.uint8)
    img[st == dg.GRID_DRIVABLE] = (70, 140, 70)
    img[st == dg.GRID_BLOCKED] = (50, 50, 220)
    if dig is not None:
        img[dig] = (30, 100, 110)
    if flip:
        img = img[::-1]
    img = cv2.resize(img, (dg.GRID_NX * SCALE, dg.GRID_NZ * SCALE),
                     interpolation=cv2.INTER_NEAREST)
    H, W = img.shape[:2]
    canvas = np.full((H + 40, W + 70, 3), 30, np.uint8)
    canvas[30:30 + H, 60:60 + W] = img
    z_near, z_far = (dg.GRID_Z_HI, dg.GRID_Z_LO) if flip else \
        (dg.GRID_Z_LO, dg.GRID_Z_HI)
    cv2.putText(canvas, f"top z={z_near:.0f}m  bottom z={z_far:.0f}m"
                f"  (flip={flip})", (8, 20), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (255, 255, 255), 1)
    for z in (3.0, 6.0, 9.0, 12.0, 15.0):
        iz = int((z - dg.GRID_Z_LO) / dg.GRID_CELL)
        y = 30 + (dg.GRID_NZ - 1 - iz if flip else iz) * SCALE + SCALE // 2
        cv2.putText(canvas, f"{z}m", (10, y + 4), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, (200, 200, 200), 1)
    if ro is not None:
        xc = int(60 + (-ro * lwm + dg.GRID_X_MAX) / dg.GRID_CELL * SCALE)
        cv2.line(canvas, (xc, 30), (xc, 30 + H), (255, 255, 255), 1)
        cv2.putText(canvas, "C", (xc + 2, 44), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, (255, 255, 255), 1)
    cv2.imwrite(str(path), canvas)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=Path, required=True)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    bgr = cv2.imread(str(args.frame))
    assert bgr is not None, args.frame
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    sess = dg.load_session(DEPTH_MODEL_FILE)
    pts, fx, fy = dg.infer_points(sess, rgb)
    print(f"pts {pts.shape} fx={fx:.1f} fy={fy:.1f}")

    # 直证 1：图像行 ↔ Z 的方向（近路面在图像下缘 → 底行 Z 应最小）
    Z = pts[..., 2]
    print("── 每行有效点中位 Z（米）──")
    for r in range(340, 720, 30):
        z = Z[r][np.isfinite(Z[r])]
        if z.size:
            print(f"row {r:3d}: Z_med={np.median(z):7.2f}  n={z.size}")

    ego = dg.DepthRoadObserver._load_ego_mask()
    cal = load_calib()
    reading = dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
    center = None
    if reading.left_edge_lane is not None and reading.right_edge_lane is not None:
        center = (reading.left_edge_lane + reading.right_edge_lane) / 2.0
    print(f"L={reading.left_edge_lane} R={reading.right_edge_lane} "
          f"center_lane={center} rejects={reading.rejects}")
    print(f"coef={reading.coef}")

    grid = dg.drivable_grid_from_points(pts, fx, fy, ego, None,
                                        coef=reading.coef)
    st, dig = grid.state, grid.dig_mask
    print(f"dig_cells={grid.dig_cells} drivable={int((st == 1).sum())} "
          f"blocked={int((st == 2).sum())}")

    # 直证 2：逐 iz 行 drivable 横向范围（近行应窄、远行应宽）
    print("── 逐行 drivable 横向范围 ──")
    for iz in range(0, dg.GRID_NZ, 4):
        xs = np.nonzero(st[iz] == dg.GRID_DRIVABLE)[0]
        z = dg.GRID_Z_LO + (iz + 0.5) * dg.GRID_CELL
        if xs.size:
            lo = -dg.GRID_X_MAX + xs.min() * dg.GRID_CELL
            hi = -dg.GRID_X_MAX + (xs.max() + 1) * dg.GRID_CELL
            print(f"z={z:5.2f}m: drivable x [{lo:+6.2f}, {hi:+6.2f}]  "
                  f"dig={int(dig[iz].sum())}")
        else:
            print(f"z={z:5.2f}m: (无 drivable)  dig={int(dig[iz].sum())}")

    stem = args.frame.stem.replace("_raw", "")
    ro = -center if center is not None else None
    _render(st, dig, ro, cal.lane_w_m, flip=False,
            path=OUT / f"{stem}_asis.png")     # 与现 hud 同朝向（iz0 在顶）
    _render(st, dig, ro, cal.lane_w_m, flip=True,
            path=OUT / f"{stem}_flipped.png")  # 常规 BEV（近下远上）
    print(f"渲染输出: {OUT}\\{stem}_asis.png / _flipped.png")


if __name__ == "__main__":
    main()
