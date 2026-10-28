# -*- coding: utf-8 -*-
"""几何链 vs 深度链的交叉验证（第一批：速度 + 目标距离 + 延迟）。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_geom/probe_crossval.py \
        --video video_20261021_160547_p1 --start 400 --limit 300
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
OUT = DATA / "geom"
CAM_W, CAM_H = 1280, 720


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="video_20261021_160547_p1")
    ap.add_argument("--start", type=int, default=400)
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()

    geom_path = OUT / args.video / "geom.jsonl"
    depth_path = DATA / "3d_fusion" / args.video / "metrics_full.jsonl"
    g = [json.loads(x) for x in geom_path.read_text(encoding="utf-8").splitlines()
         if x.strip()]
    d = [json.loads(x) for x in depth_path.read_text(encoding="utf-8").splitlines()
         if x.strip()]
    # 深度链 metrics_full 从绝对帧 300 起；几何链从 args.start 起
    dep = {300 + r["k"]: r for r in d}

    pairs = []
    for r in g:
        if not r.get("d_ok"):
            continue
        ab = args.start + r["k"]
        dr = dep.get(ab)
        if dr is None or dr.get("d") is None or dr.get("src") == "prior":
            continue
        pairs.append((ab, r["d"], dr["d"], r["resp"], dr["src"]))
    if not pairs:
        print("没有可对齐的有效速度帧")
        return
    a = np.array([p[1] for p in pairs])
    b = np.array([p[2] for p in pairs])
    ratio = a / np.maximum(b, 1e-6)
    corr = float(np.corrcoef(a, b)[0, 1]) if len(a) > 2 else float("nan")
    print(f"速度交叉验证：{len(pairs)} 对有效帧")
    print(f"  几何中位 {np.median(a):.3f} m/帧；深度中位 {np.median(b):.3f} m/帧")
    print(f"  比值中位 {np.median(ratio):.3f} "
          f"10-90% {np.percentile(ratio,10):.3f}~{np.percentile(ratio,90):.3f}")
    print(f"  相关系数 {corr:.3f}")

    # 目标距离：几何框底点 vs MoGe 同像素 Z
    sess = load_session(DEPTH_MODEL_FILE)
    files = sorted((DEMOS / args.video).glob("*.jpg"))[args.start:args.start + args.limit]
    rows = []
    for idx in range(0, len(g), 15):
        r = g[idx]
        tg = [t for t in (r.get("targets") or []) if t["kind"] == "car"]
        if not tg:
            continue
        f = files[r["k"]]
        img = cv2.imread(str(f))
        pts, _, _, ev = infer_points(
            sess, cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
            with_evidence=True, native=True)
        ph, pw = pts.shape[:2]
        for t in tg:
            u = int(np.clip(t["u"] * pw / CAM_W, 0, pw - 1))
            v = int(np.clip(t["v"] * ph / CAM_H, 0, ph - 1))
            zm = float(pts[v, u, 2])
            if not np.isfinite(zm) or zm <= 0:
                continue
            rows.append((t["z_ground"], zm))
    if rows:
        z1 = np.array([r[0] for r in rows])
        z2 = np.array([r[1] for r in rows])
        rel = np.abs(z1 - z2) / np.maximum(z2, 1e-6)
        print(f"目标距离交叉验证：{len(rows)} 个车框")
        for lo, hi in ((0, 15), (15, 30), (30, 60)):
            m = (z2 >= lo) & (z2 < hi)
            if m.sum():
                print(f"  MoGe {lo}-{hi}m: n={int(m.sum())} "
                      f"相对误差中位 {np.median(rel[m])*100:.1f}% "
                      f"P90 {np.percentile(rel[m],90)*100:.1f}%")
    else:
        print("目标距离：没有可用车框")

    lat = [r["total_ms"] for r in g if r.get("total_ms")]
    print(f"几何链延迟：中位 {np.median(lat):.1f} ms，"
          f"P90 {np.percentile(lat,90):.1f} ms")


if __name__ == "__main__":
    main()
