# -*- coding: utf-8 -*-
"""地面平面距离 vs MoGe 距离（C 类实验，只读人工局帧）。

对每个 YOLO 车框的底点：
  平面式：Z = K/(y2 - y_h)（K 由地面平面标定一次性确定）
  MoGe：框底点附近点图的 Z 中位
比较两者误差，判断地面平面能否替代逐帧深度。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_ground_vs_moge.py \
        --sess manual_20261019_173047_p1 --start 100 --limit 100
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
K = 1101.0          # 地面平面常数（probe_ground_plane 标定中位）


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sess", default="manual_20261019_173047_p1")
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--limit", type=int, default=100)
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

    rows = []
    for k, name in enumerate(files):
        img = cv2.imread(str(frame_dir / name))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        per = perc.detect(rgb, frame_id=k, ts_ns=k)
        _, evid = depth.observe_debug(
            rgb, _yolo_object_mask(per), detections=(), fid=k, frame_ts_ns=k)
        if evid is None:
            continue
        pts = evid["pts"]
        ph, pw = pts.shape[:2]
        H, W = img.shape[:2]
        for d in per.cars:
            y2 = d.cy + d.h / 2.0
            if y2 <= cal.y_h + cal.min_denom or y2 >= H * 0.99:
                continue
            z_plane = K / (y2 - cal.y_h)
            if not (4.0 < z_plane < 60.0):
                continue
            gu = int(np.clip(d.cx * pw / W, 0, pw - 1))
            gv = int(np.clip(y2 * ph / H, 0, ph - 1))
            patch = pts[max(0, gv - 3):gv + 4, max(0, gu - 3):gu + 4, 2]
            zz = patch[np.isfinite(patch) & (patch > 0)]
            if zz.size < 3:
                continue
            z_moge = float(np.median(zz))
            rows.append((z_plane, z_moge, d.conf, d.h))

    if not rows:
        print("无有效样本")
        return
    err = [abs(zp - zm) for zp, zm, _, _ in rows]
    rel = [abs(zp - zm) / zm for zp, zm, _, _ in rows]
    print(f"{args.sess}: 样本 {len(rows)}（车框底点）")
    print(f"  距离误差 中位 {np.median(err):.2f}m，P90 {np.percentile(err, 90):.2f}m")
    print(f"  相对误差 中位 {np.median(rel):.1%}，P90 {np.percentile(rel, 90):.1%}")
    for lo, hi in ((4, 10), (10, 20), (20, 40), (40, 60)):
        s = [(e, r) for (zp, zm, _, _), e, r in zip(rows, err, rel)
             if lo <= zm < hi]
        if len(s) >= 5:
            print(f"  MoGe {lo}-{hi}m: n={len(s)} 误差中位 "
                  f"{statistics.median([x[0] for x in s]):.2f}m，"
                  f"相对 {statistics.median([x[1] for x in s]):.1%}")


if __name__ == "__main__":
    main()
