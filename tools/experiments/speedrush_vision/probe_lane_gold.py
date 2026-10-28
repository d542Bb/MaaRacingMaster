# -*- coding: utf-8 -*-
"""S0 离线车道线 gold：从 raw 帧拟合左右黄色路缘，产出真值横位/航向。

**为什么不用 YOLOX**：YOLOX 只有 coin/car/bonus 三类，没有车道线；gold 走
经典 CV（HSV 黄线阈值 + 直线拟合），只做离线标定/验证，不进生产。

**坐标**：4 车道路心=中间分隔线（lane 0），左路缘=-2、右路缘=+2。
地面直线在图上满足 ``x_px = vpx + L*(y_px - y_h)/cam_h``，斜率
``dx/dy = L/cam_h``，因此单条路缘线的斜率直接给出它相对相机轴的
横向偏移 L（道）；自车横位 ``x = k - L``（左缘 k=-2，右缘 k=+2）。

**输出**：``<out>/gold_<race>.jsonl`` 每采样帧一行；``<out>/gold_<race>.jpg``
抽帧叠图（gold 线 + trace 的模型/观测/生产位）；stdout 打误差分账。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.planner import step  # noqa: E402

VPX = 640.7
Y_H = 354.2
LANE_W = 3.38
# 线的横向偏移合法域（道，相对相机轴）：左右路缘各留一车道余量
FONT = "C:/Windows/Fonts/msyh.ttc"


def _font(size: int):
    return ImageFont.truetype(FONT, size)


def _yellow_mask(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # 只留高饱和高亮的纯黄；路面色/夕照/护栏褐黄都低于这条线
    mask = cv2.inRange(hsv, (18, 150, 140), (40, 255, 255))
    mask[: int(Y_H) + 30] = 0          # 地平线以上/远处不作为路缘证据
    return mask


def _search_line(mask: np.ndarray, k: float, x_lo: float, x_hi: float,
                 cam_h: float, f: float = 807.7) -> dict | None:
    """投影线搜索：候选 (x, theta) 把 k 处的车道线投到画面，按到黄色像素的
    距离变换打分（连续分数，0.02 道级分辨率）。

    比"最大黄色连通块"稳：黄色护栏/路缘/金币是别的偏移上的线或短块，
    只有与路面黄线重合的 (x, theta) 能在近场形成连续、近零距离的覆盖。"""
    ys = np.arange(565, 719, 3)
    dist = cv2.distanceTransform(255 - mask, cv2.DIST_L2, 3)
    dist_rows = dist[ys]                      # (n_rows, width)
    width = mask.shape[1]

    def score(x: float, th: float) -> float:
        vp = VPX - f * math.tan(th)
        cxs = vp + ((k - x) * LANE_W) * (ys - Y_H) / cam_h
        d = np.zeros(len(ys))
        for r, cx in enumerate(cxs):
            xi = int(round(cx))
            xi = min(max(xi, 0), width - 1)
            d[r] = dist_rows[r, xi]
        return float(np.mean(np.maximum(0.0, 1.0 - np.minimum(d, 8.0) / 8.0)))

    def coarse(x_lo, x_hi, th_lo, th_hi, x_steps, th_steps):
        best = (-1.0, x_lo, 0.0)
        for x in np.linspace(x_lo, x_hi, x_steps):
            for th in np.linspace(th_lo, th_hi, th_steps):
                s = score(x, th)
                if s > best[0]:
                    best = (s, float(x), float(th))
        return best

    c = coarse(x_lo, x_hi, -0.05, 0.05, 31, 11)
    fine = coarse(c[1] - 0.12, c[1] + 0.12, c[2] - 0.008, c[2] + 0.008, 13, 9)
    if fine[0] < 0.35:
        return None
    return {"cov": round(float(fine[0]), 3), "x": float(fine[1]),
            "theta": float(fine[2])}


def fit_gold(img: np.ndarray, cam_h: float) -> dict:
    """左右路缘投影搜索 → 自车横位（道，右正）与航向（rad，正=车头偏右）。

    左缘 x 约束 [-2, +0.5]、右缘 [-0.5, +2]：左缘必须真在车左侧、右缘在右侧，
    防止把对侧黄线当成这一侧（旧版 4 道宽搜索的主要野值来源）。"""
    mask = _yellow_mask(img)
    left = _search_line(mask, -2.0, -2.0, 0.5, cam_h)
    right = _search_line(mask, +2.0, -0.5, 2.0, cam_h)
    # 右缘搜索常会再次命中同一条左缘线：此时 x_right = x_left + 4（k 差 4 道）
    if left is not None and right is not None \
            and abs(right["x"] - (left["x"] + 4.0)) < 0.2:
        right = None
    out = {"cam_h": cam_h, "left": left, "right": right,
           "x_left": None if left is None else left["x"],
           "x_right": None if right is None else right["x"],
           "x": None, "theta": None, "vp_x": None, "conf": 0.0}
    xs = [v for v in (out["x_left"], out["x_right"]) if v is not None]
    if xs:
        out["x"] = float(np.mean(xs))
        covs = [v["cov"] for v in (left, right) if v is not None]
        out["conf"] = float(np.mean(covs))
    ths = [v["theta"] for v in (left, right) if v is not None]
    if ths:
        out["theta"] = float(np.mean(ths))
    return out


def _load_rows(run: Path) -> list[dict]:
    p = next(run.glob("trace_*.jsonl"))
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    t = 0.0
    for r in rows:
        r["_t"] = t
        t += r.get("dt") or 0.05
    return rows


def model_replay(rows: list[dict], planner) -> tuple[list[float], float]:
    x = v = st = 0.0
    xs = []
    for r in rows:
        dt = r.get("dt") or 0.05
        u = r.get("steer_raw") if r.get("steer_raw") is not None else 0.0
        x, v, st = step(x, v, st, u, dt, planner)
        xs.append(x)
    i0 = next((i for i, r in enumerate(rows)
               if r.get("road_offset") is not None), 0)
    return xs, rows[i0].get("road_offset") or 0.0


def _line_xy(x: float, k: float, cam_h: float):
    off = (k - x) * LANE_W
    return [(VPX + off * (cy - Y_H) / cam_h, cy) for cy in range(int(Y_H) + 10, 720, 4)]


def _draw_line(d, x, k, cam_h, color, width=3):
    pts = _line_xy(x, k, cam_h)
    vis = [p for p in pts if -20 <= p[0] <= 1300]
    if len(vis) > 2:
        d.line(vis, fill=color, width=width)


def overlay(run: Path, idx: int, gold: dict, row: dict, model_x: float,
            out_path: Path) -> None:
    img = Image.open(run / "raw" / f"{idx + 1:04d}_raw.jpg").convert("RGB")
    d = ImageDraw.Draw(img)
    cam_h = gold["cam_h"]
    d.rectangle([0, 0, 520, 126], fill=(0, 0, 0))
    d.text((6, 4), f"{run.name} idx {idx}", font=_font(18), fill=(255, 255, 255))
    d.text((6, 28), f"gold {gold['x']}  model {model_x:+.2f}", font=_font(16),
           fill=(0, 255, 0))
    d.text((6, 50), f"obs {row.get('road_offset')}  prod {row.get('executed_lane')}",
           font=_font(16), fill=(255, 230, 0))
    d.text((6, 72), f"theta {gold['theta']}  conf {gold['conf']}",
           font=_font(16), fill=(0, 200, 255))
    if gold["x"] is not None:
        _draw_line(d, gold["x"], -2, cam_h, (0, 255, 0), 5)
        _draw_line(d, gold["x"], 2, cam_h, (0, 255, 0), 2)
    if gold["x_left"] is not None:
        _draw_line(d, gold["x_left"], -2, cam_h, (0, 160, 255), 2)
    if gold["x_right"] is not None:
        _draw_line(d, gold["x_right"], 2, cam_h, (0, 160, 255), 2)
    if row.get("road_offset") is not None:
        _draw_line(d, row["road_offset"], -2, cam_h, (255, 0, 255), 2)
    if row.get("executed_lane") is not None:
        _draw_line(d, row["executed_lane"], -2, cam_h, (255, 230, 0), 2)
    img.save(out_path, quality=90)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="debug 会话目录")
    ap.add_argument("--step", type=int, default=5, help="采样步长（拍）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    run = Path(args.dir)
    out = Path(args.out) if args.out else run / "gold"
    out.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(run)
    cfg = load_decision()
    xs, ro0 = model_replay(rows, cfg.planner)
    recs = []
    for i in range(0, len(rows), max(1, args.step)):
        raw = run / "raw" / f"{i + 1:04d}_raw.jpg"
        if not raw.exists():
            continue
        img = cv2.imread(str(raw))
        if img is None:
            continue
        cam_h = rows[i].get("dgeo_cam_h") or 1.469
        g = fit_gold(img, cam_h)
        r = rows[i]
        lw = (r.get("scene") or {}).get("lane_w") or LANE_W
        far = None
        for c in (r.get("cor_far") or []):
            if c[2] is not None and c[3] is not None:
                w = (c[3] - c[2]) / lw
                if 3.0 <= w <= 6.0:
                    far = -(0.5 * (c[2] + c[3])) / lw
                    break
        rec = {"idx": i, "fid": r.get("fid"), "t_s": round(r["_t"], 3),
               "gold": g["x"], "x_left": g["x_left"], "x_right": g["x_right"],
               "theta": g["theta"], "conf": g["conf"],
               "model": ro0 + xs[i], "obs": r.get("road_offset"),
               "prod": r.get("executed_lane"), "far": far,
               "src": r.get("ro_source")}
        recs.append(rec)
        if i in (20, 100, 155, 300, 460) or len(recs) <= 3:
            overlay(run, i, g, r, ro0 + xs[i], out / f"overlay_{i:04d}.jpg")
    path = out / f"gold_{run.name}.jsonl"
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in recs),
                    encoding="utf-8")
    vals = [r for r in recs if r["gold"] is not None]
    print(f"{run.name}: {len(recs)} samples, gold coverage {len(vals)/max(1,len(recs)):.0%}")
    for key in ("model", "obs", "prod", "far"):
        errs = [abs(r["gold"] - r[key]) for r in vals if r[key] is not None]
        if errs:
            errs.sort()
            print(f"  |{key:5s}-gold| p50 {errs[len(errs)//2]:.2f}  "
                  f"p90 {errs[int(.9*len(errs))]:.2f}  n {len(errs)}")
    thetas = [abs(r["theta"]) for r in vals if r["theta"] is not None]
    if thetas:
        thetas.sort()
        print(f"  |theta| p50 {thetas[len(thetas)//2]:.3f} rad  p90 "
              f"{thetas[int(.9*len(thetas))]:.3f}")
    print("wrote", path)


if __name__ == "__main__":
    main()
