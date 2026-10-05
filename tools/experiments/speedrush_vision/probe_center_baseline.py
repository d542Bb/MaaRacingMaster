# -*- coding: utf-8 -*-
"""路心三方基线探针：找边路心 vs 栅格范围路心 vs 人工目检（离线，raw 帧）。

**问题**（2026-10-05 实机排障，session 20261005_222424_p1 trace）：找边
「最近一致边界优先」在多车道宽路+虚线场景把邻车道虚线当路缘，路心读数被
夹回自车（真值约右 1.5~2 道的场景 p50≈0）。候选替源=可行驶栅格绿区横向
范围（3~16m 全带、不依赖"找最近的线"），但绿区在日光直射沥青上自身缺证
（unknown 收缩）。本探针对同一批 raw 帧并算两路路心，出拼图供人工判读。

**标尺纪律**（C 类硬约束 2）：判读真值来自人工对 raw 帧场景的目检
（黄线/护栏/车道线的像面位置），与两路被测算法无关；帧间抖动为辅助量。

**口径**：两路共用产线 MoGe 点图与同一平面 coef（`reading_from_points` 与
`drivable_grid_from_points` 同源）；栅格路心=近场带 z 4~9m 逐行绿区
[min,max] 中点的车道量中位（范围中点不用质心——挖洞楔偏在中央）；找边
路心=生产同式 (L+R)/2（双侧在场才有；单帧不带 YOLO object_mask，与实机
差此一项，报告中注明）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_center_baseline.py \
        --session 20261005_222424_p1 --session 20261005_215624_p1 [--every 8] [--max 30]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DEBUG_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush/center_baseline"
SCALE = 8           # BEV 每格像素
NEAR_IZ = (4, 24)   # 近场带 z 4~9m → iz 行区间 [4,24)
FAR_IZ = (24, 52)   # 远场带 z 9~16m


def _grid_center(st: np.ndarray, iz_lo: int, iz_hi: int, lwm: float):
    """逐行绿区 [min,max] 中点 → 车道量；返回 (中位, 行数, 逐行记录)。"""
    rows = []
    for iz in range(iz_lo, iz_hi):
        xs = np.nonzero(st[iz] == dg.GRID_DRIVABLE)[0]
        if xs.size < dg.GRID_MIN_PTS:
            continue
        lo = -dg.GRID_X_MAX + xs.min() * dg.GRID_CELL
        hi = -dg.GRID_X_MAX + (xs.max() + 1) * dg.GRID_CELL
        rows.append({"z": dg.GRID_Z_LO + (iz + 0.5) * dg.GRID_CELL,
                     "mid_lane": (lo + hi) / 2.0 / lwm,
                     "w_lane": (hi - lo) / lwm, "px": (lo, hi)})
    if not rows:
        return None, 0, rows
    return float(np.median([r["mid_lane"] for r in rows])), len(rows), rows


def _grid_fit(rows: list):
    """逐行中点 z 线性拟合 → 截距（z→0 外推=自车位置路心，消化航向角）。"""
    if len(rows) < 4:
        return None
    z = np.array([r["z"] for r in rows])
    m = np.array([r["mid_lane"] for r in rows])
    A = np.stack([np.ones_like(z), z], -1)
    coef, *_ = np.linalg.lstsq(A, m, rcond=None)
    return float(coef[0])


def _grid_wmid(rows: list):
    """行中点按绿区宽度加权的加权中位（宽行=全幅更可能完整可见）。"""
    if not rows:
        return None
    m = np.array([r["mid_lane"] for r in rows])
    w = np.array([r["w_lane"] for r in rows])
    order = np.argsort(m)
    cw = np.cumsum(w[order])
    return float(m[order][np.searchsorted(cw, cw[-1] / 2.0)])


def _render(st: np.ndarray, dig: np.ndarray | None, rows: list, lwm: float,
            edges, path: Path) -> None:
    """BEV：三态+挖洞+绿区逐行中点（品红点）+L/R（青）+找边心（白）+栅格心（品红）。"""
    img = np.full((dg.GRID_NZ, dg.GRID_NX, 3), 64, np.uint8)
    img[st == dg.GRID_DRIVABLE] = (70, 140, 70)
    img[st == dg.GRID_BLOCKED] = (50, 50, 220)
    if dig is not None:
        img[dig] = (30, 100, 110)
    img = img[::-1]                      # 近下远上（与 hud BEV 同约定）
    img = cv2.resize(img, (dg.GRID_NX * SCALE, dg.GRID_NZ * SCALE),
                     interpolation=cv2.INTER_NEAREST)
    H, W = img.shape[:2]
    cv = np.full((H + 30, W + 150, 3), 30, np.uint8)
    cv[26:26 + H, 130:130 + W] = img

    def _px(x_m: float) -> int:
        return int(130 + (x_m + dg.GRID_X_MAX) / dg.GRID_CELL * SCALE)

    def _py(z_m: float) -> int:
        return int(26 + H - (z_m - dg.GRID_Z_LO) / dg.GRID_CELL * SCALE)

    cv2.drawMarker(cv, (_px(0.0), 26 + H - 6), (0, 255, 0),
                   cv2.MARKER_TRIANGLE_UP, 12, 2)
    for z in (3, 6, 9, 12, 15):
        cv2.line(cv, (126, _py(z)), (130 + W, _py(z)), (110, 110, 110), 1)
        cv2.putText(cv, f"{z}", (96, _py(z) + 4), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, (180, 180, 180), 1)
    for r in rows:                        # 绿区逐行中点
        cv2.circle(cv, (_px(r["mid_lane"] * lwm), _py(r["z"])), 2,
                   (255, 0, 255), -1)
    l_lane, r_lane, c_edge, c_grid = edges
    if l_lane is not None:
        cv2.line(cv, (_px(l_lane * lwm), 26), (_px(l_lane * lwm), 26 + H),
                 (255, 255, 0), 1)
    if r_lane is not None:
        cv2.line(cv, (_px(r_lane * lwm), 26), (_px(r_lane * lwm), 26 + H),
                 (255, 255, 0), 1)
    if c_edge is not None:
        cv2.line(cv, (_px(c_edge * lwm), 26), (_px(c_edge * lwm), 26 + H),
                 (255, 255, 255), 1)
    if c_grid is not None:
        cv2.line(cv, (_px(c_grid * lwm), 26), (_px(c_grid * lwm), 26 + H),
                 (255, 0, 255), 1)
    cv2.putText(cv, "C edge=white  grid=magenta  L/R=cyan", (6, 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1)
    cv2.imwrite(str(path), cv)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", required=True)
    ap.add_argument("--every", type=int, default=8)
    ap.add_argument("--max", type=int, default=30)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    frames = []
    for s in args.session:
        raws = sorted((DEBUG_ROOT / s / "raw").glob("*_raw.jpg"))
        frames += raws[::args.every]
    frames = frames[:args.max]
    print(f"取样 {len(frames)} 帧（every={args.every}）")

    sess = dg.load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    lwm = cal.lane_w_m
    ego = dg.DepthRoadObserver._load_ego_mask()
    results = []
    for i, f in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(f)), cv2.COLOR_BGR2RGB)
        pts, fx, fy = dg.infer_points(sess, rgb)
        reading = dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
        grid = dg.drivable_grid_from_points(pts, fx, fy, ego, None,
                                            coef=reading.coef)
        l, r = reading.left_edge_lane, reading.right_edge_lane
        c_edge = (l + r) / 2.0 if (l is not None and r is not None) else None
        c_near, n_near, rows_near = _grid_center(grid.state, *NEAR_IZ, lwm)
        c_far, n_far, rows_far = _grid_center(grid.state, *FAR_IZ, lwm)
        all_rows = rows_near + rows_far
        rec = {
            "frame": f.parent.parent.name + "/" + f.name,
            "L": None if l is None else round(l, 3),
            "R": None if r is None else round(r, 3),
            "c_edge": None if c_edge is None else round(c_edge, 3),
            "c_grid_near": None if c_near is None else round(c_near, 3),
            "c_grid_far": None if c_far is None else round(c_far, 3),
            "c_grid_fit": (lambda v: None if v is None else round(v, 3))(
                _grid_fit(all_rows)),
            "c_grid_wmid": (lambda v: None if v is None else round(v, 3))(
                _grid_wmid(all_rows)),
            "near_rows": n_near, "far_rows": n_far,
            "mids": [[round(r["z"], 2), round(r["mid_lane"], 3),
                      round(r["w_lane"], 2)] for r in all_rows],
            "rejects": list(reading.rejects)}
        results.append(rec)
        print(f"[{i + 1}/{len(frames)}] {f.name} L={rec['L']} R={rec['R']} "
              f"c_edge={rec['c_edge']} c_near={rec['c_grid_near']}({n_near}行) "
              f"c_far={rec['c_grid_far']}({n_far}行) fit={rec['c_grid_fit']} "
              f"wmid={rec['c_grid_wmid']}")

        # 拼图：上 raw（缩 640），下 BEV
        raw_s = cv2.resize(cv2.imread(str(f)), (640, 360))
        bev = np.full((360, 640, 3), 30, np.uint8)
        _render(grid.state, grid.dig_mask, rows_near, lwm,
                (l, r, c_edge, c_near),
                OUT / "_tmp_bev.png")
        tmp = cv2.imread(str(OUT / "_tmp_bev.png"))
        th = cv2.resize(tmp, (int(640 * tmp.shape[0] / tmp.shape[1] / 1.0), 360)
                        if tmp.shape[1] * 360 // tmp.shape[0] > 640
                        else (tmp.shape[1] * 360 // tmp.shape[0], 360))
        th = cv2.resize(tmp, (min(640, tmp.shape[1]), 360))
        bev[:th.shape[0], :th.shape[1]] = th
        canvas = np.vstack([raw_s, bev])
        cv2.putText(canvas, f"{f.name}  L={rec['L']} R={rec['R']} "
                    f"c_edge={rec['c_edge']} c_near={rec['c_grid_near']}",
                    (6, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 255, 255), 1)
        cv2.imwrite(str(OUT / f"{i:02d}_{f.stem.replace('_raw', '')}.png"),
                    canvas)
    (OUT / "_tmp_bev.png").unlink(missing_ok=True)
    (OUT / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"拼图与 results.json → {OUT}")


if __name__ == "__main__":
    main()
