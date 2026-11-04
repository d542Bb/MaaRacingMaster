# -*- coding: utf-8 -*-
"""YOLO×深度融合读数首探：检测框并入掩码剔除车/金币/奖励块，122 帧金标全卷
对比两权重（rel q4f16 现役 / metric q4f16）在护栏/路肩两类边界上的分离质量。

目标架构（维护者 2026-09-25 定向）：深度出区域块、YOLO 打身份——「是什么」由
语义说、深度只管几何。本探针是融合读数的第一块：物体框（外扩 YOLO_MARGIN px）
并入候选块掩码，与 ego 掩码同通道；护栏/路肩缘不受影响，车/金币块从源头消失。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_yolo_fused.py [--no-fuse 对照]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from probe_metric_gold import infer, pct, ROWS, rel_map  # noqa: E402
from probe_metric_swap import halo_ring_r  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
W = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\depth_review\weights")
YOLO_MARGIN = 10   # 检测框外扩（px）：框缘的深度过渡带不留在候选区


def yolo_mask(sp: StreetPerception, rgb, ego: np.ndarray) -> np.ndarray:
    res = sp.detect(rgb)
    mask = ego.copy()
    for det in res.cars + res.coins + res.bonuses:
        x0 = int(max(det.cx - det.w / 2 - YOLO_MARGIN, 0))
        x1 = int(min(det.cx + det.w / 2 + YOLO_MARGIN, 1279))
        y0 = int(max(det.cy - det.h / 2 - YOLO_MARGIN, 0))
        y1 = int(min(det.cy + det.h / 2 + YOLO_MARGIN, 719))
        mask[y0:y1 + 1, x0:x1 + 1] = True
    return mask


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuse", default="1", choices=("0", "1"))
    args = ap.parse_args()

    sp = StreetPerception(str(PERCEPTION_MODEL_FILE))
    sess = {"rel": dg.load_session(DEPTH_MODEL_FILE),
            "met": ort.InferenceSession(str(W / "metric_vkitti_vits_336_q4f16.onnx"),
                                        providers=["DmlExecutionProvider"])}
    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}

    for tag, s in (("rel", sess["rel"]), ("met", sess["met"])):
        stats = {"L": [], "R": [], "cov": {"L": 0, "R": 0}, "den": {"L": 0, "R": 0},
                 "bad": {"L": 0, "R": 0}, "s2": 0}
        st_str = {}
        for stem, r in sorted(labels.items()):
            rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
            m = infer(s, rgb, metric=(tag == "met"))
            ego = yolo_mask(sp, rgb, EGO) if args.fuse == "1" else EGO
            rd = dg.reading_from_map(m, CAL, ego)
            st = st_str.setdefault(r["stratum"], {
                "L": [], "R": [], "cov": {"L": 0, "R": 0}, "den": {"L": 0, "R": 0},
                "bad": {"L": 0, "R": 0}, "s2": 0})
            n2 = 0
            for side, lane in (("L", rd.left_edge_lane), ("R", rd.right_edge_lane)):
                from probe_mech_score import gold_lane
                glane, _ = gold_lane(r, side.lower())
                if glane is None:
                    continue
                for d in (stats, st):
                    d["den"][side] += 1
                if lane is None or lane != lane:
                    continue
                n2 += 1
                for d in (stats, st):
                    d["cov"][side] += 1
                dev = float(lane - glane)
                for d in (stats, st):
                    d[side].append(dev)
                    d["bad"][side] += abs(dev) > 0.5
            if n2 == 2:
                stats["s2"] += 1
                st["s2"] += 1
        fuse = "YOLO融合" if args.fuse == "1" else "无融合对照"
        ld, rd_ = stats["L"], stats["R"]
        print(f"== {tag} [{fuse}] ==  L {stats['cov']['L']}/{stats['den']['L']}"
              f" ({pct(ld, .5):+.2f}/{pct([abs(x) for x in ld], .9):.2f} 坏{stats['bad']['L']}/{len(ld)})"
              f"  R {stats['cov']['R']}/{stats['den']['R']}"
              f" ({pct(rd_, .5):+.2f}/{pct([abs(x) for x in rd_], .9):.2f} 坏{stats['bad']['R']}/{len(rd_)})"
              f"  双侧 {stats['s2']}/{len(labels)}")
        for name, st in sorted(st_str.items()):
            l, rr = st["L"], st["R"]
            print(f"  {name:14s} L {st['cov']['L']}/{st['den']['L']}"
                  f" ({pct(l, .5):+.2f} 坏{st['bad']['L']}/{len(l)})"
                  f"  R {st['cov']['R']}/{st['den']['R']}"
                  f" ({pct(rr, .5):+.2f} 坏{st['bad']['R']}/{len(rr)})  双侧 {st['s2']}")


if __name__ == "__main__":
    main()
