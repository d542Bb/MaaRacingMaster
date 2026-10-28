"""锚点清单帧重放（2026-10-22，锚点归因的实证工具）。

20Hz raw 帧就是深度 worker 的输入帧：消费拍 `fid` 的清单来自
`sheet_fid = fid - asheet.fid_lag`，该帧一定在 raw 落盘序列里
（12 局 3089 张清单帧 100% 命中）。本探针在指定 session/sheet_fid 上
离线重跑 YOLO + MoGe，打印每个车框：

- 原始框与框内点云的 Z 分位/直方图峰（看近簇到底在哪一段）；
- `_box_anchor_z` 复算的锚值（与在线 trace 对照，验证重放保真度）；
- 两两框的像素重叠（排除“串到旁边近车”的简单解释）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_sheet_replay.py \
        20261022_140508_p2 12064
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
    DepthRoadObserver, _box_anchor_z, load_session)
from maaracing_master.plugins.speedrush.module import _yolo_object_mask  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402


def _root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("需要 APPDATA（Windows 运行数据目录）")
    return Path(base) / "MaaRacingMaster" / "debug" / "speedrush"


def _box(d) -> tuple[int, int, int, int]:
    return (int(d.cx - d.w / 2), int(d.cy - d.h / 2),
            int(d.cx + d.w / 2), int(d.cy + d.h / 2))


def _overlap(a, b) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("fid", type=int)
    args = ap.parse_args()
    base = _root() / args.session
    rows = [json.loads(line) for line in
            (base / f"trace_{args.session}.jsonl").open(encoding="utf-8")]
    idx = {r["fid"]: i for i, r in enumerate(rows)}
    if args.fid not in idx:
        raise SystemExit(f"{args.session} 没有 fid {args.fid}")
    row = rows[idx[args.fid]]
    img_path = base / "raw" / f"{idx[args.fid] + 1:04d}_raw.jpg"
    img = cv2.imread(str(img_path))
    if img is None:
        raise SystemExit(f"找不到 {img_path}")
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    cal = load_calib()
    per = StreetPerception(str(PERCEPTION_MODEL_FILE)).detect(
        rgb, frame_id=args.fid, ts_ns=0)
    mask = _yolo_object_mask(per)
    depth = DepthRoadObserver(load_session(DEPTH_MODEL_FILE), cal)
    res = depth.observe_debug(rgb, mask, detections=(), fid=args.fid,
                              frame_ts_ns=0)
    evid = res[1] if isinstance(res, tuple) and len(res) > 1 else None
    if not evid or "pts" not in evid:
        print("深度重放无点云")
        return 1
    pts = evid["pts"]
    gh, gw = pts.shape[:2]
    print(f"== {args.session} fid {args.fid} row {idx[args.fid] + 1}"
          f" | raw {img_path.name} | pts {pts.shape} fx={evid.get('fx')}")
    print("  在线 trace（本帧）："
          f" car_h={row.get('car_h')} car_pos={row.get('car_pos')}")
    boxes = [_box(d) for d in per.cars]
    for i, (d, box) in enumerate(zip(per.cars, boxes)):
        az = _box_anchor_z(pts, box)
        xi0 = max(0, int(box[0] * gw / 1280))
        yi0 = max(0, int(box[1] * gh / 720))
        xi1 = min(gw, int(box[2] * gw / 1280))
        yi1 = min(gh, int(box[3] * gh / 720))
        zv = pts[yi0:yi1, xi0:xi1, 2]
        zv = zv[np.isfinite(zv)]
        if zv.size:
            q = np.percentile(zv, [5, 25, 50, 75, 95])
            lo, hi = np.percentile(zv, [2, 98])
            hist, edges = np.histogram(zv, bins=24, range=(lo, hi))
            peaks = np.argsort(hist)[-3:][::-1]
            pk = [(round(0.5 * (edges[j] + edges[j + 1]), 2), int(hist[j]))
                  for j in peaks]
        else:
            q, pk = [], []
        print(f"  det{i} cx={d.cx} cy={d.cy} w={d.w} h={d.h}"
              f" conf={d.conf:.2f} box={box} 锚={az}"
              f" n={zv.size} q={None if not len(q) else np.round(q, 2)}"
              f" 峰={pk}")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ov = _overlap(boxes[i], boxes[j])
            print(f"  overlap det{i}/det{j} = {ov}px²")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
