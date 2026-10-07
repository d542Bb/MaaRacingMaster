# -*- coding: utf-8 -*-
"""实体 Z 跨帧稳定性量化（全面转 3D 前置测量二，2026-10-07）。

**问题**：2D 口径的接近速率用像素行号（rel_approach，px/tick，稳但有透视偏差）；
3D 口径用实体 Z_med（米制无偏，但抖动未量化）。本探针在低运动窗内逐帧跑
MoGe+YOLO，按 IoU 关联同一实体跨帧，量 Z_med/X_med 的二阶差（源端噪声）与
一阶差（真实接近信号），回答「深度差分能否给出可用的接近速率」——判据 =
|中位 dZ|/帧 对 二阶差 P90 的信噪比。

实体化口径与 probe_point_classify 同款：框内有限点 → Z 直方图 24 bin 最密簇
→ 簇内中位（纯中位被框缘外天空/背景远点拖飞）。跨帧关联=同类贪心 IoU≥0.3。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_entity_z_jitter.py \
        --demo <dir> [--windows 3] [--winlen 16]
产物：<demo>/../depth_review/jitter/<demo名>_entity.json + 控制台摘要。
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import cv2
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision.probe_depth_jitter import (  # noqa: E402
    MOGE_ONNX, YOLO_ONNX, motion_scores, pick_windows, second_diff_stats)


def entity_z(pts: np.ndarray, box) -> tuple | None:
    """检测框 → (点数, Z_med, X_med, cy)。口径=probe_point_classify.stat。"""
    h, w = pts.shape[:2]
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    sub = pts[y0:y1, x0:x1]
    zf, xf = sub[..., 2], sub[..., 0]
    ok = np.isfinite(zf) & np.isfinite(xf)
    if int(ok.sum()) < 10:
        return None
    zv, xv = zf[ok], xf[ok]
    lo, hi = np.percentile(zv, [2, 98])
    hist, edges = np.histogram(zv, bins=24, range=(lo, hi))
    c = 0.5 * (edges[hist.argmax()] + edges[hist.argmax() + 1])
    core = np.abs(zv - c) < (hi - lo) / 24
    if int(core.sum()) < 10:
        core = np.ones(len(zv), bool)
    return (int(core.sum()), float(np.median(zv[core])),
            float(np.median(xv[core])), (y0 + y1) // 2)


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def run_window(sess, yolo, demo, rows, start: int, winlen: int) -> list[dict]:
    """一个窗：逐帧实体化 + IoU 贪心关联，返回轨迹列表（每轨迹逐帧量）。"""
    tracks: list[dict] = []      # {cls, box, obs: [(seq, z, x, cy)]}
    for i in range(start, start + winlen):
        r = rows[i]
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / r["file"])),
                           cv2.COLOR_BGR2RGB)
        pts, _fx, _fy = dg.infer_points(sess, img)
        _cls, dets, _raw = yolo(img)
        ents = []
        for d in dets:
            e = entity_z(pts, d["box"])
            if e is not None:
                ents.append((d["class_name"], d["box"], e))
        for t in tracks:
            t["_matched"] = False
        for cls, box, e in ents:
            best, best_iou = None, 0.3
            for t in tracks:
                if t["cls"] != cls or t["_matched"] or t["obs"][-1][0] != r["seq"] - 1:
                    continue
                v = iou(t["box"], box)
                if v > best_iou:
                    best, best_iou = t, v
            if best is not None:
                best["obs"].append((r["seq"], *e[1:]))
                best["box"] = box
                best["_matched"] = True
            else:
                tracks.append({"cls": cls, "box": box,
                               "obs": [(r["seq"], *e[1:])], "_matched": True})
    return [t for t in tracks if len(t["obs"]) >= 6]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--winlen", type=int, default=16)
    args = ap.parse_args()
    demo = Path(args.demo)
    t_start = time.time()

    scores, rows = motion_scores(demo)
    wins = pick_windows(scores, args.windows, args.winlen)
    print(f"[{time.time()-t_start:5.1f}s] {demo.name}: 选 {len(wins)} 窗"
          f"（运动分均值 {[round(float(scores[a:b].mean()),1) for a,b in wins]}）")

    sess = dg.load_session(MOGE_ONNX)
    yolo = YOLODetector(str(YOLO_ONNX), conf=0.35, iou=0.45)

    out = {"demo": demo.name, "winlen": args.winlen, "tracks": []}
    for wi, (a, b) in enumerate(wins):
        for t in run_window(sess, yolo, demo, rows, a, args.winlen):
            seqs = [o[0] for o in t["obs"]]
            zs = [o[1] for o in t["obs"]]
            xs = [o[2] for o in t["obs"]]
            cys = [float(o[3]) for o in t["obs"]]
            dz = [q - p for p, q in zip(zs, zs[1:])]
            dcy = [q - p for p, q in zip(cys, cys[1:])]
            rec = {
                "win": wi, "cls": t["cls"], "n": len(zs),
                "seq": [seqs[0], seqs[-1]],
                "z": {"range": [round(min(zs), 2), round(max(zs), 2)],
                      **second_diff_stats(zs)},
                "x": second_diff_stats(xs),
                "dz_med": round(statistics.median(dz), 3) if dz else None,
                "dcy_med": round(statistics.median(dcy), 1) if dcy else None,
            }
            out["tracks"].append(rec)
            zr = rec["z"]
            if zr.get("dd_p90") is not None:
                print(f"[{time.time()-t_start:5.1f}s]   窗{wi} {t['cls']:9s} "
                      f"n={rec['n']:2d} z∈[{zr['range'][0]:6.2f},{zr['range'][1]:6.2f}]m "
                      f"dZ/帧={rec['dz_med']:+.3f}m dcy/帧={rec['dcy_med']:+.0f}px "
                      f"| Z二阶差 p50={zr['dd_p50']:.3f} p90={zr['dd_p90']:.3f} | "
                      f"X二阶差 p90={rec['x'].get('dd_p90', float('nan')):.3f}m")

    # 汇总：Z/X 二阶差池化（跨轨迹）
    def pool(key, sub):
        vals = [t[key][sub] for t in out["tracks"]
                if t.get(key, {}).get(sub) is not None]
        return round(max(vals), 4) if vals else None
    out["pooled"] = {
        "n_tracks": len(out["tracks"]),
        "z_dd_p90_max": pool("z", "dd_p90"), "z_dd_p50_max": pool("z", "dd_p50"),
        "x_dd_p90_max": pool("x", "dd_p90"),
        "z_dd_p90_med": round(statistics.median(
            [t["z"]["dd_p90"] for t in out["tracks"]
             if t["z"].get("dd_p90") is not None]), 4)
        if out["tracks"] else None}
    print(f"[{time.time()-t_start:5.1f}s] 汇总：{out['pooled']}")
    path = demo.parent / "depth_review" / "jitter" / f"{demo.name}_entity.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print("wrote", path)


if __name__ == "__main__":
    main()
