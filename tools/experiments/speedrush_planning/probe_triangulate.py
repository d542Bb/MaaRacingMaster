# -*- coding: utf-8 -*-
"""多帧三角化可行性探针（C 类实验，只读回放帧 + 融合位姿）。

用 probe_3d_fusion 产出的逐帧位姿（metrics.jsonl 的 x/z/yaw + 每帧增量
d/s/psi），把墙面/护栏上的稀疏特征逐帧跟踪，再做 DLT 三角化。与同一像素
的 MoGe 单帧深度对照，回答「时序结构能否替代/补强 MoGe 远场深度」。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_triangulate.py \
        --video video_20261021_160547_p1 --start 400 --limit 60

输出（数据目录）: 3d_fusion/<name>/triangulate.jsonl、triangulate.png
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    infer_points, load_session)

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT = DATA / "3d_fusion"
CX, CY = 640.0, 360.0
FX, FY = 559.2, 560.0


def load_frames(video, start, limit):
    files = sorted((DEMOS / video).glob("*.jpg"))[start:start + limit]
    return files


def load_metrics(video):
    path = OUT / video / "metrics.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    return rows


def side_mask(h, w):
    m = np.zeros((h, w), np.uint8)
    y0, y1 = int(h * 0.44), int(h * 0.74)
    m[y0:y1, :int(w * 0.40)] = 255
    m[y0:y1, int(w * 0.60):] = 255
    return m


def predict_px(u, v, z, d, s, psi, pb):
    """当前帧像素 + 深度 → 下一帧像素（地面系平面运动 + 固定俯仰）。"""
    X = (u - CX) * z / FX
    Y = ((v - CY) / FY - pb) * z
    c, sn = np.cos(psi), np.sin(psi)
    dx, dz = X - s, z - d
    X2 = c * dx - sn * dz
    Z2 = sn * dx + c * dz
    u2 = CX + FX * X2 / Z2
    v2 = CY + FY * (Y / Z2 + pb)
    return u2, v2


def project(P, pose, pb):
    """世界点 (X,Y,Z 向下为正) → 像素。"""
    x, z, psi = pose
    c, s = np.cos(psi), np.sin(psi)
    dx, dz = P[0] - x, P[2] - z
    bx = c * dx - s * dz
    bz = s * dx + c * dz
    u = CX + FX * bx / bz
    v = CY + FY * (P[1] / bz + pb)
    return u, v


def triangulate(obs, poses, pb):
    """obs: [(t,u,v)]；DLT（v 方程带固定俯仰项）。"""
    A = []
    for t, u, v in obs:
        x, z, psi = poses[t]
        c, s = np.cos(psi), np.sin(psi)
        # [bx,by,bz] = R*(P-t)，R 行：bx = c*X - s*Z；by=Y；bz=s*X+c*Z
        # 常数项：-R*t
        rx = -c * x + s * z
        rz = -s * x - c * z
        # (u-CX)*bz/FX - bx = 0
        row_u = np.array([
            (u - CX) * s / FX - c,
            0.0,
            (u - CX) * c / FX + s,
            (u - CX) * rz / FX - rx,
        ])
        kv = (v - CY) / FY - pb
        row_v = np.array([
            kv * s,
            -1.0,
            kv * c,
            kv * rz,
        ])
        A.append(row_u)
        A.append(row_v)
    A = np.asarray(A)
    _, _, vt = np.linalg.svd(A)
    X = vt[-1]
    if abs(X[3]) < 1e-9:
        return None
    return X[:3] / X[3]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="video_20261021_160547_p1")
    ap.add_argument("--start", type=int, default=400)
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    files = load_frames(args.video, args.start, args.limit)
    rows = load_metrics(args.video)[:len(files)]
    pb = float(rows[0]["pb"])
    poses = [(float(r["x"]), float(r["z"]), float(r["yaw"])) for r in rows]
    sess = load_session(DEPTH_MODEL_FILE)

    # 首帧特征 + 逐帧跟踪
    g0 = cv2.cvtColor(cv2.imread(str(files[0])), cv2.COLOR_BGR2GRAY)
    h, w = g0.shape
    p0 = cv2.goodFeaturesToTrack(g0, 800, 0.005, 8,
                                 mask=side_mask(h, w))
    if p0 is None:
        print("首帧无特征")
        return
    print('start corners', len(p0))
    tracks = {i: [(0, float(p[0][0]), float(p[0][1]))] for i, p in enumerate(p0)}
    gray_prev = g0
    pix_prev = p0.reshape(-1, 2).copy()
    depths = []
    for k, f in enumerate(files):
        img = cv2.imread(str(f))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        pts, _, _, ev = infer_points(
            sess, cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
            with_evidence=True, native=True)
        ph, pw = pts.shape[:2]
        if k == 0:
            u = np.clip((pix_prev[:, 0] * pw / w).astype(int), 0, pw - 1)
            v = np.clip((pix_prev[:, 1] * ph / h).astype(int), 0, ph - 1)
            depths = pts[v, u, 2].copy()
            print('depth valid', int(np.isfinite(depths).sum()), '/', len(depths),
                  'Z med', float(np.nanmedian(depths)) if np.isfinite(depths).any() else None)
            pts0 = pts.copy()
            gray_prev = gray
            continue
        # 预测落点
        pred = np.zeros_like(pix_prev)
        for i in range(len(pix_prev)):
            if i not in tracks or not np.isfinite(depths[i]):
                pred[i] = pix_prev[i]
                continue
            pred[i] = predict_px(pix_prev[i, 0], pix_prev[i, 1], depths[i],
                                 float(rows[k]["d"] or 0.0),
                                 float(rows[k]["s"] or 0.0),
                                 float(rows[k]["psi"] or 0.0), pb)
        p1, st, _ = cv2.calcOpticalFlowPyrLK(
            gray_prev, gray, pix_prev.reshape(-1, 1, 2).astype(np.float32),
            pred.reshape(-1, 1, 2).astype(np.float32),
            winSize=(31, 31), maxLevel=2,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.01),
            flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
        p0b, stb, _ = cv2.calcOpticalFlowPyrLK(
            gray, gray_prev, p1, pix_prev.reshape(-1, 1, 2).astype(np.float32),
            winSize=(31, 31), maxLevel=2,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.01),
            flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
        fb = np.linalg.norm((p0b - pix_prev.reshape(-1, 1, 2)).reshape(-1, 2),
                            axis=1)
        p1r = p1.reshape(-1, 2)
        u = np.clip((p1r[:, 0] * pw / w).astype(int), 0, pw - 1)
        v = np.clip((p1r[:, 1] * ph / h).astype(int), 0, ph - 1)
        znew = pts[v, u, 2]
        for i in range(len(pix_prev)):
            ok = (st.ravel()[i] == 1 and stb.ravel()[i] == 1 and fb[i] < 2.0
                  and 2.0 < znew[i] < 45.0)
            if ok and i in tracks:
                tracks[i].append((k, float(p1r[i, 0]), float(p1r[i, 1])))
                pix_prev[i] = p1r[i]
                depths[i] = float(znew[i])
            else:
                tracks.pop(i, None)
        gray_prev = gray
        if k % 10 == 0:
            print('k', k, 'alive', len(tracks), 'st', int(st.sum()),
                  'fb med', float(np.median(fb)), 'znew med',
                  float(np.nanmedian(znew)))

    # 三角化
    out = []
    for i, obs in tracks.items():
        if len(obs) < 12:
            continue
        P = triangulate(obs, poses, pb)
        if P is None or P[2] <= 0 or P[2] > 80:
            continue
        errs = []
        for t, u, v in obs:
            pu, pv = project(P, poses[t], pb)
            errs.append(float(np.hypot(pu - u, pv - v)))
        # 与首帧 MoGe 深度对照
        _, u0, v0 = obs[0]
        uu = int(np.clip(u0 * pw / w, 0, pw - 1))
        vv = int(np.clip(v0 * ph / h, 0, ph - 1))
        zm = float(pts0[vv, uu, 2])
        # 首帧相机系深度
        x0, z0, psi0 = poses[0]
        c, s = np.cos(psi0), np.sin(psi0)
        dx, dz = P[0] - x0, P[2] - z0
        bx = c * dx - s * dz
        bz = s * dx + c * dz
        out.append({"n": len(obs), "X": round(float(P[0]), 2),
                    "Y": round(float(P[1]), 2), "Z": round(float(P[2]), 2),
                    "bz0": round(float(bz), 2),
                    "z_moge": None if not np.isfinite(zm) else round(zm, 2),
                    "rep_med": round(float(np.median(errs)), 2),
                    "rep_p90": round(float(np.percentile(errs, 90)), 2)})

    path = OUT / args.video / "triangulate.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    if out:
        rep = np.array([o["rep_med"] for o in out])
        pairs = [(o["bz0"], o["z_moge"]) for o in out
                 if o["z_moge"] is not None and o["z_moge"] > 0]
        print(f"{args.video} {len(files)} 帧：{len(out)} 条三角化轨迹 "
              f"(>=12 帧)，重投影中位 {np.median(rep):.2f}px "
              f"P90 {np.percentile(rep,90):.2f}px")
        if pairs:
            a = np.array([p[0] for p in pairs])
            b = np.array([p[1] for p in pairs])
            rel = np.abs(a - b) / np.maximum(b, 1e-6)
            print(f"  与 MoGe 首帧深度对照：n={len(pairs)} "
                  f"相对误差中位 {np.median(rel)*100:.1f}% "
                  f"P90 {np.percentile(rel,90)*100:.1f}%")
        # 俯视图：三角化点 vs MoGe 单帧点
        W, H = 800, 600
        img = np.zeros((H, W, 3), np.uint8)
        for o in out:
            c = int((o["X"] + 16) / 32 * (W - 1))
            r = int((60 - o["Z"]) / 60 * (H - 1))
            if 0 <= c < W and 0 <= r < H:
                col = (60, 200, 255) if o["Y"] < 1.0 else (40, 40, 230)
                cv2.circle(img, (c, r), 2, col, -1)
        cv2.putText(img, "triangulated side features (top view)",
                    (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200),
                    1, cv2.LINE_AA)
        cv2.imwrite(str(OUT / args.video / "triangulate.png"), img)


if __name__ == "__main__":
    main()
