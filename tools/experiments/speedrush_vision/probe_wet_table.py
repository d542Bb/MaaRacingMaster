# -*- coding: utf-8 -*-
"""212349 全量逐帧判据表（2026-10-11）：湿地局 depth_debug 全部证据包过当前链
拟合+质量门（复用 probe_curve_plane 的重建与判据拆解，fy=×720 生产口径），
输出逐帧表——同局同链内找「被拒帧」的共同点，钉死或推翻湿反射假设。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_wet_table.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from probe_curve_plane import DD, gate_parts, rebuild  # noqa: E402


def main() -> None:
    pkgs = sorted(re.match(r"d(\d{5})", os.path.basename(p)).group(0)
                  for p in glob.glob(os.path.join(DD, "d*_evid.npz")))
    print(f"共 {len(pkgs)} 包\n")
    print(f"{'pkg':8} {'fid':>5} {'判定':>4} {'roll°':>7} {'pitch°':>7} "
          f"{'cam_h':>6} {'内点率':>6} {'残差cm':>7}   近4-8/中8-11/远11-14 残差(m)|内点率")
    n_ok = n_rej = 0
    for pkg in pkgs:
        pts, fx, fy, obj, fid = rebuild(pkg)
        coef, info = gate_parts(pts, fx, fy, obj)
        if not isinstance(info, dict):
            print(f"{pkg:8} {fid:>5} 拟合失败  ({info})")
            continue
        verdict = "过" if info["ok"] else "拒"
        n_ok += info["ok"]
        n_rej += not info["ok"]
        bd = "  ".join(f"{lo}-{hi}:{r:.3f}|{q:.2f}"
                       for lo, hi, r, q in info["bands"])
        cam_h = abs(float(coef[2])) if coef is not None else float("nan")
        print(f"{pkg:8} {fid:>5} {verdict:>4} {info['roll']:+7.2f} {info['pitch']:+7.2f} "
              f"{cam_h:6.2f} {info['rate']:6.2f} {info['resid']*100:7.1f}   {bd}  {info['why']}")
    print(f"\n过 {n_ok} / 拒 {n_rej}")


if __name__ == "__main__":
    main()
