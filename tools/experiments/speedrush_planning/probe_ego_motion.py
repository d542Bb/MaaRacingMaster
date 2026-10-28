# -*- coding: utf-8 -*-
"""自车横向运动反推探针（C 类实验，只读人工局帧）。

用静态场景（路面/墙）的光流 + 深度点图估计自车横向速度与横摆角速度：
  du ≈ fx·(v_y·dt/Z + ω·dt)
近场不同 Z 的点给出不同方程，最小二乘解 (v_y, ω)。与人工杆值/积分模型对照，
判断能否成为快回路的自车状态观测。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_ego_motion.py \
        --sess manual_20261019_173047_p1 --start 0 --limit 300 --step 2
输出（数据目录）: ego_motion_<sess>.jsonl
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

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DepthRoadObserver, load_session)
from maaracing_master.plugins.speedrush.module import (  # noqa: E402
    _yolo_object_mask)
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
Z_LO, Z_HI = 6.0, 30.0
MAX_CORNERS = 400


def fit_motion(du, z, fx, dt):
    """du*Z/fx = v_y*dt + omega*dt*Z，鲁棒一次（去 3σ 外点）。"""
    y = du * z / fx
    A = np.stack([np.ones_like(z), z], axis=1)
    w = np.ones_like(z, dtype=bool)
    for _ in range(3):
        coef, *_ = np.linalg.lstsq(A[w], y[w], rcond=None)
        resid = y - A @ coef
        s = resid[w].std()
        if s < 1e-9:
            break
        nw = np.abs(resid - resid[w].mean()) < 3.0 * s
        if nw.sum() < 8:
            break
        w = nw
    return float(coef[0] / dt), float(coef[1] / dt), int(w.sum())


def run(args) -> Path:
    sess = DEMOS / args.sess
    idx = [json.loads(x) for x in
           (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
           if x.strip()][args.start:args.start + args.limit]
    pads = [json.loads(x) for x in
            (sess / "pads.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    pad_ts = [p["ts_ns"] for p in pads]
    pad_lx = [p.get("lx", 0) for p in pads]

    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    depth = DepthRoadObserver(load_session(DEPTH_MODEL_FILE), load_calib())
    out_path = DATA / f"ego_motion_{sess.name}.jsonl"

    gray_prev = None
    evid_prev = None
    prev_ts = None
    rows = []
    t0 = time.perf_counter()
    with out_path.open("w", encoding="utf-8") as f:
        for k in range(0, len(idx), args.step):
            item = idx[k]
            img = cv2.imread(str(sess / "frames" / item["file"]))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            per = perc.detect(rgb, frame_id=item["frame_id"],
                              ts_ns=item["ts_ns"])
            _, evid = depth.observe_debug(
                rgb, _yolo_object_mask(per), detections=(),
                fid=item["frame_id"], frame_ts_ns=item["ts_ns"])
            row = {"fid": item["frame_id"], "ts": item["ts_ns"],
                   "v_y": None, "omega": None, "n": 0}
            if gray_prev is not None and evid is not None \
                    and evid_prev is not None:
                dt = (item["ts_ns"] - prev_ts) / 1e9
                if 0.05 < dt < 0.2:
                    # 只看两侧护栏（静态、纹理独特），避开路面重复纹理与车辆
                    mask = np.zeros_like(gray)
                    mask[int(gray.shape[0] * 0.25):int(gray.shape[0] * 0.72), :] = 255
                    mask[:, int(gray.shape[1] * 0.36):int(gray.shape[1] * 0.64)] = 0
                    mask[:int(gray.shape[0] * 0.22), : int(gray.shape[1] * 0.30)] = 0
                    mask[:int(gray.shape[0] * 0.22), int(gray.shape[1] * 0.70):] = 0
                    p0 = cv2.goodFeaturesToTrack(
                        gray_prev, MAX_CORNERS, 0.01, 12, mask=mask)
                    if p0 is not None and len(p0) >= 10:
                        p1, st, _ = cv2.calcOpticalFlowPyrLK(
                            gray_prev, gray, p0, None,
                            winSize=(31, 31), maxLevel=3)
                        p0b, stb, _ = cv2.calcOpticalFlowPyrLK(
                            gray, gray_prev, p1, None,
                            winSize=(31, 31), maxLevel=3)
                        fb = np.linalg.norm(
                            (p0b - p0).reshape(-1, 2), axis=1)
                        good = (st.ravel() == 1) & (stb.ravel() == 1)                             & (fb < 1.0)
                        a = p0[good].reshape(-1, 2)
                        b = p1[good].reshape(-1, 2)
                        pts = evid_prev["pts"]
                        h, w = pts.shape[:2]
                        gu = np.clip((a[:, 0] * w / gray.shape[1]).astype(int), 0, w - 1)
                        gv = np.clip((a[:, 1] * h / gray.shape[0]).astype(int), 0, h - 1)
                        X = pts[gv, gu, 0]
                        Z = pts[gv, gu, 2]
                        ok = np.isfinite(X) & np.isfinite(Z) \
                            & (Z > Z_LO) & (Z < Z_HI)
                        if ok.sum() >= 10:
                            fx = float(evid_prev["fx"]) * gray.shape[1]
                            du = b[ok, 0] - a[ok, 0]
                            v_y, omega, n = fit_motion(du, Z[ok], fx, dt)
                            row["v_y"] = round(v_y, 3)
                            row["omega"] = round(np.degrees(omega), 2)
                            row["n"] = n
            # 人工杆值（最近 200Hz 采样）
            import bisect
            i = bisect.bisect_left(pad_ts, item["ts_ns"])
            row["lx"] = pad_lx[min(i, len(pad_lx) - 1)]
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            gray_prev = gray
            evid_prev = evid
            prev_ts = item["ts_ns"]
    el = time.perf_counter() - t0
    ok = [r for r in rows if r["v_y"] is not None]
    print(f"{sess.name}: {len(rows)} 拍，有效 {len(ok)}，{el:.1f}s → {out_path}")
    if ok:
        vy = [r["v_y"] for r in ok]
        om = [r["omega"] for r in ok]
        print(f"  v_y 中位 {np.median(vy):+.2f} m/s，10~90% "
              f"{np.percentile(vy, 10):+.2f}~{np.percentile(vy, 90):+.2f}")
        print(f"  omega 中位 {np.median(om):+.2f} °/s，10~90% "
              f"{np.percentile(om, 10):+.2f}~{np.percentile(om, 90):+.2f}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="自车横向运动反推")
    ap.add_argument("--sess", default="manual_20261019_173047_p1")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--step", type=int, default=2)
    run(ap.parse_args())


if __name__ == "__main__":
    main()
