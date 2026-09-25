# -*- coding: utf-8 -*-
"""读数 B（形状/拐点）可行性验证：2×2 矩阵的 [原始输入 × 形状读数] 格。

协议见 README「任务③预注册协议」。本探针在 4 个法证帧上验证 B 读数的可实施性
并出图给维护者目检：r 剖面上标出 金标（绿）/ A 门限内沿（蓝）/ B 梯度峰（红，
不稳定时黄）/ 次峰（橙），附峰统计文本。不进 dev 大表（44 帧全量是下一步）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_shape_readout.py \
        [--frames 000906,001160,000500,001220] [--side L]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402
import probe_crop_quality as pcq  # noqa: E402

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

from probe_mech_score import gold_x_at  # noqa: E402
from probe_warp_forensics import row_r  # noqa: E402

KS = (9, 15, 21)          # 平滑扰动组（协议预注册）
WIN = 200                 # 峰搜索窗（块内沿 ±200px）


def smooth(x: np.ndarray, k: int) -> np.ndarray:
    return np.convolve(x, np.ones(k) / k, mode="same")


def peak_stats(prof: np.ndarray, x0: int):
    """按协议记：峰位（k=15）、显著性、FWHM、唯一性、三平滑峰位抖动。"""
    sm = smooth(prof, 15)
    g = np.abs(np.gradient(sm))
    ip = int(np.argmax(g))
    med = float(np.median(g)) + 1e-9
    prom = float(g[ip] / med)
    half = g[ip] / 2
    l = ip
    while l > 0 and g[l] > half:
        l -= 1
    r = ip
    while r < len(g) - 1 and g[r] > half:
        r += 1
    fwhm = r - l
    second = 0.0
    for i in range(len(g)):
        if abs(i - ip) > 30 and g[i] > second:
            second = float(g[i])
    unique = second < 0.6 * g[ip]
    jps = []
    for k in KS:
        gk = np.abs(np.gradient(smooth(prof, k)))
        jps.append(int(np.argmax(gk)))
    jitter = max(jps) - min(jps)
    return {"ip": ip, "prom": prom, "fwhm": fwhm, "unique": unique,
            "jitter": jitter, "pos": jps}


def run_all(side: str) -> None:
    """44 帧全量统计（协议预注册）：B 峰统计 + 与 A（生产读数）同帧集对照。"""
    CAL = load_calib()
    EGO = dg.DepthRoadObserver._load_ego_mask()
    tkey = 4 if side == "L" else 5
    labels = [r for r in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))]
    sess = dg.load_session(__import__("maaracing_master.plugins.speedrush",
                                      fromlist=["DEPTH_MODEL_FILE"]).DEPTH_MODEL_FILE)
    n_block = n_stable = 0
    devs_b, devs_a = [], []
    bad_b = 0
    for r in labels:
        p = Path(r["path"])
        cache = pd.OUT / "npy" / f"{pcq.frame_key(p)}__d336q4f16.npy"
        if not cache.exists():
            continue
        m = np.load(cache).astype(np.float32)
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        rd = dg.reading_from_map(m, CAL, EGO, gate=dg.GATE)
        lane_a = rd.left_edge_lane if side == "L" else rd.right_edge_lane
        _rr, blocks = dg._rel_and_blocks(m, EGO, gate=dg.GATE)
        cands = sorted((b for b in blocks if b[tkey]),
                       key=lambda b: b[1] - b[0], reverse=True)
        if not cands:
            continue
        b = cands[0]
        inner = b[2] if side == "L" else b[3]
        src = [y for y in inner if y - CAL.y_h >= dg.FIT_FLOOR_PX]
        if not src:
            continue
        y_ref = int(np.median(src))
        ix = inner[y_ref]
        lo = int(max(15, ix - WIN))
        hi = int(min(1265, ix + WIN))
        st = peak_stats(row_r(m, y_ref)[lo:hi], lo)
        n_block += 1
        ok = (st["unique"] and st["jitter"] <= 10 and st["prom"] >= 3.0
              and 15 <= st["ip"] <= (hi - lo) - 15)   # 峰不得贴窗缘（v0 教训）
        gx = gold_x_at(r, side.lower(), y_ref)
        if gx is None or not (0 <= gx <= 1279):
            continue
        gold_lane_v = x_lane_of(int(round(gx)), y_ref, CAL)
        if ok:
            n_stable += 1
            d = x_lane_of(lo + st["ip"], y_ref, CAL) - gold_lane_v
            devs_b.append(d)
            bad_b += abs(d) > 0.5
        if lane_a is not None and lane_a == lane_a:
            devs_a.append(lane_a - gold_lane_v)

    def pct(xs, q):
        xs = sorted(xs)
        return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]
    print(f"[{side}] 块存在（分母）={n_block}  B 稳定峰={n_stable}（{n_stable/max(n_block,1):.0%}）"
          f"  B dev p50/p90={pct(devs_b,.5):+.2f}/{pct([abs(x) for x in devs_b],.9):.2f}"
          f"（坏{bad_b/max(len(devs_b),1):.0%}，n={len(devs_b)}）"
          f"  A 同帧集 dev p50/p90={pct(devs_a,.5):+.2f}/{pct([abs(x) for x in devs_a],.9):.2f}"
          f"（n={len(devs_a)}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="000906,001160,000500,001220")
    ap.add_argument("--side", default="L")
    ap.add_argument("--all", action="store_true", help="44 帧全量统计（L+R）")
    args = ap.parse_args()
    if args.all:
        run_all("L")
        run_all("R")
        return
    side = args.side.upper()
    tkey = 4 if side == "L" else 5
    CAL = load_calib()
    EGO = dg.DepthRoadObserver._load_ego_mask()

    labels = {Path(r["path"]).stem: r for r in
              csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))}
    rows = []
    for raw in args.frames.split(","):
        hits = [s for s in labels if s.endswith(raw)]
        if not hits:
            continue
        stem = hits[0]
        r = labels[stem]
        m = np.load(pd.OUT / "npy" / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy").astype(np.float32)
        _r, blocks = dg._rel_and_blocks(m, EGO, gate=dg.GATE)
        cands = sorted((b for b in blocks if b[tkey]),
                       key=lambda b: b[1] - b[0], reverse=True)
        if not cands:
            print(f"{stem}: 无触边块"); continue
        b = cands[0]
        inner = b[2] if side == "L" else b[3]
        src = [y for y in inner if y - CAL.y_h >= dg.FIT_FLOOR_PX]
        if not src:
            print(f"{stem}: 块无源行"); continue
        y_ref = int(np.median(src))
        ix = inner[y_ref]
        prof_full = row_r(m, y_ref)
        lo = int(max(0, ix - WIN))
        hi = int(min(1280, ix + WIN))
        prof = prof_full[lo:hi]
        st = peak_stats(prof, lo)
        gx = gold_x_at(r, side.lower(), y_ref)
        px_gold = None if gx is None else int(round(gx)) - lo
        dev_lane = None
        if gx is not None:
            lane = x_lane_of(lo + st["ip"], y_ref, CAL)
            dev_lane = lane - x_lane_of(int(round(gx)), y_ref, CAL)
        ok = st["unique"] and st["jitter"] <= 10 and st["prom"] >= 3.0
        rows.append((stem, y_ref, ix, prof, st, px_gold, dev_lane, lo, ok))
        print(f"{stem}: y_ref={y_ref} 内沿x={ix} 峰x={lo + st['ip']} "
              f"显著性={st['prom']:.1f} FWHM={st['fwhm']} 唯一={st['unique']} "
              f"抖动={st['jitter']}px 稳定峰={ok} dev={dev_lane if dev_lane is None else round(dev_lane, 2)}车道")

    # 渲染：每帧一条剖面带标注
    PH, PW = 300, 3400
    panels = []
    for stem, y_ref, ix, prof, st, px_gold, dev_lane, lo, ok in rows:
        p = np.zeros((PH, PW, 3), np.uint8)
        sm = smooth(prof, 15)
        RMAX = 0.45
        n = len(prof)

        def to_px(i, v):
            return (int(i / max(n - 1, 1) * (PW - 160)) + 100,
                    int(PH * 0.8 - min(max(float(v), -0.05), RMAX) / RMAX * PH * 0.7))

        cv2.polylines(p, [np.array([to_px(i, v) for i, v in enumerate(sm)])],
                      False, (255, 255, 255), 2)
        py = int(PH * 0.8)
        cv2.line(p, (100, py), (PW - 60, py), (128, 128, 128), 1)
        for gate, gc in ((0.08, (0, 0, 255)), (0.05, (0, 128, 255))):
            gy = int(PH * 0.8 - gate / RMAX * PH * 0.7)
            cv2.line(p, (100, gy), (PW - 60, gy), gc, 1)
        if px_gold is not None and 0 <= px_gold < n:
            cv2.line(p, to_px(px_gold, 0), (to_px(px_gold, 0)[0], int(PH * 0.1)),
                     (0, 255, 0), 2)
            cv2.putText(p, "gold", (to_px(px_gold, 0)[0] - 20, int(PH * 0.08)),
                        cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 255, 0), 1)
        cv2.line(p, to_px(ix - lo, 0), (to_px(ix - lo, 0)[0], int(PH * 0.2)),
                 (255, 0, 0), 2)
        cv2.putText(p, "A inner", (to_px(ix - lo, 0)[0] - 30, int(PH * 0.18)),
                    cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 0, 0), 1)
        col = (0, 255, 0) if ok else (0, 255, 255)
        cv2.line(p, to_px(st["ip"], 0), (to_px(st["ip"], 0)[0], int(PH * 0.35)),
                 col, 2)
        cv2.putText(p, "B peak" + ("" if ok else " (unstable)"),
                    (to_px(st["ip"], 0)[0] - 30, int(PH * 0.33)),
                    cv2.FONT_HERSHEY_SIMPLEX, .5, col, 1)
        cv2.putText(p, f"{stem} y0={y_ref} dev={dev_lane if dev_lane is None else round(dev_lane, 2)}",
                    (100, int(PH) - 10), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 1)
        panels.append(p)
    if panels:
        while len(panels) < 4:
            panels.append(np.zeros_like(panels[0]))
        g = np.vstack(panels)
        out = pd.OUT / f"shape_readout_{args.side}.jpg"
        cv2.imwrite(str(out), g, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"已出：{out}")


if __name__ == "__main__":
    main()
