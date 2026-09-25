"""地面基线 A/B：逐行 q20（现产码）vs 侧倾校正基线场（候选修复原型）。

根因（2026-09-25 22:04 局证据包实证）：追逐相机随转向侧倾（急转帧 iso-深度
线斜 ~2-4°，行内视差左近右远差五成）后，逐行 q20 钉住行内最远列，其余路面
成假隆起块——实机读数 25% 非空 + 垃圾缘、车横撞马路牙子。原型：t 网格搜
「地面解释率」（rel 落在晕影带内的像素占比）显著最大者（TILT_MARGIN 余量，
平路帧宁取 t=0），基线沿等深度线取逐行 q20；t=0 与产码严格同形。

金标卷裁决（45 帧有标，gate=产码 0.08/0.10，2026-09-25）：
- 旧（row-q20）      L 43/45 dev -0.050 p90 0.73 坏6 | R 16/45 dev -0.045
  p90 0.44 坏1 | 双侧 15
- 新（margin=0.05）  L 43/45 dev -0.077 p90 1.17 坏6 | R 19/45 dev -0.062
  p90 1.34 坏3 | 双侧 18
覆盖大涨（R +19%、双侧 +20%）、中位 dev 持平，但 dev 尾部与 R 坏读变差
——0.08/0.10 门是对旧基线标定的，**落产码前须对新基线重过门网格**，并复查
518 骑缘锚点（新基线读 L -1.67 vs 金标 -0.394，骑缘姿态与侧倾耦合待查）。

用法：python tools/experiments/speedrush_vision/probe_ground_ab.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
NPY_DIR = pd.OUT / "npy"
TILT_GRID = tuple(np.linspace(-0.12, 0.12, 13))
TILT_PIVOT_X = 640.0
TILT_MARGIN = 0.05
GROUND_RESID_LO, GROUND_RESID_HI = -0.04, 0.03


def _tilt_field(m: np.ndarray) -> np.ndarray:
    """候选修复原型：侧倾校正基线场（t 网格 + 地面解释率 + 余量门槛）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    h, w = band.shape
    cols = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:w]
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = (np.arange(w, dtype=np.float32)[None, :] - TILT_PIVOT_X)
    sub = (slice(None, None, 3), slice(None, None, 4))
    profs, scores = {}, {}
    for t in TILT_GRID:
        if t == 0.0:
            prof = np.quantile(band[:, cols], dg.Q_GROUND, axis=1)
        else:
            mmat = np.float32([[1, 0, 0], [t, 1, -TILT_PIVOT_X * t]])
            sheared = cv2.warpAffine(band, mmat, (w, h), flags=cv2.INTER_LINEAR)
            prof = np.quantile(sheared[:, cols], dg.Q_GROUND, axis=1)
        v = np.clip(ys + xs * t, 0, h - 1).astype(np.int32)
        g = prof[v]
        rel = (band[sub] - g[sub]) / np.maximum(g[sub], 1e-6)
        profs[t] = prof
        scores[t] = float(((rel > GROUND_RESID_LO) & (rel < GROUND_RESID_HI)).mean())
    best_t = 0.0
    for t, s in scores.items():
        if t != 0.0 and s > scores[0.0] + TILT_MARGIN and s > scores[best_t]:
            best_t = t
    v = np.clip(ys + xs * best_t, 0, h - 1).astype(np.int32)
    return profs[best_t][v]


def _row_q20_field(m: np.ndarray) -> np.ndarray:
    """现产码行为：逐行 q20（基线 A/B 的对照侧）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    cols = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:band.shape[1]]
    prof = np.quantile(band[:, cols], dg.Q_GROUND, axis=1)
    return np.repeat(prof[:, None], band.shape[1], axis=1)


def _pct(xs, q):
    if not xs:
        return float("nan")
    return float(np.percentile(np.abs(xs), q))


def _block_rate(m, field, gate):
    """带内假块率：rel>门 的像素占比（侧倾破产的直接症状量）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    g = field(m)
    r = (band - g) / np.maximum(g, 1e-6)
    return float((r > gate).mean())


def main() -> None:
    labels = list(csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")))
    frames = []
    for r in labels:
        key = pcq.frame_key(Path(r["path"]))
        p = NPY_DIR / f"{key}__d336q4f16.npy"
        if p.exists():
            frames.append((r, np.load(p).astype(np.float32)))
    print(f"金标缓存 {len(frames)} 帧（gate={dg.GATE}）")
    for tag, field in (("B_rowq20_产码", _row_q20_field), ("A_tilt_原型", _tilt_field)):
        rates = [_block_rate(m, field, dg.GATE) for _, m in frames]
        hot = sum(v > 0.5 for v in rates)
        print(f"[{tag}] 假块率 p50 {np.percentile(rates, 50):.0%} "
              f"p90 {np.percentile(rates, 90):.0%}  >50% 热帧 {hot}")


if __name__ == "__main__":
    main()
