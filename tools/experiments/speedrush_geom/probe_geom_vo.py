# -*- coding: utf-8 -*-
"""几何供数探针：地面 IPM + 亮度相位相关 + LSD 车道拟合 + 框底点测距。

不加载深度模型，不写颜色阈值。输出逐帧自车位移、走廊拟合、目标距离和延迟，
供 probe_crossval 与深度链对照。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_geom/probe_geom_vo.py \
        --video video_20261021_160547_p1 --start 400 --limit 300
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import PERCEPTION_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT = DATA / "geom"

CAM_W, CAM_H = 1280, 720
CX, CY = 640.0, 360.0

# 地面逆透视视野
BX0, BX1 = -10.0, 10.0
BZ0, BZ1 = 3.0, 45.0
RES = 0.10
EGO_HALF = 2.6

# 实验标定参数（显式记录，便于尺度核对）
Y_H = 324.2
CAM_HEIGHT = 1.90
FX = 559.2
FY = 560.0
MIN_DENOM = 20.0
CAR_W = 1.9
LSD = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD)


class BEV:
    def __init__(self):
        self.w = int(round((BX1 - BX0) / RES))
        self.h = int(round((BZ1 - BZ0) / RES))
        self.xs = BX0 + (np.arange(self.w) + 0.5) * RES
        self.zs = BZ0 + (np.arange(self.h) + 0.5) * RES
        X = self.xs[None, :]
        Z = self.zs[:, None]
        self.U = (CX + FX * X / Z).astype(np.float32)
        self.V = (Y_H + FY * CAM_HEIGHT / Z)
        self.V = np.broadcast_to(self.V, (self.h, self.w)).astype(np.float32)
        self.ok = ((self.U >= 0) & (self.U < CAM_W - 1)
                   & (self.V >= 0) & (self.V < CAM_H - 1))
        self.mask = np.ones((self.h, self.w), np.float32)
        self.mask[:, np.abs(self.xs) < EGO_HALF] = 0.0
        self.mask[:, np.abs(self.xs) > 9.5] = 0.0
        self.win = cv2.createHanningWindow((self.w, self.h), cv2.CV_32F)

    def image(self, gray):
        bev = cv2.remap(gray, np.clip(self.U, 0, CAM_W - 1),
                        np.clip(self.V, 0, CAM_H - 1), cv2.INTER_LINEAR)
        bev = bev.astype(np.float32)
        bev[~self.ok] = 0.0
        return bev

    def to_bev(self, us, vs):
        z = FY * CAM_HEIGHT / np.maximum(vs - Y_H, 1e-3)
        x = (us - CX) * z / FX
        ok = (vs > Y_H + 6) & (z > BZ0) & (z < BZ1) & (np.abs(x) < 9.5)
        return x, z, ok



FEAT_MASK = None


def road_feature_mask():
    global FEAT_MASK
    if FEAT_MASK is not None:
        return FEAT_MASK
    m = np.zeros((CAM_H, CAM_W), np.uint8)
    m[int(Y_H + 8):int(CAM_H * 0.80), int(CAM_W * 0.04):int(CAM_W * 0.96)] = 255
    m[int(CAM_H * 0.48):, int(CAM_W * 0.28):int(CAM_W * 0.72)] = 0
    m[int(CAM_H * 0.80):, :] = 0
    FEAT_MASK = m
    return m


def feature_motion(prev_gray, cur_gray, prior, use_init):
    """地面特征 + BEV 对应 → 3 自由度刚体（前移/横移/横摆），RANSAC。"""
    p0 = cv2.goodFeaturesToTrack(prev_gray, 900, 0.004, 7,
                                 mask=road_feature_mask())
    if p0 is None or len(p0) < 30:
        return None
    p0 = p0.reshape(-1, 2).astype(np.float32)
    u, v = p0[:, 0], p0[:, 1]
    z = FY * CAM_HEIGHT / np.maximum(v - Y_H, 1e-3)
    x = (u - CX) * z / FX
    ok = (z > 7.5) & (z < 42.0) & (np.abs(x) < 9.0)
    if ok.sum() < 30:
        return None
    p0, x, z = p0[ok], x[ok], z[ok]
    if use_init:
        d0, s0, psi0 = prior
        c, sn = np.cos(psi0), np.sin(psi0)
        dx, dz = x - s0, z - d0
        x2 = c * dx - sn * dz
        z2 = sn * dx + c * dz
        u2 = CX + FX * x2 / z2
        v2 = Y_H + FY * CAM_HEIGHT / z2
        init = np.stack([u2, v2], 1).astype(np.float32)
        p1, st, _ = cv2.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, p0.reshape(-1, 1, 2), init.reshape(-1, 1, 2),
            winSize=(31, 31), maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
            flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
    else:
        p1, st, _ = cv2.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, p0.reshape(-1, 1, 2), None,
            winSize=(31, 31), maxLevel=4,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    p0b, stb, _ = cv2.calcOpticalFlowPyrLK(
        cur_gray, prev_gray, p1, p0.reshape(-1, 1, 2),
        winSize=(31, 31), maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
    fb = np.linalg.norm((p0b - p0.reshape(-1, 1, 2)).reshape(-1, 2), axis=1)
    p1r = p1.reshape(-1, 2)
    u1, v1 = p1r[:, 0], p1r[:, 1]
    z1 = FY * CAM_HEIGHT / np.maximum(v1 - Y_H, 1e-3)
    x1 = (u1 - CX) * z1 / FX
    good = ((st.ravel() == 1) & (stb.ravel() == 1) & (fb < 2.0)
            & (z1 > 3.5) & (z1 < 44.0) & (np.abs(x1) < 10.0)
            & (p1r[:, 0] > 0) & (p1r[:, 0] < CAM_W - 1)
            & (p1r[:, 1] > Y_H + 6) & (p1r[:, 1] < CAM_H - 1))
    if good.sum() < 25:
        return None
    x, z, x1, z1 = x[good], z[good], x1[good], z1[good]
    n = len(x)
    best = None
    rng = np.random.default_rng(3)
    for _ in range(120):
        idx = rng.choice(n, 3, replace=False)
        # 线性化: x1 = x - s - psi*z ; z1 = z - d + psi*x
        A = []
        b = []
        for i in idx:
            # x1-x = -s - psi*z ; z1-z = -d + psi*x
            A.append([-1.0, 0.0, -z[i]])
            b.append(x1[i] - x[i])
            A.append([0.0, -1.0, x[i]])
            b.append(z1[i] - z[i])
        A = np.asarray(A)
        b = np.asarray(b)
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        s_est, d_est, psi_est = sol
        c, sn = np.cos(psi_est), np.sin(psi_est)
        dxp, dzp = x - s_est, z - d_est
        xe = c * dxp - sn * dzp
        ze = sn * dxp + c * dzp
        r = np.hypot(xe - x1, ze - z1)
        inl = r < 0.25
        if best is None or inl.sum() > best[0]:
            best = (int(inl.sum()), s_est, d_est, psi_est)
    if best is None or best[0] < 20:
        return None
    inl = None
    # 用最优解重算内点并再拟合一次
    _, s_est, d_est, psi_est = best
    c, sn = np.cos(psi_est), np.sin(psi_est)
    dxp, dzp = x - s_est, z - d_est
    xe = c * dxp - sn * dzp
    ze = sn * dxp + c * dzp
    r = np.hypot(xe - x1, ze - z1)
    inl = r < 0.25
    if inl.sum() < 20:
        return None
    # 在内点上用非线性最小二乘细化
    from scipy.optimize import least_squares

    def fun(p):
        s_, d_, psi_ = p
        c_, sn_ = np.cos(psi_), np.sin(psi_)
        dx_, dz_ = x[inl] - s_, z[inl] - d_
        return np.concatenate([c_ * dx_ - sn_ * dz_ - x1[inl],
                               sn_ * dx_ + c_ * dz_ - z1[inl]])
    sol = least_squares(fun, [s_est, d_est, psi_est], loss="soft_l1", f_scale=0.1)
    s_est, d_est, psi_est = (float(v) for v in sol.x)
    resid = float(np.median(np.abs(fun(sol.x))))
    return d_est, s_est, psi_est, int(inl.sum()), resid

def phase_motion(prev, cur, bev):
    A = (prev - prev[bev.mask > 0].mean()) * bev.mask
    B = (cur - cur[bev.mask > 0].mean()) * bev.mask
    (dx, dy), resp = cv2.phaseCorrelate(A, B, bev.win)
    return -dy * RES, -dx * RES, float(resp)


def lsd_lanes(gray, bev):
    """LSD 线段 → BEV → 左右各拟合二次曲线。返回中心/宽度/曲率与置信。"""
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    segs = LSD.detect(small)[0]
    if segs is None:
        return None, 0, 0
    segs = segs.reshape(-1, 4) * 2.0
    pts_l, pts_r = [], []
    for x1, y1, x2, y2 in segs:
        if max(y1, y2) < Y_H + 10:
            continue
        if np.hypot(x2 - x1, y2 - y1) < 18:
            continue
        xa, za, oka = bev.to_bev(np.array([x1]), np.array([y1]))
        xb, zb, okb = bev.to_bev(np.array([x2]), np.array([y2]))
        if not (oka[0] and okb[0]):
            continue
        dx, dz = xb[0] - xa[0], zb[0] - za[0]
        if abs(dz) < 1.0 or abs(dx / dz) > 0.6:
            continue
        # 取线段在两个 z 处的点，参与拟合
        for xx, zz in ((xa[0], za[0]), (xb[0], zb[0])):
            if abs(xx) < 8.0:
                (pts_l if xx < 0 else pts_r).append((zz, xx))

    def fit(pts):
        if len(pts) < 8:
            return None, 0
        P = np.array(pts)
        m = np.ones(len(P), bool)
        coef = None
        for _ in range(4):
            if m.sum() < 6:
                return None, 0
            A = np.stack([P[m, 0] ** 2, P[m, 0], np.ones(m.sum())], 1)
            coef, *_ = np.linalg.lstsq(A, P[m, 1], rcond=None)
            resid = np.abs(P[:, 1] - (coef[0] * P[:, 0] ** 2
                                      + coef[1] * P[:, 0] + coef[2]))
            nm = resid < max(0.25, 3.0 * np.median(resid[m]))
            if np.array_equal(nm, m):
                break
            m = nm
        if coef is None or m.sum() < 6:
            return None, 0
        rms = float(np.sqrt(np.mean(resid[m] ** 2)))
        return coef, (int(m.sum()), round(rms, 3))

    cl, nl = fit(pts_l)
    cr, nr = fit(pts_r)
    out = {"n_left": nl[0] if nl else 0, "n_right": nr[0] if nr else 0}
    for z in (10.0, 20.0, 40.0):
        xl = None if cl is None else float(cl[0] * z * z + cl[1] * z + cl[2])
        xr = None if cr is None else float(cr[0] * z * z + cr[1] * z + cr[2])
        out[f"z{int(z)}"] = {
            "xl": None if xl is None else round(xl, 2),
            "xr": None if xr is None else round(xr, 2),
            "center": None if (xl is None or xr is None)
            else round(0.5 * (xl + xr), 2),
            "width": None if (xl is None or xr is None)
            else round(xr - xl, 2)}
    if cl is not None:
        out["curv_left"] = round(float(2 * cl[0]), 5)
    if cr is not None:
        out["curv_right"] = round(float(2 * cr[0]), 5)
    return out, len(pts_l) + len(pts_r), len(segs)


def targets(per, bev):
    out = []
    for kind, dets in (("car", per.cars), ("coin", per.coins),
                       ("bonus", per.bonuses)):
        for d in dets:
            u = d.cx
            v = d.cy + d.h / 2
            if v - Y_H <= MIN_DENOM:
                continue
            z = FY * CAM_HEIGHT / (v - Y_H)
            x = (u - CX) * z / FX
            item = {"kind": kind, "u": int(u), "v": int(v),
                    "z_ground": round(float(z), 2),
                    "x": round(float(x), 2),
                    "w_px": int(d.w), "conf": round(float(d.conf), 3)}
            if kind == "car" and d.w > 1:
                item["z_width"] = round(float(FX * CAR_W / d.w), 2)
            out.append(item)
    return out


def frame_list(args):
    if args.video:
        files = sorted((DEMOS / args.video).glob("*.jpg"))
        items = [(p, i) for i, p in enumerate(files)]
        return items[args.start:args.start + args.limit], 1.0 / args.fps
    sess = DEMOS / args.sess
    rows = [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    rows = rows[args.start:args.start + args.limit]
    return [(sess / "frames" / r["file"], r["ts_ns"] / 1e9) for r in rows], None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=None)
    ap.add_argument("--sess", default=None)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    if not args.video and not args.sess:
        ap.error("--video 或 --sess 至少一个")

    items, fixed_dt = frame_list(args)
    name = args.video or args.sess
    out_dir = OUT / name
    out_dir.mkdir(parents=True, exist_ok=True)
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    bev = BEV()
    prev = None
    rows = []
    t0 = time.perf_counter()
    writer = None
    if args.render:
        writer = cv2.VideoWriter(str(out_dir / "geom.mp4"),
                                 cv2.VideoWriter_fourcc(*"mp4v"), 20.0,
                                 (bev.w + 640, bev.h))

    for k, (path, ts) in enumerate(items):
        img = cv2.imread(str(path))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        t_y = time.perf_counter()
        per = perc.detect(rgb, frame_id=k, ts_ns=int(ts * 1e9))
        yolo_ms = (time.perf_counter() - t_y) * 1000.0

        t_b = time.perf_counter()
        bimg = bev.image(gray)
        bev_ms = (time.perf_counter() - t_b) * 1000.0

        row = {"k": k, "ts": round(float(ts), 4), "yolo_ms": round(yolo_ms, 1),
               "bev_ms": round(bev_ms, 1), "d": None, "s": None,
               "resp": None, "lanes": None, "targets": None,
               "total_ms": None}
        if prev is not None:
            t_p = time.perf_counter()
            d, s, resp = phase_motion(prev["bev"], bimg, bev)
            # 相位相关是几何测速主供应；响应/位移范围做诚实门控
            okm = (resp > 0.35) and (0.30 < d < 2.20) and (abs(s) < 0.5)
            row.update({"d": round(d, 3), "s": round(s, 3),
                        "resp": round(resp, 3), "d_ok": bool(okm),
                        "src": "phase",
                        "phase_ms": round((time.perf_counter() - t_p) * 1000.0, 1)})
        t_l = time.perf_counter()
        lanes, npts, nseg = lsd_lanes(gray, bev)
        row["lanes"] = lanes
        row["lsd_n"] = {"pts": npts, "seg": nseg}
        row["lsd_ms"] = round((time.perf_counter() - t_l) * 1000.0, 1)
        row["targets"] = targets(per, bev)
        row["total_ms"] = round(sum(row[x] for x in
                                    ("yolo_ms", "bev_ms", "lsd_ms")
                                    if x in row)
                                + row.get("phase_ms", 0.0), 1)
        rows.append(row)
        prev = {"bev": bimg, "gray": gray,
                "vel": prev.get("vel", (1.0, 0.0, 0.0)) if prev else (1.0, 0.0, 0.0),
                "init": prev.get("init", False) if prev else False}

        if writer is not None and k % 5 == 0:
            vis = cv2.cvtColor(cv2.resize(img, (640, 360)), cv2.COLOR_BGR2RGB)
            canvas = np.zeros((bev.h, bev.w + 640, 3), np.uint8)
            canvas[:, :bev.w] = cv2.cvtColor(
                (bimg / max(float(bimg.max()), 1) * 255).astype(np.uint8),
                cv2.COLOR_GRAY2BGR)
            for z in (10, 20, 40):
                if lanes and lanes.get(f"z{z}"):
                    for side, col in (("xl", (0, 200, 255)),
                                      ("xr", (255, 160, 0))):
                        x = lanes[f"z{z}"][side]
                        if x is None:
                            continue
                        c = int((x - BX0) / RES)
                        r = int((z - BZ0) / RES)
                        if 0 <= c < bev.w:
                            cv2.circle(canvas, (c, r), 4, col, -1)
            canvas[:360, bev.w:] = vis
            if row["d"] is not None:
                cv2.putText(canvas,
                            f"v={row['d']*60:5.1f}m/s lat={row['s']*60:+.2f} "
                            f"resp={row['resp']:.2f}",
                            (bev.w + 8, 385), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (230, 230, 230), 1, cv2.LINE_AA)
            writer.write(canvas)
        if k % 100 == 0:
            print(f"  {k+1}/{len(items)}  {time.perf_counter()-t0:.0f}s",
                  flush=True)

    if writer is not None:
        writer.release()
    with (out_dir / "geom.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    v = [r for r in rows if r["d"] is not None]
    print(f"{name}: {len(rows)} 帧，{time.perf_counter()-t0:.0f}s → {out_dir}")
    if v:
        print(f"  运动有效: {sum(1 for r in v if r.get('d_ok'))}/{len(v)}")
        for key in ("d", "s", "resp", "total_ms"):
            vals = [r[key] for r in v if r[key] is not None]
            if vals:
                print(f"  {key}: 中位 {np.median(vals):.3f}  "
                      f"10-90% {np.percentile(vals,10):.3f}~"
                      f"{np.percentile(vals,90):.3f}")
        lanes = [r for r in v if r["lanes"] and r["lanes"].get("z20")]
        if lanes:
            w = [r["lanes"]["z20"]["width"] for r in lanes
                 if r["lanes"]["z20"]["width"] is not None]
            if w:
                print(f"  z=20m 拟合宽度: 中位 {np.median(w):.2f}m "
                      f"(参考单车道 3.38m / 四车道 13.5m)")


if __name__ == "__main__":
    main()
