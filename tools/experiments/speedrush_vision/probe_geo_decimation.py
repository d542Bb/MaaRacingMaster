# -*- coding: utf-8 -*-
"""几何块抽取（GEO_DS）的时延与读数一致性验收面。

两条臂都走**产线同一条路径** `DepthRoadObserver.observe_debug`（真推理 + 真
后处理），只切换 `depth_geo.GEO_DS`：1=旧全幅，2=2×2 抽取。指标与阈值是改代码
之前预登记的（见 DEPTH_CHAIN_LATENCY_20261017.md §3 第 6 项）：

- sides 同判率 ≥95%
- 左右缘车道差 P50 ≤0.05 lane
- 栅格近场裁决带（z3~9m、|x|≤5m）同判率 ≥98%、blocked→drivable 漏判 ≤1%

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_geo_decimation.py [局名] [帧数]``
只读 debug 产物。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import maaracing_master.plugins.speedrush.depth_geo as dg    # noqa: E402
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DBG = Path.home() / "AppData" / "Roaming" / "MaaRacingMaster" / "debug" / "speedrush"
GAME = sys.argv[1] if len(sys.argv) > 1 else "20261017_115032_p1"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 40

xs_c = -dg.GRID_X_MAX + (np.arange(dg.GRID_NX) + 0.5) * dg.GRID_CELL
zs_c = dg.GRID_Z_LO + (np.arange(dg.GRID_NZ) + 0.5) * dg.GRID_CELL
BAND = np.outer((zs_c >= 3.0) & (zs_c <= 9.0), np.abs(xs_c) <= 5.0)


def _safe(d: str) -> Path:
    if ".." in Path(d).parts:
        raise ValueError(f"路径分量含 ..：{d}")
    p = (DBG / d).resolve()
    if DBG.resolve() not in p.parents:
        raise ValueError(f"路径越界：{d}")
    return p


def _med(xs):
    xs = sorted(xs)
    return float("nan") if not xs else xs[min(len(xs) - 1, int(0.5 * (len(xs) - 1)))]


def main() -> None:
    cal = load_calib()
    d = _safe(GAME) / "depth_debug"
    frames = sorted(d.glob("*_frame.jpg"))[:LIMIT]
    if not frames:
        print(f"无帧：{d}")
        return
    obs = dg.DepthRoadObserver(dg.load_session(DEPTH_MODEL_FILE), cal)
    print(f"局={GAME} 帧={len(frames)} weights={DEPTH_MODEL_FILE.name}")

    stats = {"1": [], "2": []}
    sides_eq, lane_diff, band_eq, leak = [], [], [], []
    for i, fp in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
        out = {}
        for ds in (1, 2):
            dg.GEO_DS = ds
            t = time.perf_counter()
            reading, evid = obs.observe_debug(rgb, detections=None, fid=i,
                                              frame_ts_ns=None)
            stats[str(ds)].append((time.perf_counter() - t) * 1000.0)
            out[ds] = (reading, evid.get("grid") if evid else None)
        a, b = out[1], out[2]
        if a[0] is not None and b[0] is not None:
            sides_eq.append(a[0].sides == b[0].sides)
            for s in ("left_edge_lane", "right_edge_lane"):
                x, y = getattr(a[0], s), getattr(b[0], s)
                if x is not None and y is not None:
                    lane_diff.append(abs(x - y))
        if a[1] is not None and b[1] is not None:
            sa, sb = a[1].state, b[1].state
            m = BAND & (sa >= 0) & (sb >= 0)
            band_eq.append(float((sa[m] == sb[m]).mean()) if m.any() else 1.0)
            blk = BAND & (sa == dg.GRID_BLOCKED)
            if blk.any():
                leak.append(float((sb[blk] == dg.GRID_DRIVABLE).mean()))
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(frames)}")

    print("\n=== 时延（observe_debug 端到端，含推理） ===")
    for k, label in (("1", "全幅 GEO_DS=1"), ("2", "抽取 GEO_DS=2")):
        v = stats[k]
        print(f"  {label}: P50={_med(v):.1f}ms min={min(v):.1f} max={max(v):.1f}")
    print("\n=== 读数一致性 ===")
    print(f"  sides 同判 {sum(sides_eq)}/{len(sides_eq)}"
          f"（{100 * sum(sides_eq) / max(len(sides_eq), 1):.1f}%）")
    print(f"  左右缘车道差 P50={_med(lane_diff):.3f} lane P90="
          f"{sorted(lane_diff)[min(len(lane_diff) - 1, int(0.9 * len(lane_diff)))] if lane_diff else float('nan'):.3f}"
          f"（n={len(lane_diff)}）")
    print(f"  栅格近场裁决带同判率 P50={_med(band_eq):.3f}（n={len(band_eq)}）")
    print(f"  blocked→drivable 漏判率 P50={_med(leak):.3f}（n={len(leak)}）")


if __name__ == "__main__":
    main()
