# -*- coding: utf-8 -*-
"""3D 时序融合可行性探针（C 类实验，只读回放帧）。

位姿来自「地面逆透视 + 车道标记跟踪」：用 MoGe 拟合的路面把下半个画面
重投影成米制 BEV，在地平线以下的地面纹理上做跨帧配准（前向/横移用相位
相关，横摆用标记点位移对 z 的回归）。三维结构来自 MoGe 点图，按位姿累积
成世界系点云并渲染 BEV。逐帧指标用于判断：
  - 点云跨帧是否重合（位姿对不对）
  - 走廊自由空间是否稳定（深度对不对）
  - 误差归谁（位姿 / 深度 / 动态物体）

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_3d_fusion.py \
        --video video_20261021_160547_p1 --start 300 --limit 400 --render
    python tools/experiments/speedrush_planning/probe_3d_fusion.py \
        --sess manual_20261020_145444_p1 --start 0 --limit 300 --render

输出（数据目录）: 3d_fusion/<name>/metrics.jsonl、bev.mp4、样张。
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
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    _band_rows, _fit_road_plane, infer_points, load_session)
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT_ROOT = DATA / "3d_fusion"

CAM_W, CAM_H = 1280, 720
CX, CY = CAM_W * 0.5, CAM_H * 0.5
FX, FY = 559.2, 560.0          # 全幅像素焦距（MoGe 每帧另给归一值，诊断同口径）

# 地面逆透视视野（自车系：x 右、z 前）
BX0, BX1 = -16.0, 16.0
BZ0, BZ1 = 2.0, 50.0
RES = 0.10
BLOB_THR = 120
EGO_HALF = 2.4                  # BEV 里挖掉自车本体的半宽（米）


def frame_list(args):
    if args.video:
        files = sorted((DEMOS / args.video).glob("*.jpg"))
        items = [(p, int(round(i * 1e9 / args.fps))) for i, p in enumerate(files)]
        return items[args.start:args.start + args.limit], 1.0 / args.fps
    sess = DEMOS / args.sess
    rows = [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    rows = rows[args.start:args.start + args.limit]
    return [(sess / "frames" / r["file"], int(r["ts_ns"])) for r in rows], None


def boxes_of(per):
    return [(d.cx - d.w / 2, d.cy - d.h / 2, d.cx + d.w / 2, d.cy + d.h / 2)
            for d in (per.cars + per.coins + per.bonuses)]


class GroundBEV:
    """固定像素网格 → 地面米制 BEV 的重投影（平面系数每帧更新）。"""

    def __init__(self):
        self.w = int(round((BX1 - BX0) / RES))
        self.h = int(round((BZ1 - BZ0) / RES))
        self.xs = BX0 + (np.arange(self.w) + 0.5) * RES
        self.zs = BZ0 + (np.arange(self.h) + 0.5) * RES

    def maps(self, plane):
        a, b, c = plane
        X = self.xs[None, :]
        Z = self.zs[:, None]
        Y = a * X + b * Z + c
        U = (CX + FX * X / Z).astype(np.float32)
        V = (CY + FY * Y / Z).astype(np.float32)
        ok = (U >= 0) & (U < CAM_W - 1) & (V >= 0) & (V < CAM_H - 1)
        return U, V, ok

    def image(self, gray, plane):
        U, V, ok = self.maps(plane)
        bev = cv2.remap(gray, np.clip(U, 0, CAM_W - 1),
                        np.clip(V, 0, CAM_H - 1), cv2.INTER_LINEAR)
        bev = bev.astype(np.float32)
        bev[~ok] = 0.0
        return bev, ok


def detect_blobs(bev, ok, geom, ego_half=EGO_HALF):
    """BEV 里的亮色车道虚线短段（长实线另走线拟合，不在这里当点用）。"""
    bw = (bev > BLOB_THR).astype(np.uint8) * 255
    bw[~ok] = 0
    bw[:, np.abs(geom.xs) < ego_half] = 0
    # 墙/护栏是竖直结构，逆透视到地面会给出错误 z——只留路面标线带
    bw[:, np.abs(geom.xs) > 9.0] = 0
    n, _, st, cent = cv2.connectedComponentsWithStats(bw, 8)
    out = []
    for i in range(1, n):
        w, h, area = int(st[i, 2]), int(st[i, 3]), int(st[i, 4])
        if area >= 25 and 8 <= h <= 130 and 1 <= w <= 30:
            out.append((BX0 + cent[i][0] * RES, BZ0 + cent[i][1] * RES))
    return out


def _match_once(prev_blobs, cur_blobs, prior):
    d0, s0, _ = prior
    pairs = []
    for x, z in prev_blobs:
        px, pz = x - s0, z - d0
        best = None
        for cx, cz in cur_blobs:
            e = abs(cx - px) + abs(cz - pz)
            if e < 0.45 and (best is None or e < best[0]):
                best = (e, cx, cz)
        if best is not None:
            pairs.append((best[1] - x, best[2] - z, z, best[0]))
    return pairs


def lane_anchor(bev, ok, geom):
    """车道边线拟合 → 路面中心相对自车的横位与夹角（米/弧度）。

    只取 z 5~22m 的地面带；左右两侧各自取亮像素中位，再做一次去外点
    直线拟合 x = m*z + b。用来给长时累积补横向/航向的绝对锚。"""
    left, right = [], []
    r0 = int((5.0 - BZ0) / RES)
    r1 = int((22.0 - BZ0) / RES)
    for r in range(max(r0, 0), min(r1, geom.h), 4):
        bright = np.where(bev[r] > BLOB_THR)[0]
        if len(bright) < 2:
            continue
        xb = geom.xs[bright]
        l = xb[(xb > -9.0) & (xb < -2.6)]
        rr = xb[(xb > 2.6) & (xb < 9.0)]
        if len(l):
            left.append((geom.zs[r], float(np.median(l))))
        if len(rr):
            right.append((geom.zs[r], float(np.median(rr))))

    def fit(pts):
        if len(pts) < 8:
            return None
        P = np.array(pts)
        m = np.ones(len(P), bool)
        coef = None
        for _ in range(3):
            if m.sum() < 6:
                return None
            A = np.stack([P[m, 0], np.ones(m.sum())], 1)
            coef, *_ = np.linalg.lstsq(A, P[m, 1], rcond=None)
            resid = np.abs(P[:, 1] - (coef[0] * P[:, 0] + coef[1]))
            nm = resid < max(0.15, 3.0 * np.median(resid[m]))
            if np.array_equal(nm, m):
                break
            m = nm
        return float(coef[0]), float(coef[1])

    fl, fr = fit(left), fit(right)
    if fl is None and fr is None:
        return None
    if fl is None:
        fl = fr
    if fr is None:
        fr = fl
    zref = 12.0
    center = 0.5 * ((fl[0] + fr[0]) * zref + fl[1] + fr[1])
    heading = float(np.arctan(0.5 * (fl[0] + fr[0])))
    return center, heading


def match_motion(prev_blobs, cur_blobs, prior, wide=False, scale=1.0):
    """虚线点匹配 → (前移, 横移, 横摆)。启动阶段允许大范围前向偏移，
    稳定后只在前验附近微调，避免锁到错误速度。scale=dt/(1/60)，兼容低帧率。"""
    best = None
    offs = tuple(x * scale for x in
                 ((0.0, 0.4, 0.8, 1.2, -0.4) if wide else (-0.2, 0.0, 0.2)))
    for dd in offs:
        pairs = _match_once(prev_blobs, cur_blobs,
                            (prior[0] + dd, prior[1], prior[2]))
        if len(pairs) >= 3:
            cost = float(np.median([p[3] for p in pairs]))
            key = (len(pairs), -cost)
            if best is None or key > best[0]:
                best = (key, pairs)
    if best is None:
        return None
    pairs = best[1]
    dx = np.array([p[0] for p in pairs])
    dz = np.array([p[1] for p in pairs])
    zz = np.array([p[2] for p in pairs])
    d = float(-np.median(dz))
    if np.ptp(zz) > 4.0:
        A = np.stack([np.ones_like(zz), -zz], 1)
        coef, *_ = np.linalg.lstsq(A, dx, rcond=None)
        s = float(-coef[0])
        psi = float(coef[1])
    else:
        s = float(-np.median(dx))
        psi = 0.0
    return d, s, psi, len(pairs)


def phase_motion(prev_bev, cur_bev, mask, win):
    A = (prev_bev - prev_bev[mask > 0].mean()) * mask
    B = (cur_bev - cur_bev[mask > 0].mean()) * mask
    (dx, dy), resp = cv2.phaseCorrelate(A, B, win)
    return -dy * RES, -dx * RES, float(resp)


def to_world(X, Y, Z, pose):
    c, s = np.cos(pose[2]), np.sin(pose[2])
    wx = c * X + s * Z + pose[0]
    wz = -s * X + c * Z + pose[1]
    return wx, Y, wz


def to_body(wx, wy, wz, pose):
    dx, dz = wx - pose[0], wz - pose[1]
    c, s = np.cos(pose[2]), np.sin(pose[2])
    return c * dx - s * dz, wy, s * dx + c * dz


def subsample(pts, valid, obj_mask, step=2, zmin=2.0, zmax=45.0):
    X = pts[::step, ::step, 0]
    Y = pts[::step, ::step, 1]
    Z = pts[::step, ::step, 2]
    ok = valid[::step, ::step] & np.isfinite(Z) & (Z > zmin) & (Z < zmax) \
        & (np.abs(X) < 18.0) & (Y > -4.0) & (Y < 8.0)
    # 自车 3D 框（车体/尾翼/阴影）不是世界结构
    ok &= ~((np.abs(X) < 1.4) & (Z < 10.0))
    if obj_mask is not None:
        ok &= ~obj_mask[::step, ::step]
    return X[ok], Y[ok], Z[ok]


def nn_score(ax, az, bx, bz):
    if len(ax) < 200 or len(bx) < 200:
        return None, None
    A = np.stack([ax, az], 1)[:: max(1, len(ax) // 15000)]
    B = np.stack([bx, bz], 1)[:: max(1, len(bx) // 15000)]
    dist, _ = cKDTree(B).query(A, k=1, workers=-1)
    return float(np.median(dist)), float(np.percentile(dist, 90))


def render_bev(points, dets):
    w = int(round((BX1 - BX0) / RES))
    h = int(round((BZ1 - BZ0) / RES))
    img = np.zeros((h, w, 3), np.uint8)
    if points:
        bx = np.concatenate([p[0] for p in points])
        bz = np.concatenate([p[1] for p in points])
        hg = np.concatenate([p[2] for p in points])
        c = ((bx - BX0) / RES).astype(int)
        r = ((BZ1 - bz) / RES).astype(int)
        ok = (c >= 0) & (c < w) & (r >= 0) & (r < h)             & (hg > -0.4) & (hg < 3.5)
        if ok.any():
            idx = r[ok] * w + c[ok]
            hh = hg[ok]
            n = np.bincount(idx, minlength=h * w).reshape(h, w)
            ssum = np.bincount(idx, weights=hh, minlength=h * w).reshape(h, w)
            ssq = np.bincount(idx, weights=hh * hh, minlength=h * w).reshape(h, w)
            mean = np.where(n > 0, ssum / np.maximum(n, 1), -9.0).astype(np.float32)
            var = np.where(n > 0, ssq / np.maximum(n, 1) - mean * mean, 0.0)
            std = np.sqrt(np.maximum(var, 0.0)).astype(np.float32)
            vis = n >= 2
            img[vis] = (70, 70, 70)
            img[vis & (mean > 0.12) & (mean <= 0.45)] = (60, 140, 200)
            img[vis & (mean > 0.45) & (mean <= 1.2)] = (40, 140, 230)
            img[vis & (mean > 1.2)] = (40, 40, 230)
            # 观测不一致的格子退暗：厚度/方差即误差可观测面
            unstable = vis & (std > 0.6)
            img[unstable] = (img[unstable].astype(np.float32) * 0.35).astype(np.uint8)
            stats = {"cells": int((n >= 3).sum()),
                     "std_med": float(np.median(std[n >= 3])) if (n >= 3).any() else None,
                     "stable_frac": float((std[n >= 3] < 0.3).mean()) if (n >= 3).any() else None}
        else:
            stats = {"cells": 0, "std_med": None, "stable_frac": None}
    else:
        stats = {"cells": 0, "std_med": None, "stable_frac": None}
    for xm in range(int(BX0), int(BX1) + 1, 5):
        c = int((xm - BX0) / RES)
        img[::4, max(0, min(w - 1, c))] = (35, 35, 35)
    for zm in range(int(BZ0), int(BZ1) + 1, 5):
        r = int((BZ1 - zm) / RES)
        img[max(0, min(h - 1, r)), ::4] = (35, 35, 35)
    cv2.rectangle(img, (int((0.95 - BX0) / RES), int((BZ1 - 2.2) / RES)),
                  (int((-0.95 - BX0) / RES), int((BZ1 + 2.2) / RES)),
                  (255, 255, 255), 1)
    for x, z, wm in dets:
        c, r = int((x - BX0) / RES), int((BZ1 - z) / RES)
        if 0 <= c < w and 0 <= r < h:
            hw = max(int(wm / 2 / RES), 2)
            cv2.rectangle(img, (c - hw, r - 5), (c + hw, r + 5), (60, 220, 255), 1)
    cv2.putText(img, "BEV 0.1m/cell cnt>=2 gray=ground dim=unstable",
                (6, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (150, 150, 150), 1,
                cv2.LINE_AA)
    return img, stats


def run(args) -> None:
    items, fixed_dt = frame_list(args)
    name = args.video or args.sess
    out_dir = OUT_ROOT / name
    out_dir.mkdir(parents=True, exist_ok=True)
    metric_path = out_dir / "metrics.jsonl"

    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    sess = load_session(DEPTH_MODEL_FILE)
    geom = GroundBEV()
    win = cv2.createHanningWindow((geom.w, geom.h), cv2.CV_32F)
    bev_mask = np.ones((geom.h, geom.w), np.float32)
    bev_mask[:, np.abs(geom.xs) < EGO_HALF] = 0.0
    bev_mask[:, np.abs(geom.xs) > 10.0] = 0.0

    bev_h = int(round((BZ1 - BZ0) / RES))
    bev_w = int(round((BX1 - BX0) / RES))
    writer = None
    if args.render:
        writer = cv2.VideoWriter(
            str(out_dir / "bev.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
            args.video_fps, (bev_w + 640, bev_h))

    pose = [0.0, 0.0, 0.0]        # x, z, psi
    vel = np.array([0.0, 0.0, 0.0])   # d, s, psi（每帧增量，米/弧度）
    plane_hold = None               # BEV 几何用固定平面：逐帧拟合的抖动会毁掉虚线匹配
    d_win = deque(maxlen=args.smooth)
    s_win = deque(maxlen=args.smooth)
    p_win = deque(maxlen=args.smooth)
    prev = None
    hist = []                     # [(X, Y, Z, pose)]
    gap_ref = None
    rows = []
    t_start = time.perf_counter()

    for k, (path, ts_ns) in enumerate(items):
        img = cv2.imread(str(path))
        if img is None:
            continue
        if img.shape[1] != CAM_W:
            img = cv2.resize(img, (CAM_W, CAM_H))
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        per = perc.detect(rgb, frame_id=k, ts_ns=ts_ns)
        boxes = boxes_of(per)

        t_d = time.perf_counter()
        pts, _, _, evid = infer_points(sess, rgb, with_evidence=True, native=True)
        valid = evid["valid"]
        ph, pw = pts.shape[:2]
        fy_native = evid["fy"] * ph
        y0, y1 = _band_rows(ph)
        coef = _fit_road_plane(pts[y0:y1, :, 0], pts[y0:y1, :, 1],
                               pts[y0:y1, :, 2], fy=fy_native)
        if coef is not None:
            plane = tuple(float(x) for x in coef)
            plane_dist = plane[2] / float(np.sqrt(1.0 + plane[0] ** 2
                                                 + plane[1] ** 2))
        else:
            plane = (0.0, -0.078, 1.80)
            plane_dist = float("nan")
        depth_ms = (time.perf_counter() - t_d) * 1000.0

        if plane_hold is None:
            plane_hold = plane
        t_b = time.perf_counter()
        bev, ok = geom.image(gray, plane_hold)
        blobs = detect_blobs(bev, ok, geom)
        bev_ms = (time.perf_counter() - t_b) * 1000.0

        row = {"k": k, "fid": per.frame_id, "ts_ms": ts_ns / 1e6,
               "ms": round(float(evid["ms"]), 3),
               "plane_dist": round(plane_dist, 3),
               "plane_b": round(plane[1], 4), "nv": int(valid.sum()),
               "depth_ms": round(depth_ms, 1), "bev_ms": round(bev_ms, 1),
               "n_blob": len(blobs), "n_pair": 0,
               "d": None, "s": None, "psi": None, "resp": None,
               "src": None, "nn_gap": None, "nn_gap_id": None,
               "x": 0.0, "z": 0.0, "yaw": 0.0,
               "pa": round(plane[0], 5), "pb": round(plane[1], 5),
               "pc": round(plane[2], 4)}

        if prev is not None:
            dt = ((ts_ns - prev["ts"]) / 1e9) if fixed_dt is None else fixed_dt
            if 0.008 < dt < 0.12:
                prior = (float(np.median(d_win)) if d_win else 0.0,
                         float(np.median(s_win)) if s_win else 0.0,
                         float(np.median(p_win)) if p_win else 0.0)
                boot = len(d_win) < 4
                tscale = dt * 60.0
                mm = match_motion(prev["blobs"], blobs, prior, wide=boot,
                                  scale=tscale)
                if mm is not None:
                    d, s, psi, npair = mm
                    row["src"] = "blob"
                    row["n_pair"] = npair
                else:
                    d, s, resp = phase_motion(prev["bev"], bev, bev_mask, win)
                    psi = prior[2]
                    row["resp"] = round(resp, 3)
                    row["src"] = "phase" if resp > (0.20 if boot else 0.12)                         else "prior"
                    if row["src"] == "prior":
                        d, s = prior[0], prior[1]
                # 单帧限幅（相对平滑先验）；启动阶段放开，否则锁不上初速
                lim = (2.0 if boot else 0.25) * tscale
                d = float(np.clip(d, prior[0] - lim, prior[0] + lim))
                s = float(np.clip(s, prior[1] - 0.15, prior[1] + 0.15))
                psi = float(np.clip(psi, prior[2] - 0.010 * tscale,
                                    prior[2] + 0.010 * tscale))
                if abs(d) < 3.0 * tscale and abs(s) < 1.0                         and abs(psi) < 0.15 * tscale:
                    if row["src"] != "prior":
                        d_win.append(d)
                        s_win.append(s)
                        p_win.append(psi)
                    vel = np.array([float(np.median(d_win)),
                                    float(np.median(s_win)),
                                    float(np.median(p_win))])
                    row.update({"d": round(d, 3), "s": round(s, 3),
                                "psi": round(psi, 5),
                                "d_f": round(vel[0], 3)})
                    d, s, psi = (float(x) for x in vel)
                    pm = pose[2] + 0.5 * psi
                    pose[0] += s * np.cos(pm) + d * np.sin(pm)
                    pose[1] += -s * np.sin(pm) + d * np.cos(pm)
                    pose[2] += psi
                    if args.anchor > 0:
                        anc = lane_anchor(bev, ok, geom)
                        if anc is not None:
                            c_m, h_m = anc
                            pose[0] += args.anchor * (-c_m - pose[0])
                            pose[2] += args.anchor * (-h_m - pose[2])
                            row["anchor_c"] = round(c_m, 3)
                            row["anchor_h"] = round(h_m, 5)
                else:
                    row["src"] = "reject"

                if gap_ref is not None:
                    sx, sy, sz = subsample(pts, valid, None, args.sub)
                    if len(sx) > 100:
                        wx, _, wz = to_world(sx, sy, sz, pose)
                        nn, _ = nn_score(gap_ref["wx"], gap_ref["wz"], wx, wz)
                        nn_id, _ = nn_score(gap_ref["X"], gap_ref["Z"], sx, sz)
                        row["nn_gap"] = None if nn is None else round(nn, 3)
                        row["nn_gap_id"] = None if nn_id is None else round(nn_id, 3)

        sub = subsample(pts, valid, None, args.sub)
        if len(sub[0]) > 0:
            hist.append((sub[0], sub[1], sub[2], list(pose)))
            while len(hist) > args.hist:
                hist.pop(0)

        if k % args.gap == 0:
            sx, sy, sz = sub
            if len(sx) > 100:
                wx, _, wz = to_world(sx, sy, sz, pose)
                gap_ref = {"X": sx, "Y": sy, "Z": sz,
                           "wx": wx, "wz": wz, "pose": list(pose)}

        if writer is not None and k % args.render_step == 0:
            allp = []
            for X, Y, Z, hp in hist:
                wx, wy, wz = to_world(X, Y, Z, hp)
                bx, by, bz = to_body(wx, wy, wz, pose)
                # 高度 = 当前帧地面平面在该 (x,z) 的 Y 减去点的 Y
                gy = plane_hold[0] * bx + plane_hold[1] * bz + plane_hold[2]
                hgt = gy - by
                m = (hgt > -0.6) & (hgt < 4.5) & (bx > BX0) & (bx < BX1) \
                    & (bz > BZ0) & (bz < BZ1)
                allp.append((bx[m], bz[m], hgt[m]))
            det3 = []
            for (x0, y0b, x1, y1b) in boxes:
                gu = int(np.clip((x0 + x1) / 2, 0, CAM_W - 1) * pw / CAM_W)
                gv = int(np.clip(y1b, 0, CAM_H - 1) * ph / CAM_H)
                zz = pts[gv, gu, 2]
                xx = pts[gv, gu, 0]
                if np.isfinite(zz) and 1.0 < zz < 60:
                    det3.append((float(xx), float(zz),
                                 float((x1 - x0) * zz / (evid["fx"] * CAM_W))))
            bev_img, mstats = render_bev(allp, det3)
            row.update({"cells": mstats["cells"],
                        "std_med": None if mstats["std_med"] is None
                        else round(mstats["std_med"], 3),
                        "stable_frac": None if mstats["stable_frac"] is None
                        else round(mstats["stable_frac"], 3)})
            cam = cv2.cvtColor(cv2.resize(img, (640, 360)), cv2.COLOR_BGR2RGB)
            canvas = np.zeros((bev_h, bev_w + 640, 3), np.uint8)
            canvas[:, :bev_w] = bev_img
            canvas[:360, bev_w:] = cam
            d, s, psi = (float(x) for x in vel)
            cv2.putText(canvas,
                        f"v={d*60:5.1f}m/s lat={s*60:+.2f} "
                        f"yaw={np.degrees(psi*60):+6.2f}deg/s",
                        (bev_w + 8, 385), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (230, 230, 230), 1, cv2.LINE_AA)
            cv2.putText(canvas,
                        f"pose x={pose[0]:+.1f} z={pose[1]:.1f} "
                        f"psi={np.degrees(pose[2]):+.1f}",
                        (bev_w + 8, 405), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (230, 230, 230), 1, cv2.LINE_AA)
            writer.write(canvas)
            if k % (args.render_step * 40) == 0:
                cv2.imwrite(str(out_dir / f"bev_{k:05d}.jpg"), canvas)

        row.update({"x": round(pose[0], 3), "z": round(pose[1], 3),
                    "yaw": round(pose[2], 5)})
        rows.append(row)
        prev = {"bev": bev, "blobs": blobs, "ts": ts_ns}

        if k % 100 == 0:
            el = time.perf_counter() - t_start
            print(f"  {k+1}/{len(items)}  {el:.0f}s  "
                  f"v={vel[0]*60:.1f} lat={vel[1]*60:+.2f} "
                  f"yaw={np.degrees(vel[2]*60):+.1f}", flush=True)

    if writer is not None:
        writer.release()
    with metric_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    okd = [r for r in rows if r.get("d") is not None]
    print(f"\n{name}: {len(rows)} 帧，有效运动 {len(okd)}，"
          f"{time.perf_counter()-t_start:.0f}s → {out_dir}")
    for key in ("d", "s", "psi", "resp", "nn_gap", "nn_gap_id"):
        vals = [r[key] for r in okd if r.get(key) is not None]
        if vals:
            print(f"  {key}: 中位 {np.median(vals):.4f}  "
                  f"10-90% {np.percentile(vals,10):.4f}~"
                  f"{np.percentile(vals,90):.4f}")
    src = {}
    for r in okd:
        src[r["src"]] = src.get(r["src"], 0) + 1
    print(f"  位姿来源: {src}")


def main() -> None:
    ap = argparse.ArgumentParser(description="3D 时序融合可行性探针")
    ap.add_argument("--video", default=None, help="demos 下的纯帧视频目录名")
    ap.add_argument("--sess", default=None, help="demos 下的人工局名")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--gap", type=int, default=5, help="跨帧重合的间隔帧数")
    ap.add_argument("--sub", type=int, default=2, help="点云抽稀步长")
    ap.add_argument("--hist", type=int, default=60, help="BEV 累积帧数")
    ap.add_argument("--smooth", type=int, default=9, help="位姿中值滑窗帧数")
    ap.add_argument("--anchor", type=float, default=0.0,
                    help="车道边线横位/航向锚的互补滤波系数（0=关闭）")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--render-step", type=int, default=3)
    ap.add_argument("--video-fps", type=float, default=20.0)
    args = ap.parse_args()
    if not args.video and not args.sess:
        ap.error("--video 或 --sess 至少给一个")
    run(args)


if __name__ == "__main__":
    main()
