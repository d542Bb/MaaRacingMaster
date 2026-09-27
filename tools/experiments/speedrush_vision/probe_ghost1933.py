# -*- coding: utf-8 -*-
"""1933 两局假宽对行级取证（用户判：路宽是常数 → 假宽对=缘挂错结构）。

对两局 depth_debug 包（带新 _ego.npy）跑生产同口径读法（含动态自车吸收），
抽双侧对宽 ≥2.2 道（消费守卫拒收域）的帧，看行级形态：
- 假宽是「所有行都宽」（近带真缘整段缺席 → 只剩远行幽灵，生产端无材料可救）
  还是「窄行存在但分位没选中」（聚合判据问题）；
- 近行为什么缺席：出画（u_l≤2/u_r≥1277）还是遮挡/行不足。

用法（仓库根 .venv）：
  python tools/experiments/speedrush_vision/probe_ghost1933.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DIAG_Y1, Y0, DepthRoadObserver, _cloud_band, _dig_band, _grow_ego_above,
    _plane_fit_band, _row_scan, reading_from_map)
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

APP = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
SESSIONS = ("depth_debug_20260927_193302", "depth_debug_20260927_193346")


def row_detail(src: Path, stem: str, cal) -> list[dict]:
    band = np.load(src / f"{stem}_band.npy").astype(np.float32)
    m = np.full((720, 1280), np.inf, np.float32)
    m[Y0:DIAG_Y1] = band
    merged = np.unpackbits(np.load(src / f"{stem}_mask.npy"))\
        .astype(bool).reshape(720, 1280)
    ego_p = src / f"{stem}_ego.npy"
    ego = np.unpackbits(np.load(ego_p)).astype(bool).reshape(720, 1280) \
        if ego_p.exists() else DepthRoadObserver._load_ego_mask()
    X, Y, Z = _cloud_band(m, 24.06)
    dig = _dig_band(merged, 1280)
    coef, ground, above = _plane_fit_band(X, Y, Z, dig)
    if coef is not None:
        grown = _grow_ego_above(above, _dig_band(ego, 1280), Z)
        if grown is not None:
            dig = dig | grown
            coef, ground, above = _plane_fit_band(X, Y, Z, dig)
    if coef is None:
        return []
    rows = []
    for rr in _row_scan(X, Z, ground, above, dig, _dig_band(merged, 1280), coef):
        rows.append({
            "v": rr.v, "xl": rr.xl, "xr": rr.xr,
            "lane_l": x_lane_of(int(round(rr.u_l)), rr.v, cal),
            "lane_r": x_lane_of(int(round(rr.u_r)), rr.v, cal),
            "occ_l": rr.occ_l, "occ_r": rr.occ_r, "zmed": rr.zmed})
    return rows


def main() -> None:
    cal = load_calib()
    summary = {"all_wide": 0, "has_narrow": 0, "frames": 0}
    narrow_hist, wide_frames = [], []
    for name in SESSIONS:
        src = APP / "control_traces" / name
        for band_p in sorted(src.glob("d*_band.npy")):
            stem = band_p.stem[: -len("_band")]
            rows = row_detail(src, stem, cal)
            if len(rows) < 3:
                continue
            widths = [(d["lane_r"] - d["lane_l"]) / 2.0 for d in rows
                      if not (d["occ_l"] or d["occ_r"])]
            if not widths:
                continue
            summary["frames"] += 1
            wmin = min(widths)
            narrow_hist.append(wmin)
            if wmin >= 1.1:               # 半宽≥1.1 道=整帧无一行见真宽
                summary["all_wide"] += 1
                wide_frames.append((name, stem, len(rows), wmin,
                                    round(float(np.median([d["zmed"] for d in rows])), 1)))
            else:
                summary["has_narrow"] += 1
    n = summary["frames"]
    print(f"双侧有行帧 {n}：整帧无窄行（近带真缘缺席）{summary['all_wide']}，"
          f"存在窄行 {summary['has_narrow']}")
    q = lambda p: float(np.percentile(narrow_hist, p))
    print(f"帧最小行半宽 p10/50/90 = {q(10):.2f}/{q(50):.2f}/{q(90):.2f} 道")
    print("整帧宽帧样本（局,帧,行数,最小半宽,行Z中位）:")
    for e in wide_frames[:12]:
        print("  ", e)
    # 近带行去向细分：宽帧里 zmed<6.5 的行是被出画/遮挡杀掉，还是地面本身就少
    print("\n宽帧近带行细分（zmed<6.5 的保留行占比 / 各帧近带行均宽）:")
    for name, stem, nrow, wmin, zmed in wide_frames[:8]:
        src = APP / "control_traces" / name
        rows = row_detail(src, stem, cal)
        near = [d for d in rows if d["zmed"] < 6.5]
        far = [d for d in rows if d["zmed"] >= 6.5]
        wn = [(d["lane_r"] - d["lane_l"]) / 2.0 for d in near
              if not (d["occ_l"] or d["occ_r"])]
        wf = [(d["lane_r"] - d["lane_l"]) / 2.0 for d in far
              if not (d["occ_l"] or d["occ_r"])]
        print(f"  {stem}: 近带行 {len(near)}（均宽 "
              f"{np.mean(wn):.2f} 道） / 远带行 {len(far)}（均宽 "
              f"{np.mean(wf):.2f} 道）")


if __name__ == "__main__":
    main()
