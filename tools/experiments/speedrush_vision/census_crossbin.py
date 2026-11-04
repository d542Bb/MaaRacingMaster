# -*- coding: utf-8 -*-
"""第九轮语料普查：跨箱稳定性/平台度取证（2026-10-03 挂账转正）。

第八轮实锤（commit ef3fbc9）：平面残差缓坡类假缘与真 kerb 单帧剖面同形
（000340 右 6.2m 真 kerb 阶差仅 0.08），尾部持续门杀不掉，单帧形状判据到头。
挂账判据方向：**真路物横向位置跨 Z 箱恒定；残差缓坡穿越位随 Z 漂**
（000906 旧锁"真值"即残差缓坡：穿越位 6.0→7.3→8.9m 随 Z 漂）。

本探针重放全部证据包（depth_debug_* 的 evid npz）+ 金标帧（npy_moge npz），
逐帧逐侧逐箱导出：
- 产线口径的检出穿越（直接调 _scan_side / reading_from_points，与在线逐位同源）；
- 完整横向剖面（格心、格中位高、阈值）——判据设计与肉眼复核的原材料。

不做任何判据裁定，只出数据；分析看 print 摘要 + JSON。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/census_crossbin.py
输出 → .workbuddy-ai/speedrush_census/census.json + 摘要 stdout。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DIAG_Y1, Y0, reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

FULL_W, FULL_H = 1280, 720
TRACES = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "data" / "speedrush"
OUT = Path(".workbuddy-ai") / "speedrush_census"


def load_evid_frame(stem: Path):
    """证据包一帧 → (全幅点图, fx, fy, ego, obj)，口径同 replay_debug_evid。"""
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    ego = obj = None
    ego_p = Path(str(stem) + "_ego.npy")
    if ego_p.exists():
        ego = np.unpackbits(np.load(ego_p))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        mask_p = Path(str(stem) + "_mask.npy")
        if mask_p.exists():
            merged = np.unpackbits(np.load(mask_p))[: FULL_W * FULL_H] \
                .reshape(FULL_H, FULL_W).astype(bool)
            obj = merged & ~ego
    return pts, fx, fy, ego, obj


def bin_profile(xb: np.ndarray, hb: np.ndarray, side: int, zc: float,
                extent: float) -> dict | None:
    """单箱单侧完整横向剖面（_scan_side 的建剖面段原样口径，不裁剪不入门）。"""
    sel = (xb * side > 0.2) & (np.abs(xb) <= dg.X_MAX_M) \
        & np.isfinite(xb) & np.isfinite(hb)
    if sel.sum() < dg.MIN_SIDE_PTS:
        return None
    xs, hs = xb[sel], hb[sel]
    px: list[float] = []
    ph: list[float] = []
    x0 = -dg.X_MAX_M + dg.XBIN_DX / 2
    while x0 < dg.X_MAX_M:
        if x0 * side > 0 and abs(x0) <= extent * zc:
            m = (xs >= x0 - dg.XBIN_DX / 2) & (xs < x0 + dg.XBIN_DX / 2)
            if m.sum() >= dg.MIN_BIN_PTS:
                px.append(float(x0))
                ph.append(float(np.median(hs[m])))
        x0 += dg.XBIN_DX
    if len(px) < 3:
        return None
    arr = np.array([px, ph])
    srt = np.argsort(arr[0] * side)         # 路心 → 外，与 _scan_side 同序
    arr = arr[:, srt]
    thr = dg.EDGE_HT + dg.EDGE_HT_SLOPE * max(0.0, zc - 3.0)
    return {"x": arr[0].round(3).tolist(), "h": arr[1].round(4).tolist(),
            "thr": round(thr, 4), "n": int(sel.sum())}


def census_frame(tag: str, pts: np.ndarray, fx: float, fy: float,
                 ego, obj, cal) -> dict:
    """一帧 → 产线读数 + 逐箱逐侧剖面（判据设计的原材料）。"""
    rd = reading_from_points(pts, fx, cal, ego_mask=ego, object_mask=obj, fy=fy)
    dig = dg._dig_band(ego, pts.shape[1])
    if obj is not None:
        dig = dig | dg._dig_band(obj, pts.shape[1])
    X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
    coef = dg._fit_road_plane(X, Y, Z, dig, fy=fx if fy is None else fy)
    rec: dict = {"tag": tag,
                 "L": rd.left_edge_lane, "R": rd.right_edge_lane,
                 "rejects": list(rd.rejects),
                 "edges": [], "bins": []}
    if coef is None:
        rec["rejects"] = rec["rejects"] or ["平面拟合失败"]
        return rec
    a, b, c = float(coef[0]), float(coef[1]), float(coef[2])
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    sky = np.isfinite(hgt) & (hgt > dg.SKY_HGT)
    extent = (pts.shape[1] / 2.0) / fx
    for z0, z1 in dg.ZBIN:
        m = ((Z >= z0) & (Z < z1) & np.isfinite(X) & np.isfinite(hgt)
             & (~dig) & (~sky))
        if m.sum() < dg.MIN_BOX_PTS:
            continue
        zc = (z0 + z1) / 2
        for side in (1, -1):
            prof = bin_profile(X[m], hgt[m], side, zc, extent)
            ex = dg._scan_side(X[m], hgt[m], side, zc, extent)
            hit = {"side": side, "zc": zc,
                   "x": round(float(ex), 3) if np.isfinite(ex) else None}
            rec["edges"].append(hit)
            if prof is not None:
                rec["bins"].append({**hit, "prof": prof})
    return rec


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cal = load_calib()
    records: list[dict] = []

    for d in sorted((TRACES / "control_traces").glob("depth_debug_*")):
        stems = sorted(p.name[: -len("_evid.npz")]
                       for p in d.glob("*_evid.npz"))
        print(f"[pack] {d.name}: {len(stems)} 帧", flush=True)
        for s in stems:
            pts, fx, fy, ego, obj = load_evid_frame(d / s)
            records.append(census_frame(f"{d.name}/{s}", pts, fx, fy,
                                        ego, obj, cal))

    gold_dir = TRACES / "depth_review" / "npy_moge"
    ego = dg.DepthRoadObserver._load_ego_mask()
    for p in sorted(gold_dir.glob("*.npz")):
        z = np.load(p)
        pts = z["pts"].astype(np.float32)
        records.append(census_frame(f"gold/{p.stem}", pts, float(z["fx"]),
                                    float(z["fy"]), ego, None, cal))

    out = OUT / "census.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    # 摘要：每帧每侧的跨箱序列 + 漂移量
    print(f"\n{'tag':<34} {'L车道':>7} {'R车道':>7}  跨箱检出 (zc→x_m)")
    for r in records:
        cells = []
        for side, lab in ((-1, "L"), (1, "R")):
            xs = [(e["zc"], e["x"]) for e in r["edges"]
                  if e["side"] == side and e["x"] is not None]
            if xs:
                drift = max(x for _, x in xs) - min(x for _, x in xs)
                seq = " ".join(f"{zc:.0f}→{x:+.2f}" for zc, x in xs)
                cells.append(f"{lab}[漂{drift:.2f}] {seq}")
            else:
                cells.append(f"{lab}[无]")
        lane = (f"{r['L'] if r['L'] is not None else '--':>7} "
                f"{r['R'] if r['R'] is not None else '--':>7}")
        print(f"{r['tag']:<34} {lane}  {' | '.join(cells)}")
    print(f"\n明细 → {out.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
