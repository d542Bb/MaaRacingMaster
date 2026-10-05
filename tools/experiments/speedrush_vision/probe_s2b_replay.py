# -*- coding: utf-8 -*-
"""S2-B 接线验收回放：黑视金标 10 帧 × 闸开采样器（离线，产线同口径）。

对每帧：evid 离线复算栅格（与 reading 共享平面，同产线 observe_debug）→
以自车系（黑视时 road_offset=None，轨迹语义=相对自车）对 D_GRID 全终点
逐个过栅格裁决（近场带 z 3~9m、包络 ±(车半宽+margin)）→ 报告：
- 每帧候选存活数 / 全灭（ABORT）；
- 「金标安全终点」(|d1| < min|金标缘| − 内收包络) 是否存活——护栏层裁决
  的验收面：往里收过的安全区候选不该被 blocked 误杀。

用法：
  .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_s2b_replay.py
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.latsample import (
    GRID_CAR_HALF_LANE_M, GRID_VETO_MARGIN_LANE, D_GRID, LatTrajectorySampler,
    _effective_blocked as _effective_blocked_lf, quintic)  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
import gold_blackout_score as gbs  # noqa: E402
import select_blackout_frames as sbf  # noqa: E402

HALF_LANE = GRID_CAR_HALF_LANE_M / load_calib().lane_w_m \
    + GRID_VETO_MARGIN_LANE       # 内收包络（道）≈0.77


def main() -> None:
    cal = load_calib()
    lw = cal.lane_w_m
    # 裁决复用产线实现（_grid_penalty），回放与生产同一口径——不平行复刻
    s = LatTrajectorySampler(v_lat_max=3.0, v_ego_row=500.0, horizon_s=2.5,
                             gap_lane=0.3)
    labels = list(csv.DictReader((sbf.OUT / "gold_labels.csv")
                                 .open(encoding="utf-8")))
    seen: set[Path] = set()
    n_abort = n_frames = 0
    safe_alive = safe_total = 0
    for r in labels:
        p = Path(r["path"])
        if p in seen:
            continue
        seen.add(p)
        stem = p.with_name(p.name[:-len("_frame.jpg")])
        pts, fx, fy, ego, obj = sbf.assemble(stem)
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                    object_mask=obj, fy=fy)
        grid = dg.drivable_grid_from_points(pts, fx, fy, ego=ego, obj=obj,
                                            coef=rd.coef)
        if grid.coef is None:
            print(f"{p.name}: 栅格弃权（平面拟合失败），跳过")
            continue
        blocked_ix = np.nonzero((grid.state[:24] == dg.GRID_BLOCKED)
                                .any(axis=0))[0]
        span = gbs.gold_lanes_at(r, "l", grid.coef, fx, fy, lw), \
            gbs.gold_lanes_at(r, "r", grid.coef, fx, fy, lw)
        gold = [np.median([v for v in s.values() if v is not None])
                if s and any(v is not None for v in s.values()) else None
                for s in span]
        alive, dead, safe_ok, safe_n = [], [], 0, 0
        blk = _effective_blocked_lf(grid)
        for d1 in D_GRID:
            c = quintic(0.0, 0.0, 0.0, d1, 1.2)
            hit, _unk = s._grid_penalty(c, 1.2, d1, blk, grid, 0.0, lw)
            (dead if hit else alive).append(d1)
            if gold[0] is not None and gold[1] is not None:
                edge = min(abs(gold[0]), abs(gold[1]))
                if abs(d1) < edge - HALF_LANE:
                    safe_n += 1
                    safe_ok += 0 if hit else 1
        n_frames += 1
        n_abort += 0 if alive else 1
        safe_total += safe_n
        safe_alive += safe_ok
        print(f"{p.name} 金标缘[{gold[0]}, {gold[1]}] 存活终点={alive} "
              f"灭={dead}" + (" ←全灭(ABORT)" if not alive else ""))
    print(f"\n== 回放（{n_frames} 帧）：ABORT 率 {n_abort}/{n_frames}；"
          f"金标安全终点存活 {safe_alive}/{safe_total} ==")
    if safe_total:
        print("安全终点被误杀 = 护栏层裁决不成立的信号；ABORT 帧看 blocked "
              "是否真贴身（黑视宽路双缘出视野属正常全灭）")


if __name__ == "__main__":
    main()
