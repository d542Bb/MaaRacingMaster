# -*- coding: utf-8 -*-
"""地面平面常数验证（C 类实验，只读人工局帧）。

针孔模型下，地面点满足 Z = K/(v - y_h)，K = 焦距 × 相机高度。
用 MoGe 点图里 Y≈相机高度的点拟合每帧 K：
  K 稳定 → 固定 K 即可从 YOLO 框底点算米制距离，不必每帧跑深度。
  K 漂移 → 坡度/俯仰/尺度不稳，MoGe 仍需保留做慢校准。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_ground_plane.py \
        --sess manual_20261019_173047_p1 --start 100 --limit 50
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sess", default="manual_20261019_173047_p1")
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    sess = DEMOS / args.sess
    frame_dir = sess / "frames" if (sess / "frames").exists() else sess
    files = ([json.loads(x)["file"] for x in
              (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
              if x.strip()] if (sess / "frames.jsonl").exists()
             else [p.name for p in sorted(sess.glob("*.jpg"))])
    files = files[args.start:args.start + args.limit]

    cal = load_calib()
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    depth = DepthRoadObserver(load_session(DEPTH_MODEL_FILE), cal)
    ground_y = 1.92          # 相机离地高度（点图 Y 轴正方向朝下）

    ks = []
    none_n = 0
    for k, name in enumerate(files):
        img = cv2.imread(str(frame_dir / name))
        if img is None:
            print(f"  frame {k}: img None ({frame_dir / name})")
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        per = perc.detect(rgb, frame_id=k, ts_ns=k)
        _, evid = depth.observe_debug(
            rgb, _yolo_object_mask(per), detections=(), fid=k, frame_ts_ns=k)
        if evid is None:
            none_n += 1
            continue
        pts = evid["pts"]
        h, w = pts.shape[:2]
        X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
        vv, uu = np.mgrid[0:h, 0:w]
        v_full = vv * (img.shape[0] / h)
        # 相机有俯仰，地面点的 Y 随 Z 线性变化，不能按常数高度筛；
        # 用图像下半 + 距离域粗选，靠中位抗住车/墙等非地面点。
        m = np.isfinite(Z) & (Z > 6.0) & (Z < 40.0) \
            & (v_full > img.shape[0] * 0.52) \
            & (v_full > cal.y_h + cal.min_denom) \
            & (v_full < img.shape[0] * 0.98)
        if m.sum() < 200:
            continue
        kk = Z[m] * (v_full[m] - cal.y_h)
        ks.append((k, float(np.median(kk)), float(np.percentile(kk, 25)),
                   float(np.percentile(kk, 75)), int(m.sum())))

    print(f"  evid=None: {none_n}/{len(files)}")
    if not ks:
        print("无有效帧")
        return
    med = [x[1] for x in ks]
    print(f"{args.sess}: {len(ks)} 帧，地面点中位 {np.median([x[4] for x in ks]):.0f}")
    print(f"  K 中位 {np.median(med):.0f}，跨帧 10~90% "
          f"{np.percentile(med, 10):.0f}~{np.percentile(med, 90):.0f}，"
          f"变异 {np.std(med)/np.mean(med):.1%}")
    print(f"  帧内四分位宽中位 {np.median([x[3]-x[2] for x in ks]):.0f}"
          f"（越小=地面越像平面）")
    print(f"  fx×h 参考值见标定：fx≈530~670 × 1.92 ≈ 1018~1286")


if __name__ == "__main__":
    main()
