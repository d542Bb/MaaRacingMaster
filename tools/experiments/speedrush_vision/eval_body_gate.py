# -*- coding: utf-8 -*-
"""缘体证据门离线评估器：在 census.json 的剖面上试装判据，全语料出前后对照。

判据（2026-10-03 普查定案）：穿越候选须出示**缘体证据**——
  脸：穿越格起 3 格内存在单格增量 ≥ EDGE_FACE_MIN（真缘台阶 0.033~0.5 实测，
      缓坡逐格 ≤0.026）；或
  平台：连续 ≥3 格 |增量| ≤ EDGE_PLAT_EPS 且高度 ≥ EDGE_PLAT_K×阈
      （真 kerb 远场 smeared 平台 0.145=1.7×阈；缓坡肩台仅 1.1~1.2×阈）。
两者皆无 = 爬坡越阈的残差缓坡，跳过继续向外找。

只读 census.json（census_crossbin.py 产物），不改产线；判据与 _scan_side 的
扫描/插值/选侧逻辑逐位同构，用于全语料预估与金标复核。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

EDGE_FACE_MIN = 0.03
EDGE_FACE_CELLS = 3
EDGE_PLAT_EPS = 0.004
EDGE_PLAT_K = 1.5
EDGE_TAIL_FRAC = 0.6
EDGE_MATCH_M = 0.8
LANE_SIDE_GATE = 0.15

CENSUS = Path(".workbuddy-ai/speedrush_census/census.json")


def body_evidence(ph: list[float], i: int, thr: float) -> bool:
    """穿越候选（对 i, i+1）的缘体证据：脸或平台。steps[0]=对步增。"""
    steps = np.diff(np.asarray(ph[i:], dtype=float))
    if steps[:EDGE_FACE_CELLS].size and steps[:EDGE_FACE_CELLS].max() >= EDGE_FACE_MIN:
        return True
    h = np.asarray(ph[i + 1:], dtype=float)          # h[k] = 对后第 k 格高度
    need = EDGE_PLAT_K * thr
    run = 0
    for k, s in enumerate(steps):
        run = run + 1 if abs(s) <= EDGE_PLAT_EPS else 0
        if run >= 3 and h[k - 2:k + 1].min() >= need:
            return True
    return False


def scan(px: list[float], ph: list[float], thr: float) -> float:
    """_scan_side 同构扫描（含尾部持续门 + 新缘体证据门）。"""
    g = -1
    for j, v in enumerate(ph):
        if v <= thr:
            g = j
            break
    if g < 0:
        return np.nan
    for i in range(g + 1, len(px) - 1):
        if ph[i] > thr and ph[i + 1] > thr:
            tail = ph[i + 2:]
            if len(tail) and (np.asarray(tail) > thr).mean() < EDGE_TAIL_FRAC:
                continue
            if not body_evidence(ph, i, thr):
                continue
            f = (thr - ph[i - 1]) / (ph[i] - ph[i - 1])
            return float(px[i - 1] + f * (px[i] - px[i - 1]))
    return np.nan


def pick(xs: list[tuple[float, float]]) -> float | None:
    """_side 同构选侧：最近一致对，否则中位数。"""
    if not xs:
        return None
    smp = sorted(xs)
    for a, b in zip(smp, smp[1:]):
        if abs(a[1] - b[1]) <= EDGE_MATCH_M:
            return a[1]
    return float(np.median([x for _, x in smp]))


def main() -> None:
    lane_w = load_calib().lane_w_m
    recs = json.loads(CENSUS.read_text(encoding="utf-8"))
    n_keep = n_kill = 0
    print(f"{'tag':<36} {'L旧→新(车道)':>18} {'R旧→新(车道)':>18}")
    for r in recs:
        def side_new(side: int) -> float | None:
            xs = []
            for b in r["bins"]:
                if b["side"] != side:
                    continue
                x = scan(b["prof"]["x"], b["prof"]["h"], b["prof"]["thr"])
                if np.isfinite(x):
                    xs.append((b["zc"], round(float(x), 3)))
            v = pick(xs)
            return None if v is None else v / lane_w

        new_l, new_r = side_new(-1), side_new(1)
        # 车道量换算沿用读数侧别门：L 负 R 正，量须在自己一侧
        if new_l is not None and new_l > -LANE_SIDE_GATE:
            new_l = None
        if new_r is not None and new_r < LANE_SIDE_GATE:
            new_r = None
        old_l, old_r = r["L"], r["R"]

        def fmt(a, b):
            if a is None and b is None:
                return "   --→--   "
            sa = f"{a:+.2f}" if a is not None else "--"
            sb = f"{b:+.2f}" if b is not None else "--"
            mark = "*" if ((a is None) != (b is None)
                           or (a is not None and b is not None
                               and abs(a - b) > 0.8)) else " "
            return f"{sa:>6}→{sb:>6}{mark}"

        for b in r["edges"]:
            if b["x"] is not None:
                n_keep += 1
        for b in r["bins"]:
            if b["x"] is not None and not np.isfinite(
                    scan(b["prof"]["x"], b["prof"]["h"], b["prof"]["thr"])):
                n_kill += 1
        print(f"{r['tag']:<36} {fmt(old_l, new_l):>18} {fmt(old_r, new_r):>18}")
    print(f"\n检出穿越 {n_keep} 个，其中被缘体证据门否决 {n_kill} 个")


if __name__ == "__main__":
    main()
