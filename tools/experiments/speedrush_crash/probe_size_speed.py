# -*- coding: utf-8 -*-
"""离线影子：60fps 录像上的框尺寸—距离关系与真实接近速度（撞车复盘实验 6）。

只读录像帧，跑生产 YOLO + MoGe 取「框几何 + 框底纵深」，不接控制。
回答两个问题：
1. 未被遮挡的车框，高度/面积与 MoGe 距离的关系有多紧（能否当独立标尺）；
2. 60fps 帧间差分得到的真实接近速度分布，有多少超过跳变门的 45 m/s 上界。

输出（数据目录 crash_size/<video>_<start>_<limit>.jsonl）：
  frame, cls, cx, cy, w, h, conf, z, fx, fy
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

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT = DATA / "crash_size"
CAM_W, CAM_H = 1280, 720


def det_z(pts, det):
    h, w = pts.shape[:2]
    u = int(round(det.cx * w / CAM_W))
    v = int(round(min(CAM_H - 2, det.cy + det.h // 2 - 2) * h / CAM_H))
    win = pts[max(0, v - 2):v + 3, max(0, u - 2):u + 3, 2]
    win = win[np.isfinite(win)]
    return None if win.size == 0 else float(np.median(win))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="video_20261021_160547_p1")
    ap.add_argument("--start", type=int, default=400)
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--tokens", type=int, default=1032)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    files = sorted((DEMOS / args.video).glob("*.jpg"))[args.start:args.start + args.limit]
    out = Path(args.out) if args.out else OUT / f"{args.video}_{args.start}_{args.limit}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    per = StreetPerception(str(PERCEPTION_MODEL_FILE))
    sess = dg.load_session(DEPTH_MODEL_FILE)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, path in enumerate(files):
            bgr = cv2.imread(str(path))
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            pres = per.detect(rgb, frame_id=args.start + i, ts_ns=0)
            blob = dg.moge_post.preprocess(rgb, dg.MOGE_IN_W, dg.MOGE_IN_H)
            points, mask, mscale = dg.moge_post.forward(sess, blob, args.tokens)
            res = dg.moge_post.reconstruct(points, mask, mscale)
            pts = np.array(res["pts"], copy=True)
            pts[~res["valid"]] = np.nan
            fx = res["fx"] * dg.MOGE_IN_W
            fy = res["fy"] * dg.MOGE_IN_H
            for kind, lst in (("car", pres.cars), ("coin", pres.coins),
                              ("bonus", pres.bonuses)):
                for d in lst:
                    z = det_z(pts, d)
                    fh.write(json.dumps({
                        "frame": args.start + i, "cls": kind,
                        "cx": d.cx, "cy": d.cy, "w": d.w, "h": d.h,
                        "conf": round(d.conf, 3),
                        "z": None if z is None else round(z, 2),
                        "fx": round(fx, 1), "fy": round(fy, 1),
                    }) + "\n")
                    n += 1
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(files)} 帧，{n} 个框", flush=True)
    print(f"完成：{len(files)} 帧，{n} 个框 → {out}")


if __name__ == "__main__":
    main()
