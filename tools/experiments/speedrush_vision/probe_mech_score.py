# -*- coding: utf-8 -*-
"""机制探针金标评分：家族4 warp × 家族6 guided × 基线/参照，同一把尺裁决。

考卷：depth_review/gold_labels.csv 中有 @336q4f16 缓存的 44 帧（与生产化落档
同一考卷，口径同 probe_readband：生产 reading_from_map（含源行规则+守卫）、
生产门 GATE=0.08）。dev 利用车道量沿 3D 直线不变：拟合/读数车道量 − 金标线
在任一画面内行的车道量（每帧固定取金标可见行中位 y0，四变体共用同 y0 保可比）。

变体（同一金标、同一读数链，只换视差图来源）：
- base   全帧@336（生产现状，npy 缓存）
- warp   三分段 warp 输入 @336 → 视差反映射回原帧坐标（probe_warp_resample）
- guided 全帧@336 + numpy guided filter r=8/eps=0.01（probe_guided_refine）
- ref448 全帧@448（质量参照，不计成本）

判据（与生产化落档同型）：覆盖（有效读数/金标该侧在场帧数）、dev p50/p90
（车道量）、坏>0.5 车道占比、双侧同现。warp 的评分基础假设：反映射后边界线
恢复直线性（fwd∘inv=恒等），生产直线拟合无需按段改——由本评分直接检验。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_mech_score.py [--gate 0.08]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402
import probe_crop_quality as pcq  # noqa: E402

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

from probe_warp_resample import _row_maps  # noqa: E402
from probe_guided_refine import guided_filter  # noqa: E402

NPY = pd.OUT / "npy"
CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
GUIDE_R, GUIDE_EPS = 8, 0.01   # 与首档渲染一致，不在金标上调参


def gold_x_at(r, side, y):
    if r[side + "cls"] == "skip":
        return None
    nx, ny = float(r[side + "_nx"]), float(r[side + "_ny"])
    fx, fy = float(r[side + "_fx"]), float(r[side + "_fy"])
    if abs(ny - fy) < 1:
        return None
    return nx + (y - ny) * (fx - nx) / (fy - ny)


def gold_lane(r, side):
    """金标线车道量（在可见行中位 y0 处求值；车道量沿线不变）。不可见→None。"""
    vis = [y for y in range(dg.Y0, dg.DIAG_Y1)
           if (gx := gold_x_at(r, side, y)) is not None and 0 <= gx <= 1279]
    if not vis:
        return None, None
    y0 = int(np.median(vis))
    return x_lane_of(int(round(gold_x_at(r, side, y0))), y0, CAL), y0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", type=float, default=dg.GATE)
    ap.add_argument("--gate-l", type=float, default=None,
                    help="per-side 门：L 用 gate-l、R 用 gate-r（晕影治理候选）")
    ap.add_argument("--gate-r", type=float, default=None)
    ap.add_argument("--grid", default=None,
                    help="warp 一次性门重标定：逗号分隔门位网格（同 0.08 原裁定程序）")
    args = ap.parse_args()

    map_x, (my_warp, my_unwarp) = _row_maps()
    sess336 = dg.load_session(DEPTH_MODEL_FILE)
    import onnxruntime as ort
    from probe_warp_resample import _folded
    sess448 = ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=_folded(448),
                                   providers=["DmlExecutionProvider"])

    frames = []
    for r in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")):
        p = Path(r["path"])
        cache = NPY / f"{pcq.frame_key(p)}__d336q4f16.npy"
        if cache.exists():
            frames.append((r, np.load(cache).astype(np.float32)))
    print(f"考卷 {len(frames)} 帧（gate={args.gate}，guided r={GUIDE_R}/eps={GUIDE_EPS}）")

    if args.grid:
        gates = [float(x) for x in args.grid.split(",")]
        print(f"warp 一次性门重标定：网格 {gates}；选择规则（预注册，与 0.08 原裁定"
              f"同型）= L 覆盖最大 且 R dev p50≈0（无晕影污染迹象），并列取 R p90 小者。"
              f"映射 kink 不参与扫描。")
        warp_maps = []
        gold = {}
        denom = Counter()
        for r, m336 in frames:
            stem = pcq.frame_key(Path(r["path"]))
            rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
            warped = cv2.remap(rgb, map_x, my_warp, cv2.INTER_LINEAR)
            mw = dg.infer_map(sess336, warped, dg.DEFAULT_SHORT)
            warp_maps.append((r, stem, cv2.remap(mw, map_x, my_unwarp, cv2.INTER_LINEAR)))
            for side in ("L", "R"):
                if r[side.lower() + "cls"] != "skip":
                    gold[(stem, side)] = gold_lane(r, side.lower())
                    denom[side] += 1
        for g in gates:
            cov, devs, bads = Counter(), {"L": [], "R": []}, {"L": 0, "R": 0}
            for r, stem, m in warp_maps:
                rd = dg.reading_from_map(m, CAL, EGO, gate=g)
                for side, lane in (("L", rd.left_edge_lane), ("R", rd.right_edge_lane)):
                    glane, _ = gold.get((stem, side), (None, None))
                    if glane is None or lane is None or lane != lane:
                        continue
                    cov[side] += 1
                    d = float(lane - glane)
                    devs[side].append(d)
                    bads[side] += abs(d) > 0.5

            def pct(xs, q):
                xs = sorted(xs)
                return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]
            ld, rd_ = devs["L"], devs["R"]
            print(f"gate={g:<5} L {cov['L']:>2}/{denom['L']}  R {cov['R']:>2}/{denom['R']}  "
                  f"Ldev {pct(ld,.5):+.2f}/{pct([abs(x) for x in ld],.9):.2f}"
                  f"（坏{(bads['L']/len(ld) if ld else 0):.0%}）  "
                  f"Rdev {pct(rd_,.5):+.2f}/{pct([abs(x) for x in rd_],.9):.2f}"
                  f"（坏{(bads['R']/len(rd_) if rd_ else 0):.0%}）")
        return

    def maps_for(rgb, m336):
        out = {}
        warped = cv2.remap(rgb, map_x, my_warp, cv2.INTER_LINEAR)
        mw = dg.infer_map(sess336, warped, dg.DEFAULT_SHORT)
        out["warp"] = cv2.remap(mw, map_x, my_unwarp, cv2.INTER_LINEAR)
        I = rgb.astype(np.float32) / 255.0
        scale = max(float(m336.max()), 1e-6)
        out["guided"] = guided_filter(I, m336 / scale, GUIDE_R, GUIDE_EPS) * scale
        out["ref448"] = dg.infer_map(sess448, rgb, 448)
        return out

    variants = ("base", "warp", "guided", "ref448")
    cov = {v: Counter() for v in variants}
    devs = {v: {"L": [], "R": []} for v in variants}
    bads = {v: {"L": 0, "R": 0} for v in variants}
    sides2 = Counter()
    denom = Counter()
    rows_csv = []
    t0 = time.perf_counter()
    for r, m336 in frames:
        rgb = cv2.cvtColor(cv2.imread(str(r["path"])), cv2.COLOR_BGR2RGB)
        vmaps = {"base": m336, **maps_for(rgb, m336)}
        gold = {}
        for side in ("L", "R"):
            if r[side.lower() + "cls"] != "skip":
                gold[side] = gold_lane(r, side.lower())
                denom[side] += 1
        for v, m in vmaps.items():
            if args.gate_l is not None:
                rd_l = dg.reading_from_map(m, CAL, EGO, gate=args.gate_l)
                rd_r = dg.reading_from_map(m, CAL, EGO, gate=args.gate_r)
                lanes = (("L", rd_l.left_edge_lane), ("R", rd_r.right_edge_lane))
                n_side = sum(x is not None for _, x in lanes)
            else:
                rd = dg.reading_from_map(m, CAL, EGO, gate=args.gate)
                n_side = sum(x is not None for x in (rd.left_edge_lane, rd.right_edge_lane))
                lanes = (("L", rd.left_edge_lane), ("R", rd.right_edge_lane))
            if n_side == 2:
                sides2[v] += 1
            for side, lane in lanes:
                if side not in gold or lane is None or lane != lane:
                    continue
                glane, _y0 = gold[side]
                if glane is None:
                    continue
                cov[v][side] += 1
                d = float(lane - glane)
                devs[v][side].append(d)
                bads[v][side] += abs(d) > 0.5
                rows_csv.append({"stem": pcq.frame_key(r and Path(r["path"])),
                                 "variant": v, "side": side, "dev": round(d, 4)})

    def pct(xs, q):
        xs = sorted(xs)
        return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]

    n = len(frames)
    print(f"\n{'变体':<8}{'L覆盖':>8}{'R覆盖':>8}{'双侧':>6}"
          f"{'Ldev p50':>10}{'L p90':>8}{'L坏':>6}{'Rdev p50':>10}{'R p90':>8}{'R坏':>6}")
    for v in variants:
        ld, rd_ = devs[v]["L"], devs[v]["R"]
        print(f"{v:<9}{cov[v]['L']:>4}/{denom['L']:<3}{cov[v]['R']:>4}/{denom['R']:<3}"
              f"{sides2[v]:>4}/{n:<3}"
              f"{pct(ld, .5):>+10.2f}{pct([abs(x) for x in ld], .9):>8.2f}"
              f"{(bads[v]['L'] / len(ld) if ld else 0):>6.0%}"
              f"{pct(rd_, .5):>+10.2f}{pct([abs(x) for x in rd_], .9):>8.2f}"
              f"{(bads[v]['R'] / len(rd_) if rd_ else 0):>6.0%}")
    print(f"\n（覆盖分母=金标该侧在场帧数；dev=读数−金标 车道量；±0.3≈半车身，"
          f">0.5 算坏；总耗时 {time.perf_counter()-t0:.0f}s）")

    with (pd.OUT / "mech_score.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["stem", "variant", "side", "dev"])
        wr.writeheader()
        wr.writerows(rows_csv)
    print(f"逐帧明细：{pd.OUT / 'mech_score.csv'}")


if __name__ == "__main__":
    main()
