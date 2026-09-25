# -*- coding: utf-8 -*-
"""Metric-VKITTI-S 分辨率确认探针：@336/@448/@518 渲染对比 + 数值表。

背景：官方 metric 权重训练/推理默认 518，此前探针在 336 上跑（低于训练尺寸），
尾部退化可能含分辨率混淆。本探针渲染三档的视差/高度图与横向高度曲线（目检），
并在 44 帧上出数值表（晕影环带、读数 dev/覆盖，门 0.08）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_metric_res.py [--frames 000906,001220]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402
import probe_crop_quality as pcq  # noqa: E402

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

from probe_mech_score import gold_lane, gold_x_at  # noqa: E402
from probe_halo_template import height_img  # noqa: E402
from probe_metric_swap import halo_ring_r  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
W = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\depth_review\weights")
RES = (336, 448, 518)
ROWS = (dg.Y0, dg.DIAG_Y1)


def rel_map(m: np.ndarray) -> np.ndarray:
    g = dg._ground_q20(m)[:, None]
    r = np.full(m.shape, np.nan, np.float32)
    r[ROWS[0]:ROWS[1]] = (m[ROWS[0]:ROWS[1]] - g) / np.maximum(g, 1e-6)
    return r


def pct(xs, q):
    xs = sorted(xs)
    return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="000906,001220")
    args = ap.parse_args()

    sessions = {s: ort.InferenceSession(str(W / f"metric_vkitti_vits_{s}.onnx"),
                                        providers=["DmlExecutionProvider"]) for s in RES}
    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}

    def infer(rgb, short):
        d = sessions[short].run(None, {"pixel_values": dg.preprocess(rgb, short)})[0][0]
        return cv2.resize(1.0 / np.maximum(d.astype(np.float32), 0.1), (1280, 720),
                          interpolation=cv2.INTER_LINEAR)

    # ── 全量数值表 ──
    res_stats = {s: {"ring": [], "L": [], "R": [], "cov": {"L": 0, "R": 0},
                     "bad": {"L": 0, "R": 0}, "den": {"L": 0, "R": 0}, "s2": 0}
                 for s in RES}
    for r in labels.values():
        p = Path(r["path"])
        if not (pd.OUT / "npy" / f"{pcq.frame_key(p)}__d336q4f16.npy").exists():
            continue
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        for s in RES:
            m = infer(rgb, s)
            st = res_stats[s]
            st["ring"].append(halo_ring_r(m))
            rd = dg.reading_from_map(m, CAL, EGO, gate=dg.GATE)
            n = 0
            for side, lane in (("L", rd.left_edge_lane), ("R", rd.right_edge_lane)):
                glane, _ = gold_lane(r, side.lower())
                if glane is None:
                    continue
                st["den"][side] += 1
                if lane is None or lane != lane:
                    continue
                n += 1
                st["cov"][side] += 1
                d = float(lane - glane)
                st[side].append(d)
                st["bad"][side] += abs(d) > 0.5
            if n == 2:
                st["s2"] += 1
    for s in RES:
        st = res_stats[s]
        ld, rd_ = st["L"], st["R"]
        print(f"@{s}: 环带r={np.median(st['ring']):.4f}  L {st['cov']['L']}/{st['den']['L']}"
              f"({pct(ld,.5):+.2f}/{pct([abs(x) for x in ld],.9):.2f}坏{st['bad']['L']}/{len(ld)})"
              f"  R {st['cov']['R']}/{st['den']['R']}"
              f"({pct(rd_,.5):+.2f}/{pct([abs(x) for x in rd_],.9):.2f}坏{st['bad']['R']}/{len(rd_)})"
              f"  双侧 {st['s2']}/44")

    # ── 渲染：第一帧三档 视差|高度 + 高度曲线 ──
    for raw in args.frames.split(","):
        hits = [s for s in labels if s.endswith(raw)]
        if not hits:
            continue
        stem = hits[0]
        r = labels[stem]
        rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
        maps, rmaps = {}, {}
        for s in RES:
            maps[s] = infer(rgb, s)
            rmaps[s] = rel_map(maps[s])
        logs = np.log(np.clip(np.concatenate([m[340:715].ravel() for m in maps.values()]),
                              1e-3, None))
        lo, hi = np.percentile(logs, [2, 98])

        def color(m):
            return cv2.applyColorMap(
                (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6), 0, 1)
                 * 255).astype(np.uint8), cv2.COLORMAP_JET)

        row1 = np.hstack([color(maps[s]) for s in RES])
        row2 = np.hstack([height_img(rmaps[s][ROWS[0]:ROWS[1]], maps[s]) for s in RES])
        for i, s in enumerate(RES):
            cv2.putText(row1, f"disp @{s}", (8 + i * 1280, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)

        side = "l"
        _gl, y0 = gold_lane(r, side)
        gx = float(gold_x_at(r, side, y0))
        x0, x1 = int(max(0, gx - 260)), int(min(1280, gx + 260))
        PH, PW = 420, 3840
        panel = np.zeros((PH, PW, 3), np.uint8)
        HMAX = 0.10
        cols = {336: (255, 0, 0), 448: (0, 255, 0), 518: (0, 255, 255)}
        for s in RES:
            band = rmaps[s][y0, x0:x1]
            hh = band / (1.0 + band)
            pts = []
            for i, hv in enumerate(hh):
                px = int(i / max(len(hh) - 1, 1) * (PW - 100)) + 60
                py = int(PH * 0.8 - min(max(float(hv), -HMAX), HMAX) / HMAX * PH * 0.72)
                pts.append((px, py))
            cv2.polylines(panel, [np.array(pts)], False, cols[s], 2)
        for gate, gc in ((0.08, (0, 0, 255)), (0.05, (0, 128, 255))):
            h = gate / (1 + gate)
            py = int(PH * 0.8 - h / HMAX * PH * 0.72)
            cv2.line(panel, (60, py), (PW - 20, py), gc, 1)
            cv2.putText(panel, f"gate {gate}", (PW - 160, py - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, .5, gc, 1)
        gpx = int((gx - x0) / max(x1 - x0 - 1, 1) * (PW - 100)) + 60
        cv2.line(panel, (gpx, 0), (gpx, PH), (0, 255, 0), 1)
        cv2.putText(panel, "gold", (gpx - 20, 20), cv2.FONT_HERSHEY_SIMPLEX, .5,
                    (0, 255, 0), 1)
        for i, (s, c) in enumerate(cols.items()):
            cv2.putText(panel, f"@{s}", (60 + i * 120, 44),
                        cv2.FONT_HERSHEY_SIMPLEX, .6, c, 2)
        g = np.vstack([row1, row2, panel])
        out = pd.OUT / f"metric_res_{stem}.jpg"
        cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"已出：{out}")


if __name__ == "__main__":
    main()
