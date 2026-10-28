"""框内深度簇对照（A/B/C，2026-10-22）。

在已复现的四张清单帧上重跑 YOLO+MoGe，对每个车框回答：

- A 现有基线：`_box_anchor_z` 的近簇选择；
- B 框内簇比较：24bin 直方图的前几个峰，主体簇是否存在、支持度多少；
- C 框底几何：框底 25% 条带的 Z 中位 + 地面平面先验
  `K/(y2 - y_h)`（K=1101，probe_ground_plane 标定中位）；
- 判定：存在与几何先验同量级（0.5~2×）且支持度 ≥ 主体峰 30% 的簇 →
  “主体深度在点云里，选簇可修”；否则 → “整框偏近，需要深度质量门/换源”。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_box_clusters.py
"""
from __future__ import annotations

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

K_PLANE = 1101.0     # 地面平面常数（probe_ground_plane 标定中位）
H_NOM = 1.5
CASES = [
    ("20261022_140508_p2", 12064),
    ("20261022_140307_p2", 7460),
    ("20261022_140907_p2", 20694),
    ("20261022_140023_p1", 1678),
]


def _root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("需要 APPDATA（Windows 运行数据目录）")
    return Path(base) / "MaaRacingMaster" / "debug" / "speedrush"


def _bins(zv: np.ndarray) -> list[tuple[float, int]]:
    if zv.size < 10:
        return []
    lo, hi = np.percentile(zv, [2, 98])
    hist, edges = np.histogram(zv, bins=24, range=(lo, hi))
    out = [(0.5 * (edges[i] + edges[i + 1]), int(hist[i]))
           for i in range(len(hist)) if hist[i] > 0]
    return sorted(out, key=lambda x: -x[1])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cal = load_calib()
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    depth = DepthRoadObserver(load_session(DEPTH_MODEL_FILE), cal)
    for sess, fid in CASES:
        base = _root() / sess
        rows = [json.loads(x) for x in
                (base / f"trace_{sess}.jsonl").open(encoding="utf-8")]
        idx = {r["fid"]: i for i, r in enumerate(rows)}
        row = rows[idx[fid]]
        img = cv2.imread(str(base / "raw" / f"{idx[fid] + 1:04d}_raw.jpg"))
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        per = perc.detect(rgb, frame_id=fid, ts_ns=0)
        mask = _yolo_object_mask(per)
        res = depth.observe_debug(rgb, mask, detections=(), fid=fid,
                                  frame_ts_ns=0)
        evid = res[1] if isinstance(res, tuple) and len(res) > 1 else None
        if not evid or "pts" not in evid:
            print(f"== {sess} fid {fid}: 无点云")
            continue
        pts = evid["pts"]
        gh, gw = pts.shape[:2]
        print(f"== {sess} fid {fid} | 在线 car_h="
              f"{[(c['id'], c['h']) for c in (row.get('car_h') or [])]}")
        for i, d in enumerate(per.cars):
            x0, y0 = int(d.cx - d.w / 2), int(d.cy - d.h / 2)
            x1, y1 = int(d.cx + d.w / 2), int(d.cy + d.h / 2)
            box = (x0, y0, x1, y1)
            A = _box_anchor_z(pts, box)
            focal = row.get('dgeo_focal') or 663.0
            z_box = float(focal) * H_NOM / max(d.h, 1)
            y2 = y1
            z_plane = (K_PLANE / (y2 - cal.y_h)
                       if y2 - cal.y_h > cal.min_denom else None)
            xi0 = max(0, int(x0 * gw / 1280))
            yi0 = max(0, int(y0 * gh / 720))
            xi1 = min(gw, int(x1 * gw / 1280))
            yi1 = min(gh, int(y1 * gh / 720))
            zv = pts[yi0:yi1, xi0:xi1, 2]
            zv = zv[np.isfinite(zv)]
            bins = _bins(zv)
            # 框底 25% 条带
            yb = max(yi0, yi1 - max(1, int((yi1 - yi0) * 0.25)))
            zb = pts[yb:yi1, xi0:xi1, 2]
            zb = zb[np.isfinite(zb)]
            z_bottom = float(np.median(zb)) if zb.size else None
            refs = [z for z in (z_plane, z_box) if z is not None]
            body = None
            if bins and refs:
                lo = 0.5 * min(refs)
                hi = 2.0 * max(refs)
                cand = [b for b in bins if lo <= b[0] <= hi]
                if cand:
                    body = max(cand, key=lambda x: x[1])
            support = (body[1] / bins[0][1]) if body and bins else 0.0
            verdict = ("主体簇存在" if body and support >= 0.30
                       else "整框偏近")
            print(f"  det{i} cx={d.cx} cy={d.cy} w={d.w} h={d.h}"
                  f" box={box} n={zv.size}")
            print(f"    A={None if A is None else round(A, 2)}"
                  f" 框证={z_box:.1f} 平面={None if z_plane is None else round(z_plane, 1)}"
                  f" 底带={None if z_bottom is None else round(z_bottom, 2)}")
            print("    B 峰="
                  + ", ".join(f"{round(z, 2)}m/{n}" for z, n in bins[:6]))
            print(f"    主体={None if body is None else (round(body[0], 2), body[1])}"
                  f" 支持比={support:.2f} → {verdict}")
            if bins and refs:
                main = bins[0]
                lo, hi = 0.5 * min(refs), 2.0 * max(refs)
                ok = lo <= main[0] <= hi
                print(f"    B选最密簇={round(main[0], 2)}m/{main[1]}"
                      f" → {'可采纳' if ok else '仍在几何带外，需拒绝/未知'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
