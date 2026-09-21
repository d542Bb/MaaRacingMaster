"""yield 预分析的家庭构成复核（身份先于统计量，检查表 Q3）。

yield_scan 的 ≥1/≥3 段占比把黄族高光算进了「虚线语义检出」——补采靶点不能建在
混池上。本脚本按速度带抽**直道∧高段数**帧，逐段画框出蒙太奇，人眼终审每段
身份（真虚线 / 黄边高光 / 景物），给出「真虚线密度 vs 速度带」的干净表。

用法：.venv\\Scripts\\python.exe tools/experiments/speedrush_calibration/probe_gate3_yieldfam.py
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import DEMOS, CACHE, analyze_series, load_base, score_intervals, staged_filter
from probe_gate2_hist import get_cal
import probe_gate3_0_object as o3

BINS = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 999)]
PER_BIN = 3


def main():
    det = json.loads((CACHE / "yield_scan.json").read_text(encoding="utf-8"))
    by_sess = {}
    for d in det:
        by_sess.setdefault(d["sess"], []).append(d)
    base = load_base()
    tiles = []
    for lo, hi in BINS:
        cands, cands_curve = [], []
        for s, ds in by_sess.items():
            for d in ds:
                if d["rate"] is not None and lo <= d["rate"] < hi and d["n_sel"] >= 2:
                    (cands if d["straight"] else cands_curve).append((s, d["seq"], d["n_sel"], d["rate"]))
        if len(cands) < 2:                      # 雨景直道门（hough 矢高）失效——退回弯道并标注
            cands = [(s_, q_, n_, r_) for s_, q_, n_, r_ in cands_curve][:PER_BIN]
        cands.sort(key=lambda x: -x[2])
        picked, seen = [], set()
        for s, q, n, rt in cands:
            if s in seen:
                continue
            seen.add(s)
            picked.append((s, q, n, rt))
            if len(picked) >= PER_BIN:
                break
        if not picked:
            tiles.append((f"{lo}-{hi}: 无直道高检出帧", None))
            continue
        ims = []
        for s, q, n, rt in picked:
            rows, _ = analyze_series(s, 2)
            rows = staged_filter(rows, base["base"], score_intervals(s))
            r = next(x for x in rows if x["seq"] == q)
            cal = get_cal(s, [x for x in rows if x["stage"] == 4])
            rgb = np.array(Image.open(DEMOS / s / "frames" / r["file"]).convert("RGB"))
            im = Image.fromarray(rgb).resize((640, 360))
            dr = ImageDraw.Draw(im)
            for x in o3.classify(rgb, cal["y_h"], cal["vpx"], frontend="dash", ax=cal["A_x_used"]):
                if not x["selected"]:
                    continue
                u1, v1, u2, v2 = x["sg"]
                sc = 0.5
                dr.rectangle([(min(u1, u2) * sc - 2, min(v1, v2) * sc - 2),
                              (max(u1, u2) * sc + 2, max(v1, v2) * sc + 2)],
                             outline=(0, 255, 0), width=2)
            dr.rectangle([(0, 0), (640, 16)], fill=(0, 0, 0))
            dr.text((4, 2), f"{s} seq{q} n={n} rate={rt:.0f}", fill=(255, 255, 0))
            ims.append(im)
        strip = Image.new("RGB", (640 * len(ims), 360), (16, 16, 16))
        for i, im in enumerate(ims):
            strip.paste(im, (640 * i, 0))
        tiles.append((f"速度带 {lo}-{hi}（直道，段数降序 {len(ims)} 帧）", strip))
    W = 640 * PER_BIN
    H = 360 * len(tiles) + 22 * len(tiles)
    canvas = Image.new("RGB", (W, H), (24, 24, 24))
    dr = ImageDraw.Draw(canvas)
    y = 0
    for tag, im in tiles:
        dr.text((4, y + 4), tag, fill=(255, 255, 255))
        y += 22
        if im:
            canvas.paste(im, (0, y))
        y += 360
    p = CACHE / "yieldfam_montage.png"
    canvas.save(p)
    print(f"家庭构成蒙太奇：{p}")


if __name__ == "__main__":
    main()
