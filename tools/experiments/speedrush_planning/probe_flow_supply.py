# -*- coding: utf-8 -*-
"""光流供数体检（C 类实验，只读人工局帧）。

先不估速度，只回答四个问题：
  1. 稠密光流的前向-后向误差多大；
  2. 特征点/采样点落在静态场景（护栏、路面）还是移动车辆；
  3. 这些点上的深度是否有效、距离范围；
  4. 像素坐标、焦距、帧时间戳能否对上。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_flow_supply.py \
        --sess manual_20261019_173047_p1 --start 100 --limit 20
输出：统计打印 + 数据目录下的 flow_supply_*.jpg 可视化。
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
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DepthRoadObserver, load_session)
from maaracing_master.plugins.speedrush.module import (  # noqa: E402
    _yolo_object_mask)
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"


def sample_grid(mask, step=14):
    ys, xs = np.mgrid[0:mask.shape[0]:step, 0:mask.shape[1]:step]
    ok = mask[ys, xs] > 0
    return xs[ok], ys[ok]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sess", default="manual_20261019_173047_p1")
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--fps", type=float, default=60.0)
    args = ap.parse_args()

    sess = DEMOS / args.sess
    frame_dir = sess / "frames" if (sess / "frames").exists() else sess
    if (sess / "frames.jsonl").exists():
        idx = [json.loads(x) for x in
               (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
               if x.strip()][args.start:args.start + args.limit]
    else:
        # 纯视频帧目录（60fps 抽帧）：无时间戳/手柄，按帧率换算
        files = sorted(sess.glob("*.jpg"))[args.start:args.start + args.limit]
        idx = [{"file": f.name, "ts_ns": int(k * 1e9 / args.fps),
                "frame_id": k} for k, f in enumerate(files)]
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    depth = DepthRoadObserver(load_session(DEPTH_MODEL_FILE), load_calib())
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)

    prev_gray = prev_evid = prev_ts = None
    stats = []
    saved = 0
    for k, item in enumerate(idx):
        img = cv2.imread(str(frame_dir / item["file"]))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        per = perc.detect(rgb, frame_id=item["frame_id"], ts_ns=item["ts_ns"])
        _, evid = depth.observe_debug(
            rgb, _yolo_object_mask(per), detections=(),
            fid=item["frame_id"], frame_ts_ns=item["ts_ns"])
        if prev_gray is not None and evid is not None and prev_evid is not None:
            dt = (item["ts_ns"] - prev_ts) / 1e9
            flow = dis.calc(prev_gray, gray, None)
            flow_b = dis.calc(gray, prev_gray, None)
            h, w = gray.shape
            # 采样区域：左右护栏带 + 路面近场
            mask = np.zeros((h, w), np.uint8)
            mask[int(h * 0.24):int(h * 0.70), :int(w * 0.34)] = 255
            mask[int(h * 0.24):int(h * 0.70), int(w * 0.66):] = 255
            mask[int(h * 0.56):int(h * 0.86), int(w * 0.22):int(w * 0.78)] = 255
            xs, ys = sample_grid(mask)
            u = flow[ys, xs, 0]
            v = flow[ys, xs, 1]
            # 反向流（最近邻回采）
            xb = np.clip((xs + u).round().astype(int), 0, w - 1)
            yb = np.clip((ys + v).round().astype(int), 0, h - 1)
            ub = flow_b[yb, xb, 0]
            vb = flow_b[yb, xb, 1]
            fb = np.hypot(u + ub, v + vb)
            ph, pw = prev_evid["pts"].shape[:2]
            gu = np.clip((xs * pw / w).astype(int), 0, pw - 1)
            gv = np.clip((ys * ph / h).astype(int), 0, ph - 1)
            Z = prev_evid["pts"][gv, gu, 2]
            zok = np.isfinite(Z) & (Z > 3) & (Z < 60)
            fx = float(prev_evid["fx"]) * w
            stats.append({
                "n": int(len(xs)), "z_ok": int(zok.sum()),
                "fb_med": float(np.median(fb)), "fb_p90": float(np.percentile(fb, 90)),
                "flow_med": float(np.median(np.hypot(u, v))),
                "dt": dt, "fx": fx, "z_med": float(np.median(Z[zok])) if zok.any() else None,
            })
            if saved < 2 and k in (4, len(idx) - 1):
                hsv = np.zeros((h, w, 3), np.uint8)
                mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
                hsv[..., 0] = ang * 180 / np.pi / 2
                hsv[..., 1] = 255
                hsv[..., 2] = np.clip(mag * 20, 0, 255).astype(np.uint8)
                vis = cv2.addWeighted(img, 0.55, cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), 0.45, 0)
                for x, y, uu, vv in zip(xs[::6], ys[::6], u[::6], v[::6]):
                    cv2.arrowedLine(vis, (int(x), int(y)),
                                    (int(x + uu * 3), int(y + vv * 3)),
                                    (0, 0, 255), 1, tipLength=0.3)
                out = DATA / f"flow_supply_{sess.name}_{saved}.jpg"
                cv2.imwrite(str(out), vis)
                print("saved", out)
                saved += 1
        prev_gray, prev_evid, prev_ts = gray, evid, item["ts_ns"]

    if stats:
        print(f"\n{args.sess} 供数体检（{len(stats)} 对帧）")
        print(f"  采样点中位 {np.median([s['n'] for s in stats]):.0f}，"
              f"深度有效比例中位 {np.median([s['z_ok']/s['n'] for s in stats]):.0%}")
        print(f"  前向-后向误差 中位 {np.median([s['fb_med'] for s in stats]):.2f}px，"
              f"P90 中位 {np.median([s['fb_p90'] for s in stats]):.2f}px")
        print(f"  光流大小中位 {np.median([s['flow_med'] for s in stats]):.2f}px，"
              f"dt 中位 {np.median([s['dt'] for s in stats])*1000:.0f}ms，"
              f"fx {np.median([s['fx'] for s in stats]):.1f}px，"
              f"Z 中位 {np.median([s['z_med'] for s in stats if s['z_med']]):.1f}m")


if __name__ == "__main__":
    main()
