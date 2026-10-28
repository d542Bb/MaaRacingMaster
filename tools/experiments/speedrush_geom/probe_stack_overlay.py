# -*- coding: utf-8 -*-
"""无深度快速栈叠加探针（C 类实验，只读回放帧）。

目的：把「路的范围 / 车身方向 / 速度 / 目标」四样状态用**不加载深度模型**的栈供出，
画回帧上人工判准不准，并逐帧记时延。深度链只在离线侧当参照，不进这条链。

栈构成：标定常数（地平线 + 相机高）→ 地面逆透视 BEV；LSD 线段 → 左右边界 RANSAC
直线 → 路心/航向/宽度；YOLO 框底点 → 地面几何测距；亮度相位相关 → 自车位移（本
探针同时记录它的失败样本，不粉饰）。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_geom/probe_stack_overlay.py \
        --video video_20261021_160547_p1 --start 400 --limit 300 --step 5

输出（数据目录 geom_overlay/<video>/）: metrics.jsonl、overlay.jpg 样张、overlay.mp4。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_geom_vo import (  # noqa: E402
    BEV, CAM_H, CAM_W, CAR_W, LSD, MIN_DENOM, Y_H, phase_motion, targets)
from maaracing_master.plugins.speedrush import PERCEPTION_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
OUT = DATA / "geom_overlay"

FIT_Z0, FIT_Z1 = 4.0, 20.0     # 边界拟合窗（米）
FIT_TOL = 0.5                  # 内点容差（米）
ZREF = 12.0                    # 横偏参照距离（米）


def side_fit(pts, tol=FIT_TOL, iters=40):
    """单侧边界点 (z,x) → 直线 x=k·z+m；点不足返回 None。"""
    arr = np.array([(z, x) for z, x in pts if FIT_Z0 <= z <= FIT_Z1], float)
    if len(arr) < 3 or arr[:, 0].max() - arr[:, 0].min() < 4.0:
        return None
    best, best_n = None, -1
    rng = np.random.default_rng(11)
    for _ in range(iters):
        i, j = rng.choice(len(arr), 2, replace=False)
        z1, x1 = arr[i]
        z2, x2 = arr[j]
        if abs(z2 - z1) < 2.0:
            continue
        k = (x2 - x1) / (z2 - z1)
        m = x1 - k * z1
        n = int(np.sum(np.abs(arr[:, 1] - (k * arr[:, 0] + m)) < tol))
        if n > best_n:
            best, best_n = (k, m), n
    if best is None or best_n < 3:
        return None
    k, m = best
    keep = np.abs(arr[:, 1] - (k * arr[:, 0] + m)) < tol
    if keep.sum() >= 3:
        k, m = np.polyfit(arr[keep, 0], arr[keep, 1], 1)
    return float(k), float(m), int(keep.sum())


def lane_segments(gray, bev):
    """LSD 线段 → BEV 米制端点，按左右侧分堆（同时保留线段本身供画图）。"""
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    segs = LSD.detect(small)[0]
    left, right, lines = [], [], []
    if segs is None:
        return left, right, lines
    for x1, y1, x2, y2 in segs.reshape(-1, 4) * 2.0:
        if max(y1, y2) < Y_H + 10 or np.hypot(x2 - x1, y2 - y1) < 20:
            continue
        xa, za, oka = bev.to_bev(np.array([x1]), np.array([y1]))
        xb, zb, okb = bev.to_bev(np.array([x2]), np.array([y2]))
        if not (oka[0] and okb[0]):
            continue
        dx, dz = xb[0] - xa[0], zb[0] - za[0]
        if abs(dz) < 2.0 or abs(dx / dz) > 0.5:
            continue
        lines.append(((xa[0], za[0]), (xb[0], zb[0])))
        for xx, zz in ((xa[0], za[0]), (xb[0], zb[0])):
            (left if xx < 0 else right).append((float(zz), float(xx)))
    return left, right, lines


def to_px(x, z, fy_cam_h=560.0 * 1.90, cx=640.0, fx=559.2):
    """地面米制点 → 像素（逆 IPM）。"""
    v = Y_H + fy_cam_h / z
    u = cx + fx * x / z
    return int(round(u)), int(round(v))


def render(frame, dets, left, right, fl, fr, d, resp, ms):
    out = frame.copy()
    for pts, col in ((left, (0, 255, 255)), (right, (0, 165, 255))):
        for z, x in pts:
            cv2.circle(out, to_px(x, z), 3, col, -1)
    for fit, col in ((fl, (0, 255, 255)), (fr, (0, 165, 255))):
        if fit is None:
            continue
        k, m, _n = fit
        for z0, z1 in ((FIT_Z0, FIT_Z1),):
            cv2.line(out, to_px(k * z0 + m, z0), to_px(k * z1 + m, z1), col, 2)
    if fl is not None and fr is not None:
        k = (fl[0] + fr[0]) / 2.0
        m = (fl[1] + fr[1]) / 2.0
        for z in np.arange(6.0, 22.0, 2.0):
            cv2.circle(out, to_px(k * z + m, z), 2, (255, 0, 255), -1)
        cv2.line(out, to_px(0.0, ZREF), to_px(m + k * ZREF, ZREF), (255, 0, 255), 2)
    for t in dets:
        col = {"car": (0, 0, 255), "coin": (0, 220, 0), "bonus": (0, 255, 0)}[t["kind"]]
        u, v, w, h = t["u"], t["v"], t["w_px"], t.get("h_px", 20)
        cv2.rectangle(out, (u - w // 2, v - h), (u + w // 2, v), col, 2)
        cv2.putText(out, f"{t['kind']} {t['z_ground']:.1f}m", (u - w // 2, max(14, v - h - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    off = None if (fl is None or fr is None) else round((fl[1] + fr[1]) / 2.0 + (fl[0] + fr[0]) / 2.0 * ZREF, 2)
    hdg = None if (fl is None or fr is None) else float(np.degrees(np.arctan((fl[0] + fr[0]) / 2.0)))
    txt = (f"no-depth stack  {ms:.0f}ms  "
           f"d={'-' if d is None else format(d, '+.2f')}m/f "
           f"resp={'-' if resp is None else format(resp, '.2f')}  "
           f"off={off}  hdg={hdg if hdg is None else round(hdg, 1)}")
    cv2.rectangle(out, (0, 0), (CAM_W, 26), (0, 0, 0), -1)
    cv2.putText(out, txt, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                cv2.LINE_AA)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="video_20261021_160547_p1")
    ap.add_argument("--start", type=int, default=400)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--step", type=int, default=5, help="每多少帧存一张叠加样张")
    ap.add_argument("--fps", type=float, default=60.0)
    args = ap.parse_args()

    out_dir = OUT / args.video
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((DEMOS / args.video).glob("*.jpg"))[args.start:args.start + args.limit]
    per = StreetPerception(str(PERCEPTION_MODEL_FILE))
    bev = BEV()
    rows, prev_bev, vid = [], None, None
    mf = (out_dir / "metrics.jsonl").open("w", encoding="utf-8")
    for i, path in enumerate(files):
        frame = cv2.imread(str(path))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        t0 = time.perf_counter()
        res = per.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), frame_id=args.start + i)
        dets = targets(res, bev)
        for t, d0 in zip(dets, list(res.cars) + list(res.coins) + list(res.bonuses)):
            t["w_px"], t["h_px"] = d0.w, d0.h
        left, right, _lines = lane_segments(gray, bev)
        fl, fr = side_fit(left), side_fit(right)
        bev_cur = bev.image(gray)
        d = s_lat = resp = None
        if prev_bev is not None:
            d, s_lat, resp = phase_motion(prev_bev, bev_cur, bev)
        prev_bev = bev_cur
        ms = (time.perf_counter() - t0) * 1e3
        row = {"fid": args.start + i, "ms": ms, "d": d, "s_lat": s_lat, "resp": resp,
               "n_left": len(left), "n_right": len(right),
               "fit_l": None if fl is None else [round(fl[0], 4), round(fl[1], 2), fl[2]],
               "fit_r": None if fr is None else [round(fr[0], 4), round(fr[1], 2), fr[2]],
               "width_12": None if (fl is None or fr is None)
               else round((fr[1] - fl[1]) + (fr[0] - fl[0]) * ZREF, 2),
               "n_det": len(dets),
               "det": [(t["kind"], t["z_ground"]) for t in dets]}
        rows.append(row)
        mf.write(json.dumps(row, ensure_ascii=False) + "\n")
        vis = render(frame, dets, left, right, fl, fr, d, resp, ms)
        if i % args.step == 0:
            cv2.imwrite(str(out_dir / f"overlay_{args.start + i:06d}.jpg"), vis)
        if vid is None:
            vid = cv2.VideoWriter(str(out_dir / "overlay.mp4"),
                                  cv2.VideoWriter_fourcc(*"mp4v"), 12,
                                  (vis.shape[1] // 2, vis.shape[0] // 2))
        vid.write(cv2.resize(vis, (vis.shape[1] // 2, vis.shape[0] // 2)))
    mf.close()
    if vid:
        vid.release()
    ms = np.array([r["ms"] for r in rows])
    both = [r for r in rows if r["fit_l"] and r["fit_r"]]
    print(f"n={len(rows)} ms p50={np.median(ms):.0f} p90={np.percentile(ms,90):.0f}")
    print(f"双侧拟合成功 {len(both)}/{len(rows)}  路宽@12m 中位 "
          f"{np.median([r['width_12'] for r in both]) if both else None}")
    dd = [r["d"] for r in rows if r["d"] is not None]
    print(f"位移中位 {np.median(dd) if dd else None} m/帧（参照：深度链同段 1.09）")


if __name__ == "__main__":
    main()
