# -*- coding: utf-8 -*-
"""留点率下限探针：2×2 矩阵「晕影治理」第二候选（协议预注册，只评 A 读法）。

守卫假设：两轮剔点后留点率低（少数派留点）的拟合不可信，设下限可摘掉坏读数。
本探针回答两个问题：
1. **因果方向**：留点率与 |dev| 是否相关——坏读数（|dev|>0.5 车道）的留点率
   是否显著低于好读数？不相关则守卫治不了病，直接关闭；
2. **网格**：下限 ∈ {0.5, 0.6, 0.7, 0.8} 的覆盖/dev/坏率 vs 无下限基线。

拟合/守卫按产码 _fit/_pass 口径在探针内复刻（probe_readband 先例；真源归
depth_geo）：源行 y−y_h≥FIT_FLOOR_PX、两轮剔点（FIT_RESID_PX=30）、conv/resid/
侧别/门本底四守卫。金标 44 帧、门 0.08。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_retention_floor.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_depth as pd  # noqa: E402
import probe_crop_quality as pcq  # noqa: E402

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402


from probe_mech_score import gold_lane  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
FLOORS = (0.5, 0.6, 0.7, 0.8)


def fit_with_retention(inner: dict[int, int]):
    """产码 _fit 复刻 + 返回留点率（剔除数/源行数，剔点轮口径）。→ None/{"dead"} 或
    {conv, resid, lane, y_ref, retention}。"""
    ys_all = np.array(sorted(inner), float)
    if len(ys_all) < dg.FIT_MIN_ROWS:
        return None
    ys = ys_all[ys_all >= CAL.y_h + dg.FIT_FLOOR_PX]
    if len(ys) < dg.FIT_MIN_SRC:
        return {"dead": True}
    xs = np.array([inner[int(y)] for y in ys], float)
    sl, ic = np.polyfit(ys, xs, 1)
    keep = np.abs(xs - (sl * ys + ic)) <= dg.FIT_RESID_PX
    if keep.sum() >= dg.FIT_MIN_ROWS:
        sl, ic = np.polyfit(ys[keep], xs[keep], 1)
        ys_f, xs_f = ys[keep], xs[keep]
    else:
        ys_f, xs_f = ys, xs
    med_x = float(np.median(xs_f))
    y_ref = float(np.median(ys_f))
    lane = x_lane_of(int(round(med_x)), int(round(y_ref)), CAL)
    return {"dead": False,
            "conv": abs(float(sl * CAL.y_h + ic) - CAL.vpx),
            "resid": float(np.median(np.abs(xs_f - (sl * ys_f + ic)))),
            "lane": lane, "y_ref": y_ref,
            "retention": float(keep.sum()) / float(len(ys))}


def main() -> None:
    labels = [r for r in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))]
    per_side = {"L": [], "R": []}
    for r in labels:
        p = Path(r["path"])
        cache = pd.OUT / "npy" / f"{pcq.frame_key(p)}__d336q4f16.npy"
        if not cache.exists():
            continue
        m = np.load(cache).astype(np.float32)
        r_map, blocks = dg._rel_and_blocks(m, EGO, gate=dg.GATE)
        rec = {"r": r, "m": m}
        for side, tkey in (("L", 4), ("R", 5)):
            cands = sorted((b for b in blocks if b[tkey]),
                           key=lambda b: b[1] - b[0], reverse=True)
            entry = {}
            for _y0, _y1, il, ir, _tl, _tr in cands:
                inner = il if side == "L" else ir
                if not inner:
                    continue
                f = fit_with_retention(inner)
                if f is None:
                    break                      # 产码口径：行不足 break
                if f.get("dead") or not dg._pass(f, side):
                    continue                   # 产码口径：守卫失败换下一候选
                if not dg._baseline_ok(m, inner, side, f["y_ref"], dg.GATE):
                    continue
                entry = {**f, "inner": inner}
                break
            glane, _ = gold_lane(r, side.lower())
            if glane is not None:
                entry["gold"] = glane
            per_side[side].append(entry)

    for side in ("L", "R"):
        ents = [e for e in per_side[side] if e.get("lane") is not None
                and e.get("gold") is not None]
        print(f"\n[{side}] 过守卫且有金标：{len(ents)} 帧")
        good = [e["retention"] for e in ents if abs(e["lane"] - e["gold"]) <= 0.3]
        bad = [e["retention"] for e in ents if abs(e["lane"] - e["gold"]) > 0.5]
        mid = len(ents) - len(good) - len(bad)
        def st(xs):
            return f"n={len(xs)} 中位={np.median(xs):.2f}" if xs else "n=0"
        print(f"  留点率分布：好(|dev|≤0.3) {st(good)} | 中间 {mid} 帧 | "
              f"坏(>0.5) {st(bad)}")

        def pct(xs, q):
            xs = sorted(xs)
            return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]
        for floor in (0.0,) + FLOORS:
            kept = [e for e in ents
                    if floor == 0.0 or e["retention"] >= floor]
            devs = [e["lane"] - e["gold"] for e in kept]
            nb = sum(abs(d) > 0.5 for d in devs)
            print(f"  下限≥{floor:.1f}: 覆盖 {len(kept)}/44  "
                  f"dev p50/p90 {pct(devs,.5):+.2f}/{pct([abs(d) for d in devs],.9):.2f}"
                  f"  坏 {nb}/{len(devs)}")


if __name__ == "__main__":
    main()
