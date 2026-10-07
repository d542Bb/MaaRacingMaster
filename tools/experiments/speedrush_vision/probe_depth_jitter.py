# -*- coding: utf-8 -*-
"""MoGe 深度源端时间抖动量化（全面转 3D 前置测量一，2026-10-07）。

**问题**：2D 行号口径「有偏但稳」，深度 Z 口径「无偏但抖」——源端到底抖多少？
此前产线的全部抗抖手段（近 3 对中值窗、0.8 道新息门、1.0s 陈旧重基、W±40%
污染对门、栅格洞边片除名）都是下游补丁，源端噪声幅度从未被直接测量。本探针
补这个数：它决定「源端上时间滤波（平面 EMA/时域融合）一处治本」还是「继续
下游各抄一份补丁」。

**方法**：demo 全速率连续帧（≈20fps）按低运动分选窗（W=16 帧 ≈0.8s），逐帧
跑产线同款 MoGe→找边→栅格（infer_points + reading_from_points +
drivable_grid_from_points + grid_center_lane，含 YOLO 物体掩码）。窗口内真实
场景变化平滑，故：
  - 帧间差 Δt = x[t+1]−x[t]：真实运动 + 噪声的混合（低频为主）；
  - 二阶差 δt = x[t+1]−2x[t]+x[t−1]：光滑运动下 ≈0，残留即**源端噪声**——
    主指标（P50/P90 |δ|）。
量四层：平面法向帧间夹角（源端几何）、左右缘车道量与路心 ro_edge（找边读数）、
栅格路心 ro_grid（BEV 消费口径）、同帧焦距 fx（源稳定性的直接信号）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_depth_jitter.py \
        --demo <dir> [--windows 4] [--winlen 16]
产物：<demo>/../depth_review/jitter/<demo名>_summary.json + 控制台摘要。
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

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
MOGE_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/onnx/depth"
             / "moge2_vits_static_336x598_t1032_q4f16.onnx")
YOLO_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/onnx/perception"
             / "model.onnx")
EGO_MASK_FILE = (REPO / "maaracing_master/plugins/speedrush/resources/calibration"
                 / "ego_mask.json")
YOLO_MARGIN = 10   # 与产线 module._yolo_object_mask 的 YOLO_MASK_MARGIN 同值

METRICS = ("left", "right", "ro_edge", "ro_grid", "fx", "nrm_deg")


def yolo_object_mask(dets, w: int, h: int) -> np.ndarray:
    """检测框 → 全幅布尔掩码（外扩 YOLO_MARGIN；与产线同口径，含 None 框防御）。"""
    m = np.zeros((h, w), bool)
    for d in dets:
        x0, y0, x1, y1 = d["box"]
        m[max(0, y0 - YOLO_MARGIN):min(h, y1 + YOLO_MARGIN),
          max(0, x0 - YOLO_MARGIN):min(w, x1 + YOLO_MARGIN)] = True
    return m


def motion_scores(demo: Path) -> np.ndarray:
    """逐帧运动分：相邻帧低分辨率灰度平均绝对差（选窗用，不跑模型）。"""
    rows = [json.loads(l) for l in
            (demo / "frames.jsonl").open(encoding="utf-8") if l.strip()]
    scores = np.zeros(len(rows))
    prev = None
    for i, r in enumerate(rows):
        img = cv2.imread(str(demo / "frames" / r["file"]), cv2.IMREAD_GRAYSCALE)
        small = cv2.resize(img, (160, 90)) if img is not None else None
        if prev is not None and small is not None:
            scores[i] = float(np.abs(small.astype(np.int16) - prev).mean())
        prev = small
    return scores, rows


def pick_windows(scores: np.ndarray, n_win: int, winlen: int
                 ) -> list[tuple[int, int]]:
    """最低平均运动分的互不重叠窗（长度 winlen，返回 [start, end) 序号区间）。"""
    n = len(scores)
    cands = sorted((float(scores[i:i + winlen].mean()), i)
                   for i in range(n - winlen + 1))
    wins: list[tuple[int, int]] = []
    for _, i in cands:
        if all(i + winlen <= a or i >= b for a, b in wins):
            wins.append((i, i + winlen))
        if len(wins) >= n_win:
            break
    return sorted(wins)


def normal_deg(coef) -> float | None:
    """平面 Y=aX+bZ+c 的法向角（绕水平轴的倾角，deg；帧间差即几何抖动）。"""
    if coef is None:
        return None
    a, b = coef[0], coef[1]
    return float(np.degrees(np.arctan2(np.hypot(a, b), 1.0)))


def second_diff_stats(x: list[float]) -> dict:
    """序列的帧间差与二阶差统计（None 已剔除；<3 点返回空）。"""
    if len(x) < 3:
        return {}
    d = [b - a for a, b in zip(x, x[1:])]
    dd = [b - 2 * m + a for a, m, b in zip(x, x[1:], x[2:])]
    ad = [abs(v) for v in d]
    add = [abs(v) for v in dd]
    return {
        "n": len(x),
        "d_p50": round(statistics.median(ad), 4),
        "d_p90": round(sorted(ad)[int(len(ad) * 0.9)], 4),
        "d_max": round(max(ad), 4),
        "dd_p50": round(statistics.median(add), 4),
        "dd_p90": round(sorted(add)[int(len(add) * 0.9)], 4),
        "dd_max": round(max(add), 4),
    }


def run_window(sess, yolo, cal, lane_w_m, ego_mask, demo, rows,
               start: int, winlen: int) -> dict:
    """一个窗：逐帧产线口径推理+读数，产出逐帧量与统计。"""
    frames = []
    for i in range(start, start + winlen):
        r = rows[i]
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / r["file"])),
                           cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter()
        pts, fx, fy = dg.infer_points(sess, img)
        infer_ms = (time.perf_counter() - t0) * 1000.0
        _cls, dets, _raw = yolo(img)
        obj = yolo_object_mask(dets, pts.shape[1], pts.shape[0])
        rd = dg.reading_from_points(pts, fx, cal, ego_mask, obj, fy=fy)
        grid = dg.drivable_grid_from_points(pts, fx, fy, ego_mask, obj,
                                            coef=rd.coef)
        ro_g = dg.grid_center_lane(grid, lane_w_m)
        ro_e = (None if rd.sides < 2 or rd.left_edge_lane is None
                or rd.right_edge_lane is None
                else -(rd.left_edge_lane + rd.right_edge_lane) / 2.0)
        frames.append({
            "seq": r["seq"], "sides": rd.sides, "rejects": list(rd.rejects),
            "left": rd.left_edge_lane, "right": rd.right_edge_lane,
            "ro_edge": ro_e, "ro_grid": None if ro_g is None else -ro_g,
            "fx": fx, "nrm_deg": normal_deg(rd.coef),
            "infer_ms": round(infer_ms, 1),
            "grid_unknown": float((grid.state == dg.GRID_UNKNOWN).mean()),
        })

    def series(key: str) -> list[float]:
        return [f[key] for f in frames if f.get(key) is not None]

    stats = {k: second_diff_stats(series(k)) for k in METRICS}
    return {"start_seq": rows[start]["seq"], "end_seq": rows[start + winlen - 1]["seq"],
            "frames": frames, "stats": stats}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--windows", type=int, default=4)
    ap.add_argument("--winlen", type=int, default=16)
    args = ap.parse_args()
    demo = Path(args.demo)
    t_start = time.time()

    scores, rows = motion_scores(demo)
    wins = pick_windows(scores, args.windows, args.winlen)
    print(f"[{time.time()-t_start:6.1f}s] {demo.name}: {len(rows)} 帧，"
          f"选 {len(wins)} 窗 × {args.winlen} 帧（运动分 P10="
          f"{np.percentile(scores, 10):.1f}，选中窗均值 "
          f"{[round(float(scores[a:b].mean()),1) for a,b in wins]}）")

    sess = dg.load_session(MOGE_ONNX)
    yolo = YOLODetector(str(YOLO_ONNX), conf=0.35, iou=0.45)
    cal = load_calib()
    em = json.loads(EGO_MASK_FILE.read_text(encoding="utf-8"))
    h, w = 720, 1280
    ego_mask = np.zeros((h, w), bool)
    ego_mask[em["y0"]:em["y1"], em["x0"]:em["x1"]] = True

    out = {"demo": demo.name, "winlen": args.winlen, "windows": []}
    for wi, (a, b) in enumerate(wins):
        res = run_window(sess, yolo, cal, cal.lane_w_m, ego_mask, demo,
                         rows, a, args.winlen)
        out["windows"].append(res)
        st = res["stats"]
        fmt = lambda k: (f"dd_p90={st[k]['dd_p90']}" if st.get(k) else "n/a")
        print(f"[{time.time()-t_start:6.1f}s]   窗{wi} seq{res['start_seq']}~"
              f"{res['end_seq']}: "
              f"ro_edge {fmt('ro_edge')} | ro_grid {fmt('ro_grid')} | "
              f"left {fmt('left')} | right {fmt('right')} | "
              f"nrm {fmt('nrm_deg')} | fx {fmt('fx')}")

    # 跨窗汇总：同指标二阶差 P90 池化
    pooled = {}
    for k in METRICS:
        vals = [s for wres in out["windows"]
                if (s := wres["stats"].get(k)) is not None]
        if vals:
            pooled[k] = {
                "dd_p90_max": max(v["dd_p90"] for v in vals),
                "dd_p50_max": max(v["dd_p50"] for v in vals),
                "d_p90_max": max(v["d_p90"] for v in vals),
                "n_frames": sum(v["n"] for v in vals)}
    out["pooled"] = pooled
    out_dir = demo.parent / "depth_review" / "jitter"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{demo.name}_summary.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"[{time.time()-t_start:6.1f}s] 汇总（跨窗最差二阶差 P90）：")
    for k, v in pooled.items():
        print(f"    {k:8s} dd_p90={v['dd_p90_max']:<8} dd_p50={v['dd_p50_max']:<8} "
              f"d_p90={v['d_p90_max']:<8} n={v['n_frames']}")
    print("wrote", path)


if __name__ == "__main__":
    main()
