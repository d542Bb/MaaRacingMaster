# -*- coding: utf-8 -*-
"""油漆线探针（C 类实验，2026-10-02）：HSV 黄实线 → 3D 投影边界读数，
与深度几何读数在真机调试帧上对质。回答三件事：
① 逐帧逐侧油漆线检出率；② 与深度缘同场时的吻合度（米/车道）；
③ 深度侧弃权时的补位率（含护栏点空洞段——最近一致边界的取证帧即此形态）。

口径与产线对齐：band=Y0..DIAG_Y1、Z 箱=depth_geo.ZBIN、横格 0.25m、
「最近一致」邻箱确认窗=EDGE_MATCH_M；平面拟合直接调 depth_geo（同帧 fy）。
油漆像元的 Z/X 取自点云（深度活着是本探针前提——投影依赖深度，非冗余通道）。
掩码内像元取「该箱该侧最靠路心的 0.25m 格簇的中位 X」= 油漆线中心：
外侧肩内杂物不污染（在更外侧），中线白虚线天然排除（只收黄域）。

内参口径：evid 存原生归一化焦距，全幅 fx=×1280、fy=×720（u∈[0,1] 约定，
moge_post.focal_to_normalized_intrinsics；勿再除原生宽高）。

用法（仓库根 .venv）：
    python tools/experiments/speedrush_vision/probe_paint_line.py \
        --dir "C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/control_traces/depth_debug_20261002_162102"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

H_LO, H_HI = 18, 30       # 黄域（OpenCV H）；d00003 全帧取证后收紧域
S_MIN, V_MIN = 100, 100
EXCL_DILATE = 3           # 掩码描边挖除：ego/YOLO 轮廓线膨胀半径
MIN_BIN_PX = 25           # 单箱单侧油漆像元下限（薄远段宁缺毋假）
X_MIN_M = 1.0             # 路心 1m 内不收（白虚线域+中线杂波）


def unpack(p: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    n = int(np.prod(shape))
    return np.unpackbits(p)[:n].reshape(shape).astype(bool)


def load(stem: Path):
    d = np.load(str(stem) + "_evid.npz")
    pts_n = d["pts"].astype(np.float32)
    oh, ow = pts_n.shape[:2]
    valid = unpack(d["valid"], pts_n.shape[:2])
    pts = np.stack([cv2.resize(pts_n[..., k], (1280, 720)) for k in range(3)], -1)
    valid = cv2.resize(valid.astype(np.float32), (1280, 720),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid] = np.nan
    fx = float(d["fx"]) * 1280.0     # u∈[0,1] 焦距 × 全幅宽（勿除原生宽高）
    fy = float(d["fy"]) * 720.0
    ego = unpack(np.load(str(stem) + "_ego.npy"), (720, 1280))
    objm = unpack(np.load(str(stem) + "_mask.npy"), (720, 1280))
    bgr = cv2.imread(str(stem) + ".jpg")
    rgb = cv2.cvtColor(bgr[:720], cv2.COLOR_BGR2RGB)
    return pts, fx, fy, ego, objm & ~ego, rgb


def paint_mask(rgb: np.ndarray, excl: np.ndarray) -> np.ndarray:
    band = rgb[dg.Y0:dg.DIAG_Y1]
    hsv = cv2.cvtColor(band, cv2.COLOR_RGB2HSV)
    m = ((hsv[..., 0] >= H_LO) & (hsv[..., 0] <= H_HI)
         & (hsv[..., 1] >= S_MIN) & (hsv[..., 2] >= V_MIN))
    bad = cv2.dilate(excl[dg.Y0:dg.DIAG_Y1].astype(np.uint8),
                     np.ones((EXCL_DILATE * 2 + 1,) * 2, np.uint8)) > 0
    return m & ~bad


def paint_edges(pts: np.ndarray, pm: np.ndarray) -> dict[int, list[tuple[float, float]]]:
    """掩码像元 → 各箱各侧「最靠路心格簇」的中位 X（米）。
    返回 {side: [(zc, x_m), ...]}。"""
    rows = np.nonzero(pm.any(axis=1))[0]
    out: dict[int, list[tuple[float, float]]] = {1: [], -1: []}
    if len(rows) == 0:
        return out
    sub = slice(rows[0], rows[-1] + 1)
    Z = pts[dg.Y0:dg.DIAG_Y1, :, 2][sub][pm[sub]]
    X = pts[dg.Y0:dg.DIAG_Y1, :, 0][sub][pm[sub]]
    ok = np.isfinite(Z) & np.isfinite(X)
    Z, X = Z[ok], X[ok]
    for z0, z1 in dg.ZBIN:
        mb = (Z >= z0) & (Z < z1)
        if mb.sum() < MIN_BIN_PX:
            continue
        zc = (z0 + z1) / 2
        for side in (1, -1):
            xs = X[mb] * side
            xs = xs[xs > X_MIN_M]
            if len(xs) < MIN_BIN_PX:
                continue
            srt = np.sort(xs)
            # 最靠路心的连续簇：从路心侧起收格，遇 >0.6m 空档即断（线宽实测
            # ≤0.4m；空档外侧=肩内杂物/飞币，归外面那簇不归线）
            clusters: list[list[float]] = [[srt[0]]]
            for v in srt[1:]:
                if v - clusters[-1][-1] > 0.6:
                    clusters.append([])
                clusters[-1].append(v)
            cl = clusters[0]
            if len(cl) < MIN_BIN_PX:
                continue
            out[side].append((zc, float(side * np.median(cl))))
    return out


def nearest_consistent(samples: list[tuple[float, float]]) -> tuple[float | None, int]:
    """最近一致边界（与产线 _side 同判据）：被次近箱确认的最近读数。"""
    smp = sorted(samples)
    for i in range(len(smp) - 1):
        if abs(smp[i][1] - smp[i + 1][1]) <= dg.EDGE_MATCH_M:
            return smp[i][1], len(smp)
    if smp:
        return float(np.median([x for _, x in smp])), len(smp)
    return None, 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    root = Path(args.dir)
    cal = load_calib()
    stems = sorted(root.glob("d*_evid.npz"))

    agree_m: list[float] = []
    fill_hits = fill_tries = 0
    print(f"{'帧':>7} {'深度L/R(道)':>16} {'油漆L/R(道)':>16} "
          f"{'ΔL(m)':>7} {'ΔR(m)':>7}  箱数L/R")
    for st in stems:
        stem = st.with_name(st.name[: -len("_evid.npz")])
        pts, fx, fy, ego, obj, rgb = load(stem)
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                    object_mask=obj, fy=fy)
        pe = paint_edges(pts, paint_mask(rgb, ego | obj))
        cells = []
        for side, dl in ((-1, rd.left_edge_lane), (1, rd.right_edge_lane)):
            px, nb = nearest_consistent(pe[side])
            plc = None if px is None else px / cal.lane_w_m
            d = None
            if plc is not None and dl is not None:
                d = (px - dl * cal.lane_w_m)
                agree_m.append(abs(d))
            elif plc is not None and dl is None:
                fill_tries += 1
                if nb >= 2:
                    fill_hits += 1
            cells.append((plc, nb, d))
        (plc_l, nb_l, d_l), (plc_r, nb_r, d_r) = cells
        fmt = lambda v: "-" if v is None else f"{v:+.2f}"
        print(f"{stem.name:>7} {fmt(rd.left_edge_lane):>7}/{fmt(rd.right_edge_lane):<8}"
              f" {fmt(plc_l):>7}/{fmt(plc_r):<8} {fmt(d_l):>7} {fmt(d_r):>7}"
              f"  {nb_l}/{nb_r}")

    if agree_m:
        a = np.array(agree_m)
        print(f"\n同场吻合（|Δ| 米）：n={len(a)}  中位={np.median(a):.2f}  "
              f"p90={np.percentile(a, 90):.2f}  ≤0.5m 占比={float((a <= 0.5).mean()):.0%}"
              f"  ≤1.0m 占比={float((a <= 1.0).mean()):.0%}")
    print(f"深度弃权侧油漆补位（≥2 箱）：{fill_hits}/{fill_tries}")


if __name__ == "__main__":
    main()
