# -*- coding: utf-8 -*-
"""对宽门拒绝帧的逐行根因探针（2026-10-17 方向 3 任务 2 前置：确定根因）。

**问题**：走廊缺席分账显示拒绝 100% 是对宽一致性门，且拒绝拍上深度证据
"看不全"路面（dgeo 宽度 P50 2.3~3.2 道 vs 正常 4.1~4.2）。竞争假设：
(a) 近场视场裁剪（MoGe 焦距 570px/1280 宽，视场 ~97°，z<10.7m 看不到远侧
路缘——常量背景）；(b) 阴影让可行驶分类失败（桥段大阴影——触发器）；
(c) 高架段护栏外地面混入（虚宽批）。本探针把每帧栅格**逐行**（深度 z）
dump 出来，对照两把独立标尺判行塌原因：
  - 视场裁剪上限（第一性：x_lim = z·tan(hfov/2)，hfov 由原生焦距+图像宽算）
    ——行内可行驶区间贴着 x_lim = 被视场裁；远小于 x_lim = 分类失败/遮挡；
  - 帧亮度（原始帧逐深度带的均值亮度）——低亮度带与塌陷带相关 = 阴影。

**口径**（照抄现役实现，只 import 不改）：DepthRoadObserver.observe_debug
全管线（MoGe → 平面 → 找边/栅格并行），region_reading(grid, lane_w) 复现
生产 master 模式对宽——与 trace ro_pair_w 对账（复现性检查）。

输出：row_collapse_out/ 下 per-frame JSON（逐行 z / 可行驶区间 / 点数 /
视场上限 / 亮度）+ stdout 摘要。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_row_collapse.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    GRID_DRIVABLE, GRID_X_MAX, GRID_Z_LO,
    DepthRoadObserver, region_reading)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DBG = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = Path(__file__).resolve().parent / "row_collapse_out"

# ── 选帧（seq→fid 由 raw mtime 对齐 trace 读得，见会话分析）──
FRAMES = [
    # (run, raw 文件, 期望状态, trace 对宽, trace W)
    ("20261017_221748_p1", "0059_raw.jpg", "REJ", 3.2544, 4.4379),
    ("20261017_221748_p1", "0065_raw.jpg", "REJ", 5.3254, 4.4379),
    ("20261017_221748_p1", "0079_raw.jpg", "REJ", 3.4763, 4.4379),
    ("20261017_221748_p1", "0091_raw.jpg", "REJ", 2.8846, 4.4379),
    ("20261017_221748_p1", "0094_raw.jpg", "REJ", 3.3284, 4.4379),
    ("20261017_221748_p1", "0058_raw.jpg", "ok", 4.5858, 4.4379),
    ("20261017_221748_p1", "0089_raw.jpg", "ok", 4.8817, 4.4379),
    ("20261017_221748_p1", "0100_raw.jpg", "ok", 4.4379, 4.4379),
    ("20261017_221833_p2", "0035_raw.jpg", "REJ", 4.8077, 3.9941),
    ("20261017_221833_p2", "0037_raw.jpg", "REJ", 4.8077, 3.9941),
    ("20261017_221833_p2", "0039_raw.jpg", "REJ", 4.8817, 3.9941),
    ("20261017_221833_p2", "0033_raw.jpg", "ok", 4.5858, 3.9941),
    ("20261017_221833_p2", "0057_raw.jpg", "ok", 4.8817, 4.142),
    ("20261017_221833_p2", "0073_raw.jpg", "ok", 4.8077, 4.142),
]

NEAR_LIM_Z = 10.7   # 视场裁剪带（z 小于此的行看不到远侧路缘，第一性算得）


def main() -> None:
    OUT.mkdir(exist_ok=True)
    cal = load_calib()
    sess = dg.load_session(DEPTH_MODEL_FILE)
    obs = DepthRoadObserver(sess, cal)
    results = []
    for run, raw, expect, tr_pw, tr_w in FRAMES:
        path = DBG / run / "raw" / raw
        img = cv2.imread(str(path))
        assert img is not None, path
        frame = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        reading, evid = obs.observe_debug(frame, fid=0)
        grid = evid.get("grid")
        reg = region_reading(grid, cal.lane_w_m) if grid is not None else None
        pair_w = (None if reg is None or reg.left_edge_lane is None
                  or reg.right_edge_lane is None
                  else reg.right_edge_lane - reg.left_edge_lane)
        # 原生焦距 → 水平视场 → 逐行视场裁剪上限（第一性独立标尺）
        fy_native = evid["fy"]
        # 点图宽 = 原生分辨率；fx/fy 为归一化焦距（×原生宽 → 像素焦距）
        nat_w = evid["pts"].shape[1]
        f_px = fy_native * nat_w
        hfov_half_tan = (nat_w / 2.0) / f_px
        rows = []
        state = grid.state
        counts = grid.counts
        for iz in range(state.shape[0]):
            z = GRID_Z_LO + (iz + 0.5) * 0.25
            driv = state[iz] == GRID_DRIVABLE
            xs = np.where(driv)[0]
            if len(xs):
                x_lo = -GRID_X_MAX + (xs.min() + 0.5) * 0.25
                x_hi = -GRID_X_MAX + (xs.max() + 0.5) * 0.25
            else:
                x_lo = x_hi = None
            # 该深度带的帧亮度（像素 Z 落在本行的原始帧区均值；带掩码在
            # 原生点图系，放大到帧尺寸取亮度）
            Z = evid["pts"][:, :, 2]
            band = np.abs(Z - z) <= 0.125
            band_full = cv2.resize(band.astype(np.uint8),
                                   (frame.shape[1], frame.shape[0]),
                                   interpolation=cv2.INTER_NEAREST).astype(bool)
            bright = float(frame[band_full].mean()) if band_full.any() else None
            rows.append({
                "z": round(z, 2),
                "drivable_n": int(driv.sum()),
                "x_lo_m": None if x_lo is None else round(x_lo, 2),
                "x_hi_m": None if x_hi is None else round(x_hi, 2),
                "width_m": None if x_lo is None else round(x_hi - x_lo, 2),
                "fov_lim_m": round(z * hfov_half_tan, 2),
                "pts_n": int(counts[iz].sum()),
                "brightness": None if bright is None else round(bright, 1),
            })
        rec = {
            "run": run, "raw": raw, "expect": expect,
            "trace_pair_w": tr_pw, "trace_w": tr_w,
            "region_pair_w": None if pair_w is None else round(pair_w, 3),
            "dgeo_edges": [reading.left_edge_lane, reading.right_edge_lane]
            if reading is not None else None,
            "rows": rows,
        }
        results.append(rec)
        ok = "REJ" if (pair_w is not None and tr_w is not None
                       and abs(pair_w - tr_w) > 0.2 * tr_w) else "ok"
        print(f"{raw} [{expect}] 复现 region 对宽={pair_w and round(pair_w, 2)} "
              f"(trace {tr_pw}, 复现判定={ok}) dgeo={reading and (reading.left_edge_lane, reading.right_edge_lane)}")
    (OUT / "row_collapse.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {OUT / 'row_collapse.json'}")


if __name__ == "__main__":
    main()
