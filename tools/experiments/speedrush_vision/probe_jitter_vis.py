# -*- coding: utf-8 -*-
"""抖动结论的可视化证据（配合 probe_depth_jitter / probe_entity_z_jitter）。

图 A（找边翻跳）：demo 近静止窗内取 4 帧（画面几乎不变），逐帧画找边给出的
左右缘像素列（红/蓝竖线，会跳）与栅格路心（绿竖线，不动）——画面没动而红线
横跳 = 翻跳是找边锁错结构，不是深度噪声。
图 B（实体 Z 抖动）：demo 近静止窗内 IoU 关联一辆最近的车，画逐帧 Z_med 曲线
（上，摆动 ±1.5m）与逐帧像素行 cy（下，平的）——框不动而距离读数摆 = 深度
差分算接近速率不可行。

用法：python probe_jitter_vis.py --demo <dir> --mode edge --seqs 748 752 753 763
      python probe_jitter_vis.py --demo <dir> --mode entity --start 841 --winlen 16
产物：<demo>/../depth_review/jitter/vis_edge.jpg | vis_entity_z.jpg
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from tools.experiments.speedrush_vision.probe_depth_jitter import (  # noqa: E402
    MOGE_ONNX, YOLO_ONNX, yolo_object_mask)
from tools.experiments.speedrush_vision.probe_entity_z_jitter import (  # noqa: E402
    run_window as entity_window, entity_z)

GREEN, RED, BLUE, WHITE, GRAY = (80, 220, 80), (60, 60, 230), (230, 130, 40), (255, 255, 255), (160, 160, 160)


def load_frame(demo: Path, rows: dict, seq: int):
    r = rows[seq]
    img = cv2.cvtColor(cv2.imread(str(demo / "frames" / r["file"])),
                       cv2.COLOR_BGR2RGB)
    return img


def vis_edge(demo: Path, seqs: list[int]) -> Path:
    rows = {r["seq"]: r for r in jsonl(demo)}
    sess = dg.load_session(MOGE_ONNX)
    yolo = YOLODetector(str(YOLO_ONNX), conf=0.35, iou=0.45)
    cal = load_calib()
    import json as _json
    em = _json.loads((Path(dg.__file__).parent / "resources/calibration/ego_mask.json")
                     .read_text(encoding="utf-8"))
    ego = np.zeros((720, 1280), bool)
    ego[em["y0"]:em["y1"], em["x0"]:em["x1"]] = True

    panels = []
    for seq in seqs:
        img = load_frame(demo, rows, seq)
        pts, fx, fy = dg.infer_points(sess, img)
        _c, dets, _r = yolo(img)
        obj = yolo_object_mask(dets, 1280, 720)
        rd = dg.reading_from_points(pts, fx, cal, ego, obj, fy=fy)
        grid = dg.drivable_grid_from_points(pts, fx, fy, ego, obj, coef=rd.coef)
        off = dg.grid_center_lane(grid, cal.lane_w_m)
        vis = cv2.resize(img, (640, 360))
        if rd.left_x is not None:
            cv2.line(vis, (int(rd.left_x) // 2, 0), (int(rd.left_x) // 2, 360), RED, 2)
        if rd.right_x is not None:
            cv2.line(vis, (int(rd.right_x) // 2, 0), (int(rd.right_x) // 2, 360), BLUE, 2)
        if off is not None:
            cv2.line(vis, (320, 0), (320, 360), GREEN, 2)   # off≈0 → 路心=光轴列
        txt = f"seq{seq}  L={rd.left_edge_lane}  R={rd.right_edge_lane}  grid={None if off is None else round(-off, 3)}"
        cv2.rectangle(vis, (0, 335), (640, 360), (0, 0, 0), -1)
        cv2.putText(vis, txt, (6, 353), cv2.FONT_HERSHEY_SIMPLEX, 0.45, WHITE, 1)
        panels.append(vis)
    row = np.hstack(panels)
    head = np.full((34, row.shape[1], 3), 20, np.uint8)
    cv2.putText(head, "RED=left edge (flaps)  BLUE=right edge  GREEN=grid center (stable)   scene ~static",
                (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1)
    out = demo.parent / "depth_review" / "jitter" / "vis_edge.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(np.vstack([head, row]), cv2.COLOR_RGB2BGR))
    return out


def vis_entity(demo: Path, start: int, winlen: int) -> Path:
    rows = jsonl(demo)
    sess = dg.load_session(MOGE_ONNX)
    yolo = YOLODetector(str(YOLO_ONNX), conf=0.35, iou=0.45)
    tracks = entity_window(sess, yolo, demo, rows, start, winlen)
    if not tracks:
        raise SystemExit("该窗无 ≥6 帧实体轨迹")
    t = min(tracks, key=lambda t: sum(o[1] for o in t["obs"]) / len(t["obs"]))
    zs = [o[1] for o in t["obs"]]
    cys = [float(o[3]) for o in t["obs"]]

    W, H, M = 900, 300, 50
    canvas = np.full((2 * H + 130, W, 3), 245, np.uint8)

    def chart(y0, vals, lo, hi, color, title):
        cv2.putText(canvas, title, (M, y0 - 12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (30, 30, 30), 1)
        for gv in np.arange(lo, hi + 1e-9, (hi - lo) / 4):
            gy = y0 + H - int((gv - lo) / (hi - lo) * (H - 40)) - 20
            cv2.line(canvas, (M, gy), (W - 20, gy), (215, 215, 215), 1)
            cv2.putText(canvas, f"{gv:.1f}", (4, gy + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, GRAY, 1)
        pts = [(M + int(i / (len(vals) - 1) * (W - M - 40)),
                y0 + H - int((v - lo) / (hi - lo) * (H - 40)) - 20)
               for i, v in enumerate(vals)]
        cv2.polylines(canvas, [np.array(pts)], False, color, 2)
        for p in pts:
            cv2.circle(canvas, p, 4, color, -1)

    zlo, zhi = min(zs) - 0.3, max(zs) + 0.3
    chart(40, zs, zlo, zhi, RED,
          f"entity[{t['cls']}] Z per frame (m)  scene ~static, p90 wobble="
          f"{np.percentile(np.abs(np.diff(zs, 2)), 90):.2f}m")
    cylo, cyhi = min(cys) - 5, max(cys) + 5
    chart(H + 130, cys, cylo, cyhi, BLUE,
          "entity pixel row cy per frame (px)  -- flat: the box does not move")
    out = demo.parent / "depth_review" / "jitter" / "vis_entity_z.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    return out


def find_scene_start(sess, yolo, demo, rows, winlen: int,
                     max_try: int = 6) -> int | None:
    """YOLO 扫描（步长 8）找「车+币同屏」的帧，以其为中枢展开 winlen 窗。"""
    for i in range(4, len(rows) - winlen, 8):
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / rows[i]["file"])),
                           cv2.COLOR_BGR2RGB)
        dets = yolo(img)[1]
        kinds = {d["class_name"] for d in dets}
        if "car" not in kinds or "coin" not in kinds:
            continue
        start = max(0, i - winlen // 4)
        tracks = entity_window(sess, yolo, demo, rows, start, winlen)
        kinds_t = {t["cls"] for t in tracks if len(t["obs"]) >= 8}
        if {"car", "coin"} <= kinds_t:
            return start
        print(f"  candidate seq{rows[i]['seq']} -> tracks {kinds_t}, keep scanning")
    return None


def mini_chart(canvas, x0, y0, w, h, vals, color, title):
    lo, hi = min(vals) - 1e-9, max(vals) + 1e-9
    cv2.putText(canvas, title, (x0, y0 - 10), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (30, 30, 30), 1)
    for k in range(5):
        gv = lo + (hi - lo) * k / 4
        gy = y0 + h - int(k / 4 * (h - 30)) - 15
        cv2.line(canvas, (x0, gy), (x0 + w, gy), (215, 215, 215), 1)
        cv2.putText(canvas, f"{gv:.1f}", (x0 - 42, gy + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, GRAY, 1)
    pts = [(x0 + int(i / (len(vals) - 1) * w),
            y0 + h - int((v - lo) / (hi - lo) * (h - 30)) - 15)
           for i, v in enumerate(vals)]
    cv2.polylines(canvas, [np.array(pts)], False, color, 2)
    for p in pts:
        cv2.circle(canvas, p, 3, color, -1)


def vis_scene(demo: Path, start: int | None, winlen: int = 16) -> Path:
    rows = jsonl(demo)
    sess = dg.load_session(MOGE_ONNX)
    yolo = YOLODetector(str(YOLO_ONNX), conf=0.35, iou=0.45)
    if start is None:
        start = find_scene_start(sess, yolo, demo, rows, winlen)
        if start is None:
            raise SystemExit("未找到车+币同屏窗")
    print(f"window start seq{rows[start]['seq']} len={winlen}")
    tracks = entity_window(sess, yolo, demo, rows, start, winlen)

    def pick(cls):
        cands = [t for t in tracks if t["cls"] == cls]
        return max(cands, key=lambda t: len(t["obs"]), default=None)

    car, coin = pick("car"), pick("coin")

    # 帧条：首/中/尾三帧画框 + 深度读数
    panels = []
    for seq in (rows[start]["seq"], rows[start + winlen // 2]["seq"],
                rows[start + winlen - 1]["seq"]):
        img = load_frame(demo, rows, seq)
        pts, _fx, _fy = dg.infer_points(sess, img)
        dets = yolo(img)[1]
        for d in dets:
            e = entity_z(pts, d["box"])
            x0, y0, x1, y1 = d["box"]
            c = RED if d["class_name"] == "car" else (60, 200, 230)
            cv2.rectangle(img, (x0, y0), (x1, y1), c, 2)
            if e is not None:
                cv2.rectangle(img, (x0, max(0, y0 - 22)), (x0 + 130, y0),
                              (0, 0, 0), -1)
                cv2.putText(img, f"{d['class_name']} Z={e[1]:.1f}m",
                            (x0 + 3, max(14, y0 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1)
        panels.append(cv2.resize(img, (640, 360)))
    strip = np.hstack(panels)

    # 四联小图：车/币各一对（像素行 vs 深度 Z）
    CW, CH = 480, 260
    charts = np.full((CH + 50, CW * 4, 3), 245, np.uint8)
    slots = []
    if car is not None:
        slots += [("car", [o[3] for o in car["obs"]], BLUE, "car pixel row (px)"),
                  ("car", [o[1] for o in car["obs"]], RED, "car Z per frame (m)")]
    if coin is not None:
        slots += [("coin", [o[3] for o in coin["obs"]], BLUE, "coin pixel row (px)"),
                  ("coin", [o[1] for o in coin["obs"]], RED, "coin Z per frame (m)")]
    for k, (_cls, vals, c, title) in enumerate(slots):
        mini_chart(charts, 60 + CW * k, 40, CW - 80, CH, vals, c, title)
    head = np.full((30, strip.shape[1], 3), 20, np.uint8)
    cv2.putText(head, f"seq{rows[start]['seq']}-{rows[start+winlen-1]['seq']}  "
                "boxes: Z reading per frame", (8, 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, WHITE, 1)
    out = demo.parent / "depth_review" / "jitter" / "vis_scene.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(
        np.vstack([head, strip, charts]), cv2.COLOR_RGB2BGR))
    return out


def jsonl(demo: Path):
    import json
    return [json.loads(l) for l in
            (demo / "frames.jsonl").open(encoding="utf-8") if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--mode", choices=["edge", "entity", "scene"], required=True)
    ap.add_argument("--seqs", type=int, nargs="*")
    ap.add_argument("--start", type=int)
    ap.add_argument("--winlen", type=int, default=16)
    args = ap.parse_args()
    demo = Path(args.demo)
    if args.mode == "edge":
        print("wrote", vis_edge(demo, args.seqs))
    elif args.mode == "entity":
        print("wrote", vis_entity(demo, args.start, args.winlen))
    else:
        print("wrote", vis_scene(demo, args.start, args.winlen))


if __name__ == "__main__":
    main()
