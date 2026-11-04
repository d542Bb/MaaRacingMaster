# -*- coding: utf-8 -*-
"""晕影模板扣除探针：2×2 矩阵「晕影治理」第三候选（直接对症）。

原理：晕影钉在自车列位置、跨帧统计稳定（halo_strip 三帧跟随为证）⇒ 可用
跨帧中位模板估计其形状并从视差中扣除。**split-half 防自证**：偶数序帧估
模板、评奇数序帧，反之亦然。

预注册口径：
- 模板 T(y,x) = 半帧集 r 图的中位（r=(M−g)/g，g=产码 _ground_q20；ego_mask
  列不参与统计）；仅晕影区生效：x∈[EGO_COLS[0]−300, EGO_COLS[1]+300]、
  检测带内；T 负值截零（晕影只抬升）；25×9 盒滤波平滑。
- 修正：m' = m − T·g（仅晕影区检测带内），门维持 0.08，整条生产读数链不动。
- 判据：R 覆盖/坏率改善、L 不劣化；渲染修正前后"高度图"目检红晕是否消失。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_halo_template.py
"""
from __future__ import annotations

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
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

from probe_mech_score import gold_lane  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
ZONE = (dg.EGO_COLS[0] - 300, dg.EGO_COLS[1] + 300)   # 晕影区列范围
ROWS = (dg.Y0, dg.DIAG_Y1)


def height_img(r_band: np.ndarray, m: np.ndarray) -> np.ndarray:
    """r → 高度图（±10% 量程，蓝=远白=路面红=近；带外黑；OpenCV5 无
    COLORMAP_COOLWARM，手绘三通道分段插值）。"""
    g = dg._ground_q20(m)[:, None]
    h = np.full(r_band.shape, np.nan, np.float32)
    ok = np.isfinite(r_band) & (g > 0)
    h[ok] = r_band[ok] / (1.0 + r_band[ok])
    v = np.clip((h + 0.10) / 0.20, 0, 1)
    xs = np.array([0.0, 0.5, 1.0])
    b = np.interp(v, xs, [255, 255, 0]).astype(np.uint8)
    gch = np.interp(v, xs, [0, 255, 0]).astype(np.uint8)
    rch = np.interp(v, xs, [0, 255, 255]).astype(np.uint8)
    img = np.stack([b, gch, rch], axis=-1)
    img[~ok] = 0
    return img
    return img


def main() -> None:
    labels = [r for r in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))]
    frames = []
    for r in labels:
        cache = pd.OUT / "npy" / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy"
        if cache.exists():
            frames.append((r, np.load(cache).astype(np.float32)))
    n = len(frames)
    print(f"考卷 {n} 帧；晕影区列 {ZONE}、带行 {ROWS}；split-half 交叉估模板")

    gs = np.stack([dg._ground_q20(m) for _, m in frames])          # (n, 375)
    rs = np.full((n, ROWS[1] - ROWS[0], 1280), np.nan, np.float32)
    for i, (_, m) in enumerate(frames):
        rs[i] = (m[ROWS[0]:ROWS[1]] - gs[i][:, None]) / np.maximum(gs[i][:, None], 1e-6)
    if EGO is not None:
        rs[:, EGO[ROWS[0]:ROWS[1]]] = np.nan        # 自车挖除区不进模板统计

    halves = (np.arange(n) % 2 == 0, np.arange(n) % 2 == 1)
    templates = []
    for eval_mask in halves:
        tpl_mask = ~eval_mask
        T = np.nanmedian(rs[tpl_mask], axis=0)                     # (375, 1280)
        T = np.where(np.isfinite(T), np.maximum(T, 0.0), 0.0).astype(np.float32)
        T = cv2.blur(T, (25, 9))
        templates.append(T)
    # 模板幅度（半集 A 的模板，晕影核心区=挖除带外 40~160px）
    for half_name, eval_mask, tpl in (("A", halves[0], templates[1]),
                                      ("B", halves[1], templates[0])):
        core = tpl[:, dg.EGO_COLS[0]-160:dg.EGO_COLS[0]] 
        core2 = tpl[:, dg.EGO_COLS[1]:dg.EGO_COLS[1]+160]
        print(f"模板{half_name}（由{'奇' if half_name=='A' else '偶'}数序帧估计）："
              f"挖除带左邻中位={np.median(core):.3f} 右邻={np.median(core2):.3f}")

    def pct(xs, q):
        xs = sorted(xs)
        return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]

    res = {"base": {"L": [], "R": []}, "tmpl": {"L": [], "R": []}}
    cov = {"base": {"L": 0, "R": 0}, "tmpl": {"L": 0, "R": 0}}
    bad = {"base": {"L": 0, "R": 0}, "tmpl": {"L": 0, "R": 0}}
    denom = {"L": 0, "R": 0}
    rendered = 0
    for i, (r, m) in enumerate(frames):
        half = 0 if halves[0][i] else 1
        T = templates[1 - half]                                    # 对方半集估的模板
        g = gs[i]
        m2 = m.copy()
        m2[ROWS[0]:ROWS[1], ZONE[0]:ZONE[1]] = (
            m[ROWS[0]:ROWS[1], ZONE[0]:ZONE[1]]
            - T[:, ZONE[0]:ZONE[1]] * g[:, None])
        rd0 = dg.reading_from_map(m, CAL, EGO, gate=dg.GATE)
        rd1 = dg.reading_from_map(m2, CAL, EGO, gate=dg.GATE)
        for side, lane0, lane1 in (("L", rd0.left_edge_lane, rd1.left_edge_lane),
                                   ("R", rd0.right_edge_lane, rd1.right_edge_lane)):
            glane, _ = gold_lane(r, side.lower())
            if glane is None:
                continue
            denom[side] += 1
            for tag, lane in (("base", lane0), ("tmpl", lane1)):
                if lane is None or lane != lane:
                    continue
                cov[tag][side] += 1
                d = float(lane - glane)
                res[tag][side].append(d)
                bad[tag][side] += abs(d) > 0.5
        if rendered < 2 and pcq.frame_key(Path(r["path"])).endswith(("000906", "001220")):
            r_before = rs[i]
            r_after = (m2[ROWS[0]:ROWS[1]] - g[:, None]) / np.maximum(g[:, None], 1e-6)
            pair = np.hstack([height_img(r_before, m), height_img(r_after, m2)])
            if rendered == 0:
                gallery = pair
            else:
                gallery = np.vstack([gallery, pair])
            rendered += 1
    print(f"\n{'变体':<6}{'L覆盖':>8}{'R覆盖':>8}"
          f"{'Ldev p50/p90':>14}{'L坏':>6}{'Rdev p50/p90':>14}{'R坏':>6}")
    for tag in ("base", "tmpl"):
        ld, rd_ = res[tag]["L"], res[tag]["R"]
        print(f"{tag:<7}{cov[tag]['L']:>4}/{denom['L']:<3}{cov[tag]['R']:>4}/{denom['R']:<3}"
              f"{pct(ld,.5):>+9.2f}/{pct([abs(x) for x in ld],.9):<5.2f}"
              f"{bad[tag]['L']/max(len(ld),1):>5.0%}"
              f"{pct(rd_,.5):>+9.2f}/{pct([abs(x) for x in rd_],.9):<5.2f}"
              f"{bad[tag]['R']/max(len(rd_),1):>5.0%}")
    out = pd.OUT / "halo_template_cmp.jpg"
    cv2.imwrite(str(out), gallery, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"高度图对比（每对=修正前|修正后）：{out}")


if __name__ == "__main__":
    main()
