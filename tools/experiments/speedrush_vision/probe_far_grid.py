# -*- coding: utf-8 -*-
"""看得更远：网格纵窗 3-16m（产线）vs 扩到 3-30m（离线扩窗）BEV 对比 + 耗时。

问（维护者"程序看的不够全局"）：前方路收窄/弯道的全局趋势在 16m 截断线外，
点云数据实际延伸到 30m 开外（Z 分布 16-20m 占 6.9%、20-24m 5.0%、24-28m 3.7%）
——扩窗后远区能不能看清整体趋势、耗时涨多少，离线算一遍自然清晰。

方法：证据包重放 → 同一帧算两遍网格（换 GRID_Z_HI/GRID_NZ 模块常量，其余
口径不动：横向 ±10.5m、0.25m 格、同平面同容差）→ BEV 拼图（raw + 两版栅格，
灰=未知 绿=可走 红=不可走，16m 截断线标注）+ 背靠背计时（16m vs 30m）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_far_grid.py \
        --debug depth_debug_20261008_183813 --fids 4468,4404,3600 \
        --raw-dir <DebugPairWriter 的 raw 目录> --raw-seq 128,124,66
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import maaracing_master.plugins.speedrush.depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from probe_side_profile import replay_pts  # noqa: E402

TRACES = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
          / "speedrush" / "control_traces")
OUT = Path(__file__).resolve().parent / "far_grid_out"

_CSTATE_BGR = ((70, 70, 70), (0, 180, 0), (60, 60, 230))


def render_grid(grid: dg.DrivableGrid, hi: float, fid: int, tag: str,
                cut_at: float | None = None) -> np.ndarray:
    """栅格 BEV（x 横向 ±10.5m，z 3-hi m；灰=未知 绿=可走 红=不可走）。"""
    nz = grid.state.shape[0]
    px, pz = 7, 7
    pal = np.array(_CSTATE_BGR, np.uint8)
    body = np.repeat(np.repeat(pal[grid.state], pz, axis=0), px, axis=1)
    img = np.full((nz * pz + 22, grid.state.shape[1] * px, 3), 25, np.uint8)
    img[22:22 + nz * pz, :body.shape[1]] = body
    cv2.putText(img, f"{tag}  z=3-{hi:.0f}m  fid={fid}", (8, 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    for zm in (5, 10, 15, 20, 25):
        if zm >= hi:
            break
        y = 22 + int((zm - 3.0) / 0.25) * pz
        cv2.line(img, (0, y), (img.shape[1], y), (110, 110, 110), 1)
    if cut_at is not None:
        y = 22 + int((cut_at - 3.0) / 0.25) * pz
        cv2.line(img, (0, y), (img.shape[1], y), (0, 200, 255), 2)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--debug", default="depth_debug_20261008_183813")
    ap.add_argument("--fids", required=True, help="逗号分隔")
    ap.add_argument("--raw-dir", default="")
    ap.add_argument("--raw-seq", default="", help="与 --fids 对齐的 raw 序号")
    ap.add_argument("--hi", type=float, default=30.0, help="扩窗上限（米）")
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()

    cal = load_calib()
    ego = dg.DepthRoadObserver._load_ego_mask()
    dd = TRACES / a.debug
    evid = {}
    for p in sorted(dd.glob("*_evid.npz")):
        try:
            f = int(np.load(p)["fid"])
            if f >= 0:
                evid[f] = p
        except Exception:
            pass

    OUT.mkdir(parents=True, exist_ok=True)
    seqs = [int(x) for x in a.raw_seq.split(",") if x.strip()]
    fids = [int(x) for x in a.fids.split(",") if x.strip()]
    hi_nz = int(round((a.hi - dg.GRID_Z_LO) / dg.GRID_CELL))
    t_stat: dict[str, list[float]] = {"16m": [], f"{a.hi:.0f}m": []}

    for fid, seq in zip(fids, seqs + [0] * len(fids)):
        if fid not in evid:
            print(f"fid={fid} 无证据包，跳过")
            continue
        pts, fx, fy = replay_pts(evid[fid])

        # 背靠背计时（换窗后立刻换回，同一把尺）
        dg.GRID_Z_HI, dg.GRID_NZ = 16.0, 52
        for _ in range(a.repeat):
            t0 = time.perf_counter()
            ga = dg.drivable_grid_from_points(pts, fx, fy, ego, None)
            t_stat["16m"].append((time.perf_counter() - t0) * 1e3)
        dg.GRID_Z_HI, dg.GRID_NZ = float(a.hi), hi_nz
        for _ in range(a.repeat):
            t0 = time.perf_counter()
            gb = dg.drivable_grid_from_points(pts, fx, fy, ego, None)
            t_stat[f"{a.hi:.0f}m"].append((time.perf_counter() - t0) * 1e3)
        dg.GRID_Z_HI, dg.GRID_NZ = 16.0, 52

        # 远区（16m-hi）构成：扩窗版栅格的行 52 以后
        far = gb.state[52:, :]
        n_far = far.size
        u, d, b = ((far == s).sum() / n_far * 100 for s in (0, 1, 2))
        print(f"fid={fid}  远区 z=16-{a.hi:.0f}m 格构成: "
              f"未知 {u:.0f}%  可走 {d:.0f}%  不可走 {b:.0f}%   "
              f"16m 格 {ga.state.size}  {a.hi:.0f}m 格 {gb.state.size}")

        rows = []
        if a.raw_dir and seq:
            cand = Path(a.raw_dir) / f"{seq:04d}_raw.jpg"
            if cand.exists():
                raw = cv2.imread(str(cand))
                sc = 620 / raw.shape[1]
                rows.append(cv2.resize(raw, (620, int(raw.shape[0] * sc))))
        for g, hi, tag, cut in ((ga, 16.0, "产线", None),
                                (gb, a.hi, "扩窗", 16.0)):
            img = render_grid(g, hi, fid, tag, cut)
            sc = 620 / img.shape[1]
            rows.append(cv2.resize(img, (620, int(img.shape[0] * sc))))
        cv2.imwrite(str(OUT / f"far_{fid:05d}.png"), np.vstack(rows))

    print("\n背靠背计时（ms/帧）：")
    for k, v in t_stat.items():
        print(f"  {k:6s} min={min(v):6.1f}  p50={np.median(v):6.1f}  "
              f"max={max(v):6.1f}  (n={len(v)})")
    print(f"\n图已存 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
