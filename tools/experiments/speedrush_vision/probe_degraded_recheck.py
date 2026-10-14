# -*- coding: utf-8 -*-
"""降级补考离线对账（2026-10-11）：212349 全 82 证据包产线同链复算
（rebuild → reading_from_points），对比补考上线前后的逐帧判定。

补考只挂在主门拒分支之后：过门帧行为逐位不变；旧判定「拒」= 新判定
{降级放行, 仍拒} 之和。放行数量是预期记录不是验收目标（预期 ~2 帧接近
干净的被收编；59 帧旁路过关帧不走此路径不受影响）。

用法（仓库根 .venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_degraded_recheck.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from maaracing_master.plugins.speedrush.depth_geo import reading_from_points  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from tools.experiments.speedrush_vision.probe_curve_plane import (  # noqa: E402
    DD, rebuild)


def main() -> None:
    cal = load_calib()
    pkgs = sorted(p.name[:6] for p in Path(DD).glob("d*_evid.npz"))
    n_degraded = n_new_rej = 0
    for pkg in pkgs:
        pts, fx, fy, obj, fid = rebuild(pkg)
        rd = reading_from_points(pts, fx, cal, obj, fy=fy)
        if rd.degraded:
            n_degraded += 1
            tag = "降级放行  <== 改判"
        elif rd.coef is None:
            n_new_rej += 1
            tag = "仍拒"
        else:
            tag = "过门"
        print(f"{pkg} fid={fid:5d} {tag}"
              + (f"  rejects={';'.join(rd.rejects)}" if rd.degraded else ""),
              flush=True)
    print(f"\n合计 {len(pkgs)} 包：补考放行 {n_degraded}，仍拒 {n_new_rej}，"
          f"过门 {len(pkgs) - n_degraded - n_new_rej}（旧拒 "
          f"{n_degraded + n_new_rej}）")


if __name__ == "__main__":
    main()
