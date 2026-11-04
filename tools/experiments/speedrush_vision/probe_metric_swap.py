# -*- coding: utf-8 -*-
"""根修探针：官方 Metric-VKITTI-S（合成数据微调版）换权重——晕影是否更弱？

背景：现役 DA-V2-S 已含 ~6200 万张合成图训练但晕影仍在；理论="自车入镜"配置
在合成数据集中稀缺，晕影区是模型外推区。本探针用官方 VKITTI 度量微调权重
（近场有真值监督、同骨干同速度）做 44 帧同尺复测，回答：
1. 晕影环带幅度（挖除带外 40~160px 环、检测带内的中位 r）是否低于现役？
2. 边界读数 dev/覆盖是否改善？

口径：米制深度 → disp=1/max(depth,0.1)（近=大，读数链口径不变）→ resize 回
1280×720 → 生产 reading_from_map（门 0.08）。preprocess 与现役完全一致。
判据（预注册）：环带幅度显著下降 且 L/R dev 不劣化 ⇒ 值得换权重+重标定；
否则"自车入镜外推区"理论成立，根修需自车入镜真值数据（成本决策交维护者）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_metric_swap.py
"""
from __future__ import annotations

import csv
import sys
import time
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

from probe_mech_score import gold_lane  # noqa: E402
from probe_halo_template import height_img  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
METRIC_ONNX = Path(__import__("os").environ.get("TEMP", ".")) / "da2_ft" / "metric_vkitti_vits_336.onnx"
ROWS = (dg.Y0, dg.DIAG_Y1)


def halo_ring_r(m: np.ndarray) -> float:
    """晕影环带（挖除带外 40~160px、检测带内）的中位 r。"""
    g = dg._ground_q20(m)[:, None]
    r = (m[ROWS[0]:ROWS[1]] - g) / np.maximum(g, 1e-6)
    cols = np.r_[dg.EGO_COLS[0] - 160:dg.EGO_COLS[0] - 40,
                 dg.EGO_COLS[1] + 40:dg.EGO_COLS[1] + 160]
    v = r[:, cols]
    return float(np.median(v[np.isfinite(v)]))


def pct(xs, q):
    xs = sorted(xs)
    return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def main() -> None:
    if not METRIC_ONNX.exists():
        print(f"缺权重 ONNX：{METRIC_ONNX}")
        return
    sess = ort.InferenceSession(str(METRIC_ONNX), providers=["DmlExecutionProvider"])

    labels = [r for r in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))]
    frames = []
    for r in labels:
        cache = pd.OUT / "npy" / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy"
        if cache.exists():
            frames.append((r, np.load(cache).astype(np.float32)))
    print(f"考卷 {len(frames)} 帧，门 {dg.GATE}，米制深度→disp=1/max(d,0.1)")

    res = {"incm": {"L": [], "R": []}, "metr": {"L": [], "R": []}}
    cov = {"incm": {"L": 0, "R": 0}, "metr": {"L": 0, "R": 0}}
    bad = {"incm": {"L": 0, "R": 0}, "metr": {"L": 0, "R": 0}}
    denom = {"L": 0, "R": 0}
    ring = {"incm": [], "metr": []}
    t_inf = []
    rendered = 0
    gallery = None
    for r, m_inc in frames:
        rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
        x = dg.preprocess(rgb, dg.DEFAULT_SHORT)
        t0 = time.perf_counter()
        d_m = sess.run(None, {"pixel_values": x})[0][0]
        t_inf.append((time.perf_counter() - t0) * 1e3)
        disp = 1.0 / np.maximum(d_m.astype(np.float32), 0.1)
        m_met = cv2.resize(disp, (1280, 720), interpolation=cv2.INTER_LINEAR)
        ring["incm"].append(halo_ring_r(m_inc))
        ring["metr"].append(halo_ring_r(m_met))
        rd0 = dg.reading_from_map(m_inc, CAL, EGO, gate=dg.GATE)
        rd1 = dg.reading_from_map(m_met, CAL, EGO, gate=dg.GATE)
        for side, lane0, lane1 in (("L", rd0.left_edge_lane, rd1.left_edge_lane),
                                   ("R", rd0.right_edge_lane, rd1.right_edge_lane)):
            glane, _ = gold_lane(r, side.lower())
            if glane is None:
                continue
            denom[side] += 1
            for tag, lane in (("incm", lane0), ("metr", lane1)):
                if lane is None or lane != lane:
                    continue
                cov[tag][side] += 1
                d = float(lane - glane)
                res[tag][side].append(d)
                bad[tag][side] += abs(d) > 0.5
        if rendered < 2 and pcq.frame_key(Path(r["path"])).endswith(("000906", "001220")):
            pair = np.hstack([height_img(r := (lambda m: (m[ROWS[0]:ROWS[1]] - dg._ground_q20(m)[:, None])
                                         / np.maximum(dg._ground_q20(m)[:, None], 1e-6))(m_inc), m_inc),
                              height_img((lambda m: (m[ROWS[0]:ROWS[1]] - dg._ground_q20(m)[:, None])
                                          / np.maximum(dg._ground_q20(m)[:, None], 1e-6))(m_met), m_met)])
            gallery = pair if gallery is None else np.vstack([gallery, pair])
            rendered += 1

    print(f"\n晕影环带中位 r（越小越好）：现役={np.median(ring['incm']):.4f}  "
          f"metric={np.median(ring['metr']):.4f}")
    print(f"推理 p50（metric，含转换）：{pct(t_inf, .5):.1f}ms")
    print(f"\n{'变体':<6}{'L覆盖':>8}{'R覆盖':>8}"
          f"{'Ldev p50/p90':>14}{'L坏':>6}{'Rdev p50/p90':>14}{'R坏':>6}")
    for tag in ("incm", "metr"):
        ld, rd_ = res[tag]["L"], res[tag]["R"]
        print(f"{tag:<7}{cov[tag]['L']:>4}/{denom['L']:<3}{cov[tag]['R']:>4}/{denom['R']:<3}"
              f"{pct(ld,.5):>+9.2f}/{pct([abs(x) for x in ld],.9):<5.2f}"
              f"{bad[tag]['L']/max(len(ld),1):>5.0%}"
              f"{pct(rd_,.5):>+9.2f}/{pct([abs(x) for x in rd_],.9):<5.2f}"
              f"{bad[tag]['R']/max(len(rd_),1):>5.0%}")
    if gallery is not None:
        out = pd.OUT / "metric_swap_cmp.jpg"
        cv2.imwrite(str(out), gallery, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"高度图对比（每对=现役|metric）：{out}")


if __name__ == "__main__":
    main()
