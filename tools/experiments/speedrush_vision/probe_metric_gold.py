# -*- coding: utf-8 -*-
"""Metric@336 全金标卷成绩单 + 相邻帧渲染（目检用）。

跑 54 帧金标：每帧 L/R 覆盖与偏差（门 0.08），汇总表；渲染相邻帧组——
金标连排（step 80）与逐帧紧邻（step 20）各一组，每帧三联：
视差 | 高度图（金标绿点、模型读数黄线）| 金标 y0 处横向高度曲线。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_metric_gold.py \
        [--gold-run 000260,000340,000420,000500] [--burst 000260,000280,000300]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

from probe_mech_score import gold_lane, gold_x_at  # noqa: E402
from probe_halo_template import height_img  # noqa: E402
from probe_metric_swap import halo_ring_r  # noqa: E402
from probe_metric_res import ROWS, rel_map, pct  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
W = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\depth_review\weights")
SHORT = 336

_K0 = (CAL.ego_cx - CAL.vpx) / (CAL.v_ego - CAL.y_h)


def lane_px(lane: float, y0: int) -> float:
    """x_lane_of 的逆（固定 y0）：车道量 → 像素 x。"""
    return CAL.vpx + (y0 - CAL.y_h) * (lane / CAL.a_x + _K0)


def infer(sess, rgb):
    d = sess.run(None, {"pixel_values": dg.preprocess(rgb, SHORT)})[0][0]
    return cv2.resize(1.0 / np.maximum(d.astype(np.float32), 0.1), (1280, 720),
                      interpolation=cv2.INTER_LINEAR)


def color(m, lo, hi):
    return cv2.applyColorMap(
        (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6), 0, 1)
         * 255).astype(np.uint8), cv2.COLORMAP_JET)


def render(stem, r, m, rm, out_path):
    rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
    lo, hi = np.percentile(np.log(np.clip(m[340:715].ravel(), 1e-3, None)), [2, 98])
    disp = color(m, lo, hi)
    ht = height_img(rm[ROWS[0]:ROWS[1]], m)

    marks = {}
    for side in ("l", "r"):
        glane, y0 = gold_lane(r, side)
        if glane is None:
            continue
        gx = float(gold_x_at(r, side, y0))
        rd = dg.reading_from_map(m, CAL, EGO, gate=dg.GATE)
        lane = rd.left_edge_lane if side == "l" else rd.right_edge_lane
        marks[side] = (y0, gx, glane, lane)

        def to_band(row, x):
            yy = row - ROWS[0]
            return int(yy), int(x)

        # 金标近点绿圈 / 模型读数黄竖线，画在视差与高度图上
        gy, gxp = to_band(y0, gx)
        cv2.circle(disp, (gxp, gy), 7, (0, 255, 0), 2)
        cv2.circle(ht, (gxp, gy), 7, (0, 255, 0), 2)
        if lane == lane and lane is not None:
            lx = int(round(lane_px(lane, y0)))
            cv2.line(disp, (lx, gy - 16), (lx, gy + 16), (0, 255, 255), 2)
            cv2.line(ht, (lx, gy - 16), (lx, gy + 16), (0, 255, 255), 2)

    # 横向高度曲线：取有金标一侧（优先 l），金标 y0 邻近行 ±3 中位
    side = "l" if "l" in marks else "r"
    if side in marks:
        y0, gx, glane, lane = marks[side]
        x0, x1 = int(max(0, gx - 260)), int(min(1280, gx + 260))
        PH, PW = 420, 1280
        panel = np.zeros((PH, PW, 3), np.uint8)
        band = np.nanmedian(rm[y0 - 3:y0 + 4, x0:x1], axis=0)
        hh = band / (1.0 + band)
        HMAX = 0.10
        pts = []
        for i, hv in enumerate(hh):
            px = int(i / max(len(hh) - 1, 1) * (PW - 100)) + 60
            py = int(PH * 0.8 - min(max(float(hv), -HMAX), HMAX) / HMAX * PH * 0.72)
            pts.append((px, py))
        cv2.polylines(panel, [np.array(pts, np.int32)], False, (255, 255, 255), 2)
        for gate, gc in ((dg.GATE, (0, 0, 255)), (0.05, (0, 128, 255))):
            h = gate / (1 + gate)
            py = int(PH * 0.8 - h / HMAX * PH * 0.72)
            cv2.line(panel, (60, py), (PW - 20, py), gc, 1)
            cv2.putText(panel, f"gate {gate}", (PW - 160, py - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, .5, gc, 1)
        gpx = int((gx - x0) / max(x1 - x0 - 1, 1) * (PW - 100)) + 60
        cv2.line(panel, (gpx, 0), (gpx, PH), (0, 255, 0), 1)
        cv2.putText(panel, "gold", (gpx - 20, 20), cv2.FONT_HERSHEY_SIMPLEX, .5,
                    (0, 255, 0), 1)
        if lane == lane and lane is not None:
            lpx = int((int(lane) - x0) / max(x1 - x0 - 1, 1) * (PW - 100)) + 60
            cv2.line(panel, (lpx, 0), (lpx, PH), (0, 255, 255), 1)
            cv2.putText(panel, "read", (lpx + 4, 40), cv2.FONT_HERSHEY_SIMPLEX, .5,
                        (0, 255, 255), 1)
        g = np.vstack([disp, ht, panel])
    else:
        g = np.vstack([disp, ht])
    cv2.imwrite(str(out_path), g, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"已出：{out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold-run", default="000260,000340,000420,000500")
    ap.add_argument("--burst", default="000260,000280,000300")
    args = ap.parse_args()

    sess = ort.InferenceSession(str(W / f"metric_vkitti_vits_{SHORT}.onnx"),
                                providers=["DmlExecutionProvider"])
    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}

    print(f"== metric@{SHORT} 金标全卷（门 {dg.GATE}）==")
    stats = {"L": [], "R": [], "cov": {"L": 0, "R": 0}, "den": {"L": 0, "R": 0},
             "bad": {"L": 0, "R": 0}, "ring": [], "s2": 0}
    for stem, r in sorted(labels.items()):
        m = infer(sess, cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB))
        rm = rel_map(m)
        stats["ring"].append(halo_ring_r(m))
        rd = dg.reading_from_map(m, CAL, EGO, gate=dg.GATE)
        cells, n2 = [], 0
        for side, lane in (("L", rd.left_edge_lane), ("R", rd.right_edge_lane)):
            glane, _ = gold_lane(r, side.lower())
            if glane is None:
                cells.append(f"{side} -/-")
                continue
            stats["den"][side] += 1
            if lane is None or lane != lane:
                cells.append(f"{side} 缺读")
                continue
            n2 += 1
            stats["cov"][side] += 1
            d = float(lane - glane)
            stats[side].append(d)
            bad = abs(d) > 0.5
            stats["bad"][side] += bad
            cells.append(f"{side} {d:+.2f}{'!' if bad else ''}")
        if n2 == 2:
            stats["s2"] += 1
        print(f"{stem[-6:]}: " + "  ".join(cells))
    ld, rd_ = stats["L"], stats["R"]
    print(f"\n汇总: 环带r={np.median(stats['ring']):.4f}  "
          f"L {stats['cov']['L']}/{stats['den']['L']}"
          f" (dev {pct(ld, .5):+.2f}/{pct([abs(x) for x in ld], .9):.2f} 坏{stats['bad']['L']}/{len(ld)})  "
          f"R {stats['cov']['R']}/{stats['den']['R']}"
          f" (dev {pct(rd_, .5):+.2f}/{pct([abs(x) for x in rd_], .9):.2f} 坏{stats['bad']['R']}/{len(rd_)})  "
          f"双侧 {stats['s2']}/{len(labels)}")

    # 相邻帧渲染：金标连排 + 紧邻连拍（同 demo）。连拍帧不在金标卷时，
    # 借最近金标帧的标线行（20 帧内路面位置漂移可忽略），只画模型读数。
    by_demo = {}
    for stem, r in labels.items():
        mnum = re.search(r"(\d+)$", stem)
        if not mnum:
            continue
        by_demo.setdefault(Path(r["path"]).parent.parent.name, {})[int(mnum.group(1))] = (stem, r)

    def pick(raws, group, allow_plain=False):
        for raw in raws:
            num = int(raw)
            for demo, d in by_demo.items():
                if num in d:
                    stem, r = d[num]
                    m = infer(sess, cv2.cvtColor(cv2.imread(str(r["path"])),
                                                 cv2.COLOR_BGR2RGB))
                    render(stem, r, m, rel_map(m),
                           pd.OUT / f"metric_gold_{group}_{raw}.jpg")
                    break
                if allow_plain:
                    near = min(d, key=lambda k: abs(k - num))
                    if abs(near - num) <= 40:
                        stem, r = d[near]
                        p = Path(r["path"]).with_name(f"{num:06d}.jpg")
                        if not p.exists():
                            continue
                        rr = dict(r, path=str(p))
                        m = infer(sess, cv2.cvtColor(cv2.imread(str(p)),
                                                     cv2.COLOR_BGR2RGB))
                        render(stem if num == near else f"plain{num}", rr, m,
                               rel_map(m),
                               pd.OUT / f"metric_gold_{group}_{raw}.jpg")
                        break

    print("\n-- 金标连排（step 80）--")
    pick([s.strip() for s in args.gold_run.split(",")], "run")
    print("-- 紧邻连拍（step 20）--")
    pick([s.strip() for s in args.burst.split(",")], "burst", allow_plain=True)


if __name__ == "__main__":
    main()
