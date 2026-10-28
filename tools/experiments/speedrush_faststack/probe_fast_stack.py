# -*- coding: utf-8 -*-
"""小模型快速栈可行性探针（C 类实验，只读回放帧）。

**要回答的问题**（维护者 2026-10-21 口径）：开车其实只需要四件事——路的范围、
车身方向、速度、加上物品感知给的标签。深度模型慢不代表不能用，抽稀（少 token）
换帧率之后，这四件事还准不准、够不够？本探针把「四件事」直接画到帧上人工确认，
并用现役满 token 档作基线量化掉多少质量、省多少时间。

**口径**（全部用生产实现，不自己造第二真源）：
- 目标：`StreetPerception`（YOLO，car/coin/bonus_car）；距离 = 同帧点图上框底中点 Z。
- 路的范围：`reading_from_points` 的近场找边（z≤16m，实测）+ `ground_far_extents`
  的中远场地面可见带（到 80m，推断级供数）；两者都按 YOLO 候选区挖除物体。
- 车身方向：近场边界求路心序列，对 z 拟合直线，路心切线与相机轴的夹角即车身
  相对路的方向（自车在路心的偏移一并给出）。
- 速度：静态目标（金币）本帧距离减匹配到的上一帧同目标距离，逐帧差分取中位——
  只用框底测距，不用图像匹配、不用密集点云。
- 时延：MoGe 分段（pre/forward/reconstruct）+ 平面 + 找边 + 栅格，逐帧记账。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_faststack/probe_fast_stack.py \
        --video video_20261021_160547_p1 --start 400 --limit 120 --tokens 1032 512 256
    python tools/experiments/speedrush_faststack/probe_fast_stack.py \
        --video video_20261021_160547_p2 --start 0 --limit 120 --overlay-step 10

输出（数据目录 fast_stack/<video>/）: metrics_<tokens>.jsonl、overlay_<tokens>.mp4、
样张 overlay_<tokens>_<fid>.jpg、汇总 summary.json。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT_ROOT = DATA / "fast_stack"

CAM_W, CAM_H = 1280, 720
ZREF = 12.0            # 路心/方向参照距离（米）
FIT_Z0, FIT_Z1 = 4.0, 18.0   # 路心拟合窗（米）
FIT_MIN_SPAN = 4.0       # 拟合所需最小纵向跨度（米）
FIT_TOL = 0.6            # 单侧线拟合内点容差（米）
MATCH_DX = 80.0        # 静态目标帧间像素匹配窗
TRACK_Z_MAX = 30.0     # 进速度估计的目标距离上限（米）
CAR_W = 1.9          # 车身宽（米，框宽测距用）
SPEED_WIN = 8          # 速度滑窗（帧）
SMOOTH_WIN = 5         # 路心/航向滑窗（帧）


def dz_gate(z1: float) -> float:
    """帧间距离跳变门（米）：容下 70m/s 的前向位移 + 该距离上的框底测距噪声。"""
    return 0.5 + 0.06 * z1 + 70.0 / 60.0



def frames_of(video: str, start: int, limit: int, fps: float):
    files = sorted((DEMOS / video).glob("*.jpg"))
    return [(p, int(round(i * 1e9 / fps))) for i, p in enumerate(files)][start:start + limit]


def object_mask(per, scale_w: int, scale_h: int) -> np.ndarray:
    """YOLO 框 → 点图像素系布尔掩码（与生产同口径：外扩一档，物体从点云源头消失）。"""
    m = np.zeros((CAM_H, CAM_W), bool)
    for det in list(per.cars) + list(per.coins) + list(per.bonuses):
        pad = max(4, int(0.06 * max(det.w, det.h)))
        x0 = max(0, det.cx - det.w // 2 - pad)
        x1 = min(CAM_W - 1, det.cx + det.w // 2 + pad)
        y0 = max(0, det.cy - det.h // 2 - pad)
        y1 = min(CAM_H - 1, det.cy + det.h // 2 + pad)
        m[y0:y1 + 1, x0:x1 + 1] = True
    if (scale_w, scale_h) != (CAM_W, CAM_H):
        m = cv2.resize(m.astype(np.uint8), (scale_w, scale_h),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    return m


def det_z(pts: np.ndarray, det) -> float | None:
    """框底中点在点图上的 Z（米）——目标远近只用这一个几何量。"""
    h, w = pts.shape[:2]
    u = int(round(det.cx * w / CAM_W))
    v = int(round(min(CAM_H - 2, det.cy + det.h // 2 - 2) * h / CAM_H))
    win = pts[max(0, v - 2):v + 3, max(0, u - 2):u + 3, 2]
    win = win[np.isfinite(win)]
    if win.size == 0:
        return None
    return float(np.median(win))


def road_geometry(reading, far, zref: float = ZREF):
    """找边读数 + 远带范围 → 路心序列 / 横偏 / 航向角。

    近场边界（实测）优先；某侧近场缺点时用远带地面可见范围补（标记为推断）。
    返回 (centers=[(z,x,src)], offset_at_zref, heading_deg, src_mix)。"""
    pts_l = [(z, x) for side, z, x in reading.edge_pts if side < 0]
    pts_r = [(z, x) for side, z, x in reading.edge_pts if side > 0]
    zs = sorted({round(z, 1) for z, _ in pts_l} | {round(z, 1) for z, _ in pts_r})
    centers = []
    for z in zs:
        xl = [x for zz, x in pts_l if abs(zz - z) < 0.6]
        xr = [x for zz, x in pts_r if abs(zz - z) < 0.6]
        if xl and xr:
            centers.append((z, (float(np.median(xl)) + float(np.median(xr))) / 2.0, "meas"))
    far_c = []
    for (z0, z1, lo, hi, _n) in far or ():
        if lo is None or hi is None:
            continue
        z = (z0 + z1) / 2.0
        if any(abs(z - c[0]) < 1.0 for c in centers):
            continue
        centers.append((z, (float(lo) + float(hi)) / 2.0, "soft"))
        far_c += [(z, float(lo)), (z, float(hi))]
    seg = "meas" if pts_l and pts_r else ("soft" if far_c else "n/a")
    fl = side_fit(pts_l)
    fr = side_fit(pts_r)
    if fl is None and far_c:
        fl = side_fit([(z, x) for z, x in far_c if x < 0])
        fr = side_fit([(z, x) for z, x in far_c if x > 0])
    if fl is None or fr is None:
        return centers, None, None, seg
    k = (fl[0] + fr[0]) / 2.0
    m = (fl[1] + fr[1]) / 2.0
    offset = k * zref + m
    heading = float(np.degrees(np.arctan(k)))
    return centers, offset, heading, seg


def side_fit(pts, zlo: float = FIT_Z0, zhi: float = FIT_Z1,
             tol: float = FIT_TOL, iters: int = 30):
    """单侧边界点 (z,x) 拟合直线 x=k·z+m（RANSAC 抗单框假缘）；点不足返回 None。"""
    arr = np.array([(z, x) for z, x in pts if zlo <= z <= zhi], float)
    if len(arr) < 2 or arr[:, 0].max() - arr[:, 0].min() < FIT_MIN_SPAN:
        return None
    best, best_n = None, -1
    rng = np.random.default_rng(7)
    for _ in range(iters):
        i, j = rng.choice(len(arr), 2, replace=False)
        z1, x1 = arr[i]
        z2, x2 = arr[j]
        if abs(z2 - z1) < 1.0:
            continue
        k = (x2 - x1) / (z2 - z1)
        m = x1 - k * z1
        n = int(np.sum(np.abs(arr[:, 1] - (k * arr[:, 0] + m)) < tol))
        if n > best_n:
            best, best_n = (k, m), n
    if best is None or best_n < 2:
        return None
    k, m = best
    keep = np.abs(arr[:, 1] - (k * arr[:, 0] + m)) < tol
    if keep.sum() >= 2:
        k2, m2 = np.polyfit(arr[keep, 0], arr[keep, 1], 1)
        return float(k2), float(m2)
    return float(k), float(m)


def run_chain(sess, frame_rgb, tokens, cal, mask_full):
    """一帧 → (点图原生, fx, fy, reading, grid, far, 分段耗时)。"""
    t0 = time.perf_counter()
    blob = dg.moge_post.preprocess(frame_rgb, dg.MOGE_IN_W, dg.MOGE_IN_H)
    t1 = time.perf_counter()
    points, mask, mscale = dg.moge_post.forward(sess, blob, tokens)
    t2 = time.perf_counter()
    res = dg.moge_post.reconstruct(points, mask, mscale)
    t3 = time.perf_counter()
    pts = np.array(res["pts"], copy=True)
    pts[~res["valid"]] = np.nan
    fx = res["fx"] * dg.MOGE_IN_W
    fy = res["fy"] * dg.MOGE_IN_H
    ds = dg.MOGE_IN_W / float(pts.shape[1])
    mask_g = cv2.resize(mask_full.astype(np.uint8), (pts.shape[1], pts.shape[0]),
                        interpolation=cv2.INTER_NEAREST).astype(bool)
    y0, y1 = dg._band_rows(pts.shape[0])
    t4 = time.perf_counter()
    coef = dg._fit_road_plane(pts[y0:y1, :, 0], pts[y0:y1, :, 1], pts[y0:y1, :, 2],
                              fy=fy / ds)
    t5 = time.perf_counter()
    if coef is None:
        return pts, fx, fy, None, None, (), coef, {
            "pre": (t1 - t0) * 1e3, "forward": (t2 - t1) * 1e3,
            "rec": (t3 - t2) * 1e3, "plane": (t5 - t4) * 1e3,
            "edges": 0.0, "grid": 0.0, "all": (t5 - t0) * 1e3}
    t6 = time.perf_counter()
    reading = dg.reading_from_points(pts, fx, cal, object_mask=mask_g, fy=fy,
                                     coef=coef, ds=ds)
    t7 = time.perf_counter()
    grid = dg.drivable_grid_from_points(pts, fx, fy, mask_g, coef=coef, ds=ds)
    far = dg.ground_far_extents(pts, coef=coef, mask=mask_g)
    t8 = time.perf_counter()
    return pts, fx, fy, reading, grid, far, coef, {
        "pre": (t1 - t0) * 1e3, "forward": (t2 - t1) * 1e3,
        "rec": (t3 - t2) * 1e3, "plane": (t5 - t4) * 1e3,
        "edges": (t7 - t6) * 1e3, "grid": (t8 - t7) * 1e3,
        "all": (t8 - t0) * 1e3}


def project(X, Z, Y, fx, fy, cam_w=CAM_W, cam_h=CAM_H):
    """地面点 → 像素（相机系，Y 向下为正）。"""
    u = cam_w / 2.0 + fx * X / Z
    v = cam_h / 2.0 + fy * Y / Z
    return u, v


def draw_overlay(frame_bgr, reading, far, grid, centers, offset, heading,
                 dets, fx, fy, coef, ms_all, ms_fwd, tokens, v=None, v_rel=None):
    """把四件事画回帧上 + 右侧 BEV 面板。"""
    out = frame_bgr.copy()
    a, b, c = coef if coef is not None else (0.0, 0.0, 0.0)
    for side, color in ((-1, (0, 255, 255)), (1, (0, 165, 255))):
        seq = sorted((z, x) for s, z, x in (reading.edge_pts if reading else ())
                     if s == side)
        for (z0, x0), (z1, x1) in zip(seq, seq[1:]):
            y0, y1 = a * x0 + b * z0 + c, a * x1 + b * z1 + c
            p0 = project(x0, z0, y0, fx, fy)
            p1 = project(x1, z1, y1, fx, fy)
            cv2.line(out, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])),
                     color, 3)
        if seq:
            z, x = seq[-1]
            pu, pv = project(x, z, a * x + b * z + c, fx, fy)
            cv2.circle(out, (int(pu), int(pv)), 7, color, -1)
    for (z0, z1, lo, hi, _n) in far or ():
        if lo is None:
            continue
        for x, color in ((lo, (255, 120, 0)), (hi, (0, 120, 255))):
            for z in (z0 + 0.3, z1 - 0.3):
                pu, pv = project(x, z, a * x + b * z + c, fx, fy)
                if 0 <= pu < CAM_W and 0 <= pv < CAM_H:
                    cv2.circle(out, (int(pu), int(pv)), 3, color, -1)
    for kind, d, z in dets:
        col = {"car": (0, 0, 255), "coin": (0, 220, 0), "bonus": (0, 255, 0)}[kind]
        x0 = int(d.cx - d.w / 2); y0 = int(d.cy - d.h / 2)
        cv2.rectangle(out, (x0, y0), (x0 + d.w, y0 + d.h), col, 2)
        if z is not None:
            cv2.putText(out, f"{kind} {z:.1f}m", (x0, max(14, y0 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    if offset is not None:
        u_c, v_c = project(0.0, ZREF, a * 0.0 + b * ZREF + c, fx, fy)
        u_g, v_g = project(offset, ZREF, a * offset + b * ZREF + c, fx, fy)
        cv2.line(out, (int(u_c), int(v_c)), (int(u_g), int(v_g)), (255, 0, 255), 2)
    for src, col in (("meas", (255, 0, 255)), ("soft", (160, 90, 255))):
        seq = sorted((z, x) for z, x, s2 in (centers or []) if s2 == src)
        for (z0, x0), (z1, x1) in zip(seq, seq[1:]):
            p0 = project(x0, z0, a * x0 + b * z0 + c, fx, fy)
            p1 = project(x1, z1, a * x1 + b * z1 + c, fx, fy)
            cv2.line(out, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])), col, 2)
    if heading is not None:   # 罗盘：车身相对路的方向（不叠在车身上，画在角上）
        c0 = (CAM_W - 70, 90)
        ang = np.radians(heading) - np.pi / 2
        cv2.circle(out, c0, 34, (255, 255, 255), 1)
        cv2.line(out, c0, (int(c0[0] + 30 * np.cos(ang)), int(c0[1] + 30 * np.sin(ang))),
                 (255, 0, 255), 2)
    hud = (f"tok{tokens} all{ms_all:.0f}ms fwd{ms_fwd:.0f}  "
           f"off{(offset if offset is not None else float('nan')):+.2f}m  "
           f"hdg{(heading if heading is not None else float('nan')):+.1f}deg  "
           f"v{(v if v is not None else float('nan')) * 3.6:.0f}km/h "
           f"dv{(v_rel if v_rel is not None else float('nan')) * 3.6:+.0f}")
    cv2.rectangle(out, (0, 0), (CAM_W, 30), (0, 0, 0), -1)
    cv2.putText(out, hud, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1,
                cv2.LINE_AA)
    bev = bev_panel(grid, centers, dets)
    return np.hstack([out, bev])


def bev_panel(grid, centers, dets):
    """与帧等高，便于横拼。"""
    """BEV：三态栅格 + 路心点 + 目标点（x 右、z 上）。"""
    W, H = 300, CAM_H
    img = np.full((H, W, 3), 40, np.uint8)
    if grid is not None and grid.state is not None:
        st = grid.state
        col = {0: (40, 40, 40), 1: (90, 200, 90), 2: (60, 60, 220)}
        for iz in range(st.shape[0]):
            for ix in range(st.shape[1]):
                x = -dg.GRID_X_MAX + (ix + 0.5) * dg.GRID_CELL
                z = dg.GRID_Z_LO + (iz + 0.5) * dg.GRID_CELL
                px = int((x + dg.GRID_X_MAX) / (2 * dg.GRID_X_MAX) * W)
                py = int(H - (z - dg.GRID_Z_LO) / (dg.GRID_Z_HI - dg.GRID_Z_LO) * H)
                cv2.rectangle(img, (px - 1, py - 1), (px + 1, py + 1),
                              col[int(st[iz, ix])], -1)
    for (z, x, src) in centers:
        px = int((x + dg.GRID_X_MAX) / (2 * dg.GRID_X_MAX) * W)
        py = int(H - (z - dg.GRID_Z_LO) / (dg.GRID_Z_HI - dg.GRID_Z_LO) * H)
        cv2.circle(img, (px, py), 2, (255, 0, 255) if src == "meas" else (0, 200, 255), -1)
    for kind, d, z in dets:
        if z is None or z > dg.GRID_Z_HI or z < dg.GRID_Z_LO:
            continue
        x = (d.cx - CAM_W / 2.0) * z / 559.2
        px = int((x + dg.GRID_X_MAX) / (2 * dg.GRID_X_MAX) * W)
        py = int(H - (z - dg.GRID_Z_LO) / (dg.GRID_Z_HI - dg.GRID_Z_LO) * H)
        cv2.circle(img, (px, py), 4, (0, 0, 255) if kind == "car" else (0, 220, 0), 1)
    cv2.putText(img, "BEV 3-30m", (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (255, 255, 255), 1, cv2.LINE_AA)
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="video_20261021_160547_p1")
    ap.add_argument("--start", type=int, default=400)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--tokens", type=int, nargs="+", default=[1032, 512, 256])
    ap.add_argument("--overlay-step", type=int, default=5)
    ap.add_argument("--tag", default="", help="输出文件名后缀，用于分段对照")
    args = ap.parse_args()

    out_dir = OUT_ROOT / args.video
    out_dir.mkdir(parents=True, exist_ok=True)
    cal = load_calib()
    frames = frames_of(args.video, args.start, args.limit, args.fps)
    per = StreetPerception(str(PERCEPTION_MODEL_FILE))
    summary = {"video": args.video, "start": args.start, "tag": args.tag, "n_frame": len(frames),
               "fps": args.fps, "tokens": {}}

    # 预跑一遍：帧 + YOLO 只过一次（DML 单锁，感知与深度不同帧抢），逐 token 档复用
    prep = []
    for i, (path, ts_ns) in enumerate(frames):
        frame_bgr = cv2.imread(str(path))
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        t_y0 = time.perf_counter()
        pres = per.detect(frame_rgb, frame_id=args.start + i, ts_ns=ts_ns)
        yolo_ms = (time.perf_counter() - t_y0) * 1e3
        prep.append((path, ts_ns, frame_bgr, frame_rgb, pres, yolo_ms,
                    object_mask(pres, CAM_W, CAM_H)))

    for tokens in args.tokens:
        sess = dg.load_session(DEPTH_MODEL_FILE)
        rows = []
        prev_coins: list[tuple[int, int, float]] = []
        prev_cars: list[tuple[int, int, float]] = []
        speeds: list[float] = []
        speed_win: deque = deque(maxlen=SPEED_WIN)
        off_win: deque = deque(maxlen=SMOOTH_WIN)
        hdg_win: deque = deque(maxlen=SMOOTH_WIN)
        vid = None
        metrics_path = out_dir / f"metrics_{tokens}{args.tag}.jsonl"
        metrics_f = metrics_path.open("w", encoding="utf-8")
        for i, (path, ts_ns, frame_bgr, frame_rgb, pres, yolo_ms, mask_full) in enumerate(prep):
            pts, fx, fy, reading, grid, far, coef, ms = run_chain(
                sess, frame_rgb, tokens, cal, mask_full)
            dets = []
            for kind, lst in (("car", pres.cars), ("coin", pres.coins),
                              ("bonus", pres.bonuses)):
                for d in lst:
                    z = det_z(pts, d) if pts is not None else None
                    dets.append((kind, d, z))
            centers, offset, heading, seg = (None, None, None, "n/a")
            if reading is not None:
                centers, offset, heading, seg = road_geometry(reading, far)
            off_win.append(offset); hdg_win.append(heading)
            ov = [x for x in off_win if x is not None]
            hv = [x for x in hdg_win if x is not None]
            offset_s = float(np.median(ov)) if len(ov) >= 3 else None
            heading_s = float(np.median(hv)) if len(hv) >= 3 else None
            # 速度：目标框底距离帧间差分（贪心配对：近邻 + 距离跳变门）。
            # 金币=静态，其接近速率即自车速；车=动态，其接近速率是跟车需要的相对速。
            def pairs(prev, cur):
                cand = []
                for ci, (cx0, cy0, z0, _w0) in enumerate(cur):
                    for pi, (cx1, cy1, z1, _w1) in enumerate(prev):
                        dd = ((cx0 - cx1) ** 2 + (cy0 - cy1) ** 2) ** 0.5
                        if dd < MATCH_DX and abs(z0 - z1) < dz_gate(z1):
                            cand.append((dd, ci, pi))
                cand.sort()
                used_c, used_p, out = set(), set(), []
                for _dd, ci, pi in cand:
                    if ci in used_c or pi in used_p:
                        continue
                    used_c.add(ci); used_p.add(pi)
                    out.append(prev[pi][2] - cur[ci][2])
                return out

            def contact(dets, kind):
                """本帧该类的 (cx, cy, 点图Z, 框宽)——Z 缺失的目标不进跟踪集。"""
                return sorted([(d.cx, d.cy, z, d.w) for k, d, z in dets
                               if k == kind and z and z <= TRACK_Z_MAX])

            dzs_coin = pairs(prev_coins, contact(dets, "coin"))
            dzs_car = pairs(prev_cars, contact(dets, "car"))
            # 相对接近速度另一路：只吃框宽（不碰点云）。z≈CAR_W·fx/w →
            # dz/dt = −CAR_W·fx·(dw/dt)/w²，w 的像素变化率天然连续，不受
            # 框底 Z 抖动与目标换人影响。
            dwv = []
            cur_cars = contact(dets, "car")
            for (cx0, cy0, _z0, w0), (cx1, cy1, _z1, w1) in zip(prev_cars, cur_cars):
                if abs(cx0 - cx1) < MATCH_DX and w0 > 8 and abs(w1 - w0) / w0 < 0.6:
                    dwv.append(-CAR_W * fx * (w1 - w0) * args.fps / (w0 * w0))
            prev_coins, prev_cars = contact(dets, "coin"), cur_cars
            for x in dzs_coin:
                speed_win.append(x)
            z_w = [CAR_W * fx / w for _cx, _cy, _z, w in contact(dets, "car") if w > 8]
            vals = list(speed_win)
            v = float(np.median(vals) * args.fps) if len(vals) >= 2 else None
            v_rel = float(np.median(dwv)) if dwv else None
            if v is not None:
                speeds.append(v)
            row = {"k": i, "fid": args.start + i, "ts_ms": ts_ns / 1e6,
                   "tokens": tokens, "yolo_ms": yolo_ms, **{f"ms_{a}": b for a, b in ms.items()},
                   "n_edge": len(reading.edge_pts) if reading else 0,
                   "sides": reading.sides if reading else 0,
                   "lane_l": reading.left_edge_lane if reading else None,
                   "lane_r": reading.right_edge_lane if reading else None,
                   "coef": None if coef is None else [float(t) for t in coef],
                   "n_far": sum(1 for b in far if b[2] is not None),
                   "far_max_z": max((b[1] for b in far if b[2] is not None), default=0),
                   "offset_raw": offset, "heading_raw": heading,
                   "offset": offset_s, "heading": heading_s, "seg_src": seg,
                   "n_center": len(centers) if centers else 0,
                   "speed": v, "speed_rel": v_rel, "speed_rel_w": v_rel,
                   "z_w": [round(x, 2) for x in z_w], "n_speed_pair": len(vals),
                   "n_det": len(dets),
                   "det_z": {k: [round(z, 2) for kk, _d, z in dets if kk == k and z]
                           for k in ("car", "coin", "bonus")}}
            rows.append(row)
            metrics_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if i % args.overlay_step == 0:
                vis = draw_overlay(frame_bgr, reading, far, grid, centers or [],
                                   offset, heading, dets, fx, fy, coef, ms["all"], ms["forward"], tokens, v, v_rel)
                cv2.imwrite(str(out_dir / f"overlay_{tokens}{args.tag}_{args.start + i:06d}.jpg"), vis)
                if vid is None:
                    vw, vh = int(vis.shape[1] / 2), int(vis.shape[0] / 2)
                    vid = cv2.VideoWriter(str(out_dir / f"overlay_{tokens}{args.tag}.mp4"),
                                          cv2.VideoWriter_fourcc(*"mp4v"), 12, (vw, vh))
                vid.write(cv2.resize(vis, (int(vis.shape[1] / 2), int(vis.shape[0] / 2))))
        metrics_f.close()
        if vid:
            vid.release()
        ms_all = np.array([r["ms_all"] for r in rows])
        summary["tokens"][tokens] = {
            "n": len(rows),
            "ms_all_p50": float(np.median(ms_all)), "ms_all_p90": float(np.percentile(ms_all, 90)),
            "ms_forward_p50": float(np.median([r["ms_forward"] for r in rows])),
            "yolo_ms_p50": float(np.median([r["yolo_ms"] for r in rows])),
            "sides2_frac": float(np.mean([r["sides"] == 2 for r in rows])),
            "far_bands_frac": float(np.mean([r["n_far"] >= 3 for r in rows])),
            "offset_have_frac": float(np.mean([r["offset"] is not None for r in rows])),
            "speed_hz_frac": float(np.mean([r["speed"] is not None for r in rows])),
            "speed_med_kmh": (float(np.median(speeds)) * 3.6) if speeds else None,
            "point_map": [int(pts.shape[0]), int(pts.shape[1])],
        }
        del sess
        print(f"[tok {tokens}] p50={summary['tokens'][tokens]['ms_all_p50']:.1f}ms "
              f"sides2={summary['tokens'][tokens]['sides2_frac']:.2f} "
              f"speed={summary['tokens'][tokens]['speed_med_kmh']}km/h "
              f"-> {out_dir}")
    (out_dir / f"summary{args.tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding="utf-8")


if __name__ == "__main__":
    main()
