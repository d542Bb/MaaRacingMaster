"""预训练单目深度在《巅峰极速》画面上的可行性探针（C 类，2026-09-23）。

**要回答的问题**（维护者 2026-09-23 提出"伪遥测 lite"设想：深度切地面/护栏、
检测器给目标，二者结合扩 WorldObservation 信息面，不新增标注训练）：
  ① 深度图在真实坏帧（badframes_*，R2 撞墙复盘素材）上，地面/非地面是否分得开？
  ② 相邻帧切分是否稳定（单帧模型逐帧抖是已知病）？
  ③ 检测框 ∩ 深度 的目标远近序是否与 cy 序一致（远带 y≈213 附近有没有分辨率）？
  ④ DirectML 上的单帧时延（帧预算 66ms，现有检测占 5.4ms）。

**候选模型**：onnx-community 官方转换（HF）：
  - da2_small：Depth Anything V2 Small，Apache-2.0（生产可行档）
  - da2_base：Depth Anything V2 Base，CC-BY-NC（仅离线质量天花板，不得进包）
  权重放 `%APPDATA%/MaaRacingMaster/data/speedrush/depth_review/weights/`（不入库）。

**预处理口径**（对齐各仓库 preprocessor_config.json，DPTImageProcessor）：
  短边 518、保持长宽比、取 14 的倍数（1280×720 → 924×518）、/255、
  mean=[.485,.456,.406] std=[.229,.224,.225]。输出为**相对视差**（大=近）。

**标尺独立性**：坏帧集来自 2026-09-22 撞墙复盘的 overlay 目视定名（非本实验生成）；
深度模型是外部预训练权重，与本仓任何实现无关。类别归因读数（哪类污染物）为
**人眼目视判读，非机检判据**。

用法（仓库根，.venv Python）：
    P=tools/experiments/speedrush_vision/probe_depth.py
    python $P infer --model small --set bad      # 全部 72 坏帧
    python $P infer --model small --set ctrl     # 对照好帧（20260922_113724_p1）
    python $P stats --model small                # 读数②③（需先 infer）
    python $P sheet  --model small               # 出拼图（目视判据用）
    python $P lat                                # 读数④：分辨率-时延扫描
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_old_model import detect  # noqa: E402  同目录、自包含约定内复用检测预处理

APP = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
OUT = APP / "depth_review"
WEIGHTS = OUT / "weights"
MODELS = {"small": WEIGHTS / "da2_small.onnx", "base": WEIGHTS / "da2_base.onnx"}
DETECT_ONNX = (Path(__file__).resolve().parents[3] / "archive" / "racing"
               / "resources" / "onnx" / "model.onnx")

CTRL_SESSION = APP / "demos" / "20260922_113724_p1" / "frames"
CTRL_STAB = list(range(100, 141))          # 稳定窗：连续 41 帧（读数②）
CTRL_SAMPLE = list(range(100, 701, 20))    # 宽采样（约 31 帧）

# 边界层宽 HSV 口径（home: plugins/speedrush/boundary.py `_HSV_*`，此处仅复刻用于分组统计）
HSV_LO, HSV_HI = np.array([8, 45, 80], np.uint8), np.array([32, 255, 255], np.uint8)
# A1 尺子冻结常数（home: resources/calibration/gate0.json，[实测·待复核] 38 场总中位）
VPX, Y_H = 640.7, 324.2


def _resize_hw(h: int, w: int, short: int) -> tuple[int, int]:
    """短边缩到 short、各边取 14 的倍数（DPTImageProcessor 同口径）。"""
    r = short / min(h, w)
    nh, nw = int(round(h * r)), int(round(w * r))
    return nh - nh % 14, nw - nw % 14


def preprocess(rgb: np.ndarray, short: int = 518) -> np.ndarray:
    nh, nw = _resize_hw(rgb.shape[0], rgb.shape[1], short)
    img = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    mean = np.array([.485, .456, .406], np.float32)
    std = np.array([.229, .224, .225], np.float32)
    return ((img - mean) / std).transpose(2, 0, 1)[None]


def depth_map(sess, rgb: np.ndarray, short: int = 518) -> np.ndarray:
    """→ float32 视差图（大=近），已上采样回原帧尺寸。"""
    blob = preprocess(rgb, short)
    d = sess.run(None, {"pixel_values": blob})[0][0]
    d = np.nan_to_num(d.astype(np.float32), nan=0.0)
    return cv2.resize(d, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def ground_line(d: np.ndarray) -> np.ndarray:
    """逐行地面视差估计 g(y)：对 y∈[Y_H+16, 715] 行取中位数，剔除自车列带 [540,740]。
    中位数对 <50% 面积的干扰（他车/自车）稳健——路在下方画面占绝对多数。"""
    g = np.full(d.shape[0], np.nan)
    lo = int(Y_H + 16)
    for y in range(lo, min(716, d.shape[0])):
        row = np.concatenate([d[y, :540], d[y, 740:]])
        g[y] = float(np.median(row))
    return g


def frame_key(p: Path) -> str:
    return f"{p.parent.name}__{p.stem}"


def set_frames(which: str) -> list[Path]:
    if which == "bad":
        return sorted((APP / "control_traces").glob("badframes_*/*.jpg"))
    if which == "ctrl":
        return [CTRL_SESSION / f"{i:06d}.jpg" for i in CTRL_SAMPLE]
    if which == "stab":
        return [CTRL_SESSION / f"{i:06d}.jpg" for i in CTRL_STAB]
    raise SystemExit(f"未知帧集 {which}")


def cmd_infer(args) -> None:
    sess = ort.InferenceSession(str(MODELS[args.model]), providers=["DmlExecutionProvider"])
    (OUT / "maps").mkdir(parents=True, exist_ok=True)
    (OUT / "npy").mkdir(parents=True, exist_ok=True)
    times = []
    for p in set_frames(args.set):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter()
        d = depth_map(sess, rgb, args.short)
        times.append(time.perf_counter() - t0)
        k = frame_key(p)
        np.save(OUT / "npy" / f"{k}__{args.model}.npy", d.astype(np.float16))
        # 并排图：左原帧、右伪彩（JET，蓝=远红=近，按本帧 1/99 分位拉对比度）
        lo, hi = np.percentile(d, [1, 99])
        du = np.clip((d - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
        panel = cv2.applyColorMap(du, cv2.COLORMAP_JET)
        both = np.hstack([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), panel])
        cv2.imwrite(str(OUT / "maps" / f"{k}__{args.model}.jpg"), both)
    ms = np.median(times) * 1000
    print(f"[infer:{args.model}/{args.set}] n={len(times)} median={ms:.1f}ms "
          f"p90={np.percentile(times, 90) * 1000:.1f}ms")


def cmd_stats(args) -> None:
    npy = OUT / "npy"
    # ---- 读数②：相邻帧切分稳定性（稳定窗，同模型）----
    pairs = []
    for a, b in zip(CTRL_STAB, CTRL_STAB[1:]):
        ka = f"{CTRL_SESSION.name}__{a:06d}__{args.model}.npy"
        kb = f"{CTRL_SESSION.name}__{b:06d}__{args.model}.npy"
        if not (npy / ka).exists() and not (npy / kb).exists():
            continue
        da = np.load(npy / (ka if (npy / ka).exists() else kb)).astype(np.float32)
        db = np.load(npy / kb).astype(np.float32)
        na = (da - np.percentile(da, 1)) / max(np.percentile(da, 99) - np.percentile(da, 1), 1e-6)
        nb = (db - np.percentile(db, 1)) / max(np.percentile(db, 99) - np.percentile(db, 1), 1e-6)
        ma, mb = na > 0.5, nb > 0.5                       # 二值"近"掩码（分位归一后中值切）
        iou = (ma & mb).sum() / max((ma | mb).sum(), 1)
        pairs.append((iou, float(np.abs(na - nb).mean())))
    if pairs:
        ious = np.array([x[0] for x in pairs])
        print(f"[②稳定 n={len(pairs)}帧对] 近区掩码 IoU 中位={np.median(ious):.3f} "
              f"p10={np.percentile(ious, 10):.3f} |Δ|归一={np.mean([x[1] for x in pairs]):.3f}")

    # ---- 读数③：检测框 ∩ 深度 的远近序 vs cy 序 ----
    dsess = ort.InferenceSession(str(DETECT_ONNX), providers=["DmlExecutionProvider"])
    rows = []
    for p in set_frames("bad") + set_frames("ctrl"):
        k = frame_key(p)
        f = npy / f"{k}__{args.model}.npy"
        if not f.exists():
            continue
        d = np.load(f).astype(np.float32)
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        det = [(cls, sc, box) for cls, sc, box in detect(dsess, rgb) if cls in (1, 2)]
        pts = []
        for cls, sc, (x1, y1, x2, y2) in det:
            dm = float(np.median(d[y1 + 2:y2 - 2, x1 + 2:x2 - 2])) if y2 - y1 > 8 else 0.0
            pts.append(((y1 + y2) / 2, dm))
        if len(pts) >= 3:
            ys = np.array([a for a, _ in pts])
            dp = np.array([b for _, b in pts])

            def _ranks(v):
                o = v.argsort()
                r = np.empty(len(v))
                r[o] = np.arange(len(v))
                return r
            rho = 1 - 6 * ((_ranks(ys) - _ranks(dp)) ** 2).sum() / (len(pts) * (len(pts) ** 2 - 1))
            rows.append((p.name, len(pts), rho, float(dp.min()), float(dp.max())))
    if rows:
        rhos = np.array([r[2] for r in rows])
        print(f"[③序一致 n={len(rows)}帧] 框视差 vs cy 序 Spearman 中位={np.median(rhos):.3f} "
              f"p10={np.percentile(rhos, 10):.3f} | ρ<0 占比={(rhos < 0).mean():.0%}")
        spread = np.array([r[4] - r[3] for r in rows])
        print(f"           框间视差极差中位={np.median(spread):.3f}（相对值，跨帧不可比；"
              f"看远带是否有内部排序用 ρ）")

    # ---- 读数①（机检部分）：黄像素相对地面的视差残差分布 ----
    resid_bad, resid_ok = [], []
    for p in set_frames("bad") + set_frames("ctrl"):
        f = npy / f"{frame_key(p)}__{args.model}.npy"
        if not f.exists():
            continue
        d = np.load(f).astype(np.float32)
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        ymask = cv2.inRange(hsv, HSV_LO, HSV_HI)
        g = ground_line(d)
        y0, y1 = int(Y_H + 16), 715
        ys, xs = np.where(ymask[y0:y1] > 0)
        if len(ys) < 50:
            continue
        yy = ys + y0
        rr = d[yy, xs] - g[yy]
        scale = np.nanpercentile(np.abs(d[y0:y1] - g[y0:y1][:, None]), 99)
        r = rr / max(float(scale), 1e-6)
        (resid_bad if p.parent.name.startswith("badframes") else resid_ok).append(np.median(r))
    if resid_bad:
        print(f"[①黄-地残差] 坏帧黄像素中位残差(归一) n={len(resid_bad)} "
              f"p50={np.median(resid_bad):.3f} p90={np.percentile(resid_bad, 90):.3f}")
    if resid_ok:
        print(f"             对照帧 n={len(resid_ok)} p50={np.median(resid_ok):.3f}")


def cmd_sheet(args) -> None:
    """拼图：坏帧 6 + 对照 6（3×4），目视判读"地面/墙基/污染物分不分得开"。"""
    from PIL import Image, ImageDraw, ImageFont
    cells = []
    bad = set_frames("bad")
    ctrl = set_frames("ctrl")
    for label, group in (("BAD", bad[:: max(len(bad) // 6, 1)]),
                         ("CTRL", ctrl[:: max(len(ctrl) // 6, 1)])):
        for p in group[:6]:
            k = frame_key(p)
            f = OUT / "maps" / f"{k}__{args.model}.jpg"
            if f.exists():
                cells.append((label + " " + k[:40], f))
    cols, cw, ch = 2, 1280, 360
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (cw // 2), rows * (ch + 22)), "white")
    dr = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, (name, f) in enumerate(cells):
        im = Image.open(f).resize((cw // 2, ch))
        x, y = (i % cols) * (cw // 2), (i // cols) * (ch + 22)
        sheet.paste(im, (x, y))
        dr.text((x + 4, y + ch + 2), name, fill="black", font=font)
    o = OUT / f"sheet_{args.model}.jpg"
    sheet.save(o, quality=88)
    print(f"[sheet] {o} （{len(cells)} 格：左半原帧 / 右半伪彩视差，JET 红=近 蓝=远）")


def cmd_lat(args) -> None:
    rng = np.random.default_rng(0)
    rgb = (rng.random((720, 1280, 3)) * 255).astype(np.uint8)
    print("model  short  provider  median_ms(p90)")
    for m in MODELS:
        if not MODELS[m].exists():
            continue
        for short in (518, 462, 392, 336, 280):
            for prov in (["DmlExecutionProvider"], ["CPUExecutionProvider"]):
                sess = ort.InferenceSession(str(MODELS[m]), providers=prov)
                blob = preprocess(rgb, short)
                sess.run(None, {"pixel_values": blob})
                ts = []
                for _ in range(5):
                    t0 = time.perf_counter()
                    sess.run(None, {"pixel_values": blob})
                    ts.append(time.perf_counter() - t0)
                tag = "DML" if prov[0].startswith("Dml") else "CPU"
                print(f"{m:5s} {short:5d}  {tag:8s} {np.median(ts) * 1000:6.1f} "
                      f"({np.percentile(ts, 90) * 1000:.1f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("infer", "stats", "sheet"):
        s = sub.add_parser(name)
        s.add_argument("--model", choices=list(MODELS), default="small")
        if name == "infer":
            s.add_argument("--set", choices=("bad", "ctrl", "stab"), required=True)
            s.add_argument("--short", type=int, default=518)
    sub.add_parser("lat")
    args = ap.parse_args()
    {"infer": cmd_infer, "stats": cmd_stats, "sheet": cmd_sheet, "lat": cmd_lat}[args.cmd](args)


if __name__ == "__main__":
    main()
