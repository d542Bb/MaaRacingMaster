# -*- coding: utf-8 -*-
"""第二刀 S2-B 证伪检验探针（README 方案稿三检验，先于任何产线代码）。

考卷：检验 A/B 用黑视金标 10 帧（gold_labels 唯一帧）；检验 C 用 3 个代表
会话逐帧（栅格调查 2026-10-04 同款帧选：雨天/宽路/弯道）。

- **A 黑视供数覆盖**：金标碰撞边界内域（lane∈[gold_L, gold_R]、z 3~16m 全带）
  的三态占比——unknown 主导（>50%）则轨迹裁决在黑视时无证据可依，设计作废。
  近似口径：金标线只在近带标注，内域按近带 lane 常数延伸到全 z 带（弯道失真，
  如实标注）。
- **B blocked 误侵入**：内域 blocked 格 = 可走地被否决。已知风险=0.15m 容差与
  kerb 顶同量级。逐帧列内域 blocked 的最小 |lane| 与距金标缘的距离。
- **C 边界层稳定性**：z∈[3,9] 带内每侧最近 blocked 边界的横向位置序列，
  帧间 |Δ| 与翻转率（|Δlane|>0.5 道）——层翻转时固定内收不成立（护栏层裁决
  的成设立据）。

口径：全部产线同源（assemble 同 select_blackout_frames；grid 同
depth_geo.drivable_grid_from_points，coef 与 reading 共享同一平面；车道量锚
gate0 lane_w_m）。

用法：
  .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_s2b_falsify.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
import gold_blackout_score as gbs  # noqa: E402  （金标折算单一实现）
import select_blackout_frames as sbf  # noqa: E402

import cv2  # noqa: E402

C_SESSIONS = ["depth_debug_20261004_175855",   # 雨天
              "depth_debug_20261004_140930",   # 晴天宽路
              "depth_debug_20261002_211445"]   # 弯道
FLIP_LANE = 0.5          # 检验 C 的层翻转判据（道）
NEAR_IZ = 24             # z∈[3,9) = (9-3)/0.25 格行


def gold_span(r: dict, coef, fx: float, fy: float, lane_w_m: float
              ) -> tuple[float, float] | None:
    """金标碰撞边界内域的车道区间（近带三行中位）。任一侧不可折算 → None。"""
    lo, hi = [], []
    for side, acc in (("l", lo), ("r", hi)):
        lanes = [v for v in gbs.gold_lanes_at(r, side, coef, fx, fy,
                                              lane_w_m).values()
                 if v is not None]
        if not lanes:
            return None
        acc.append(float(np.median(lanes)))
    return min(lo), max(hi)


def grid_lane_span(state: np.ndarray, lane_lo: float, lane_hi: float,
                   lane_w_m: float, iz_lo: int = 0, iz_hi: int = dg.GRID_NZ
                   ) -> dict[str, int]:
    """内域三态计数（格心 lane∈[lo,hi]、iz∈[lo,hi)）。"""
    x_lo = lane_lo * lane_w_m - dg.GRID_CELL / 2
    x_hi = lane_hi * lane_w_m + dg.GRID_CELL / 2
    ix_lo = max(0, int(np.ceil((x_lo + dg.GRID_X_MAX) / dg.GRID_CELL)))
    ix_hi = min(dg.GRID_NX, int(np.ceil((x_hi + dg.GRID_X_MAX) / dg.GRID_CELL)))
    sub = state[iz_lo:iz_hi, ix_lo:ix_hi]
    return {k: int((sub == v).sum()) for k, v in
            (("unknown", dg.GRID_UNKNOWN), ("drivable", dg.GRID_DRIVABLE),
             ("blocked", dg.GRID_BLOCKED))}


def nearest_blocked(state: np.ndarray, side: int, lane_w_m: float
                    ) -> float | None:
    """z∈[3,9) 带内该侧最近 blocked 格的格心 x_m；无 blocked → None。"""
    sub = state[:NEAR_IZ]
    for ix in (range(dg.GRID_NX) if side < 0 else range(dg.GRID_NX - 1, -1, -1)):
        x = -dg.GRID_X_MAX + (ix + 0.5) * dg.GRID_CELL
        if (side < 0) != (x < 0):
            continue
        if (sub[:, ix] == dg.GRID_BLOCKED).any():
            return float(x)
    return None


def bev_sheet(tiles: list[tuple[str, np.ndarray, tuple[float, float] | None,
                              dict[str, int] | None]], lw: float,
              out_path: Path) -> None:
    """检验 B 目视拼版：每帧 BEV 三态图 + 金标内域框。blocked 落框内才谈侵入。"""
    SC = 8                      # 每格 8px
    W, H = dg.GRID_NX * SC, dg.GRID_NZ * SC
    tiles_img = []
    for name, state, span, cnt in tiles:
        img = np.full((dg.GRID_NZ, dg.GRID_NX, 3), 40, np.uint8)
        img[state == dg.GRID_DRIVABLE] = (60, 160, 60)
        img[state == dg.GRID_BLOCKED] = (60, 60, 220)
        if span is not None:   # 金标缘两条竖线（青）
            for lane in span:
                ix = int(round((lane * lw + dg.GRID_X_MAX) / dg.GRID_CELL
                               - 0.5))
                cv2.line(img, (ix, 0), (ix, dg.GRID_NZ - 1), (255, 255, 0), 1)
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_NEAREST)
        cap = f"{name} u={cnt['unknown']:.0%} d={cnt['drivable']:.0%}" \
              f" b={cnt['blocked']:.0%}" if cnt else name
        cv2.rectangle(img, (0, 0), (W - 1, 18), (0, 0, 0), -1)
        cv2.putText(img, cap, (3, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (255, 255, 255), 1)
        tiles_img.append(img)
    cols = 2
    rows_n = (len(tiles_img) + cols - 1) // cols
    canvas = np.full((rows_n * H, cols * W, 3), 20, np.uint8)
    for i, t in enumerate(tiles_img):
        rr, cc = divmod(i, cols)
        canvas[rr * H:rr * H + H, cc * W:cc * W + W] = t
    ok, buf = cv2.imencode(".png", canvas)
    if ok:
        out_path.write_bytes(buf.tobytes())
        print(f"检验 B 目视拼版 → {out_path}")


def main() -> None:
    cal = load_calib()
    lw = cal.lane_w_m
    out: dict = {"a_blackout_coverage": [], "b_blocked_intrusion": [],
                 "c_stability": {}}

    # ── 检验 A/B：黑视金标 10 帧 ─────────────────────────────────────
    labels = list(csv.DictReader((sbf.OUT / "gold_labels.csv")
                                 .open(encoding="utf-8")))
    seen = set()
    tiles = []
    for r in labels:
        p = Path(r["path"])
        if p in seen:
            continue
        seen.add(p)
        stem = p.with_name(p.name[:-len("_frame.jpg")])
        pts, fx, fy, ego, obj = sbf.assemble(stem)
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                    object_mask=obj, fy=fy)
        span = (gold_span(r, rd.coef, fx, fy, lw) if rd.coef is not None
                else None)
        if span is None:
            print(f"A/B {p.name}: 金标内域不可折算（平面失败/侧缺），跳过")
            continue
        grid = dg.drivable_grid_from_points(pts, fx, fy, ego=ego, obj=obj,
                                            coef=rd.coef)
        cnt = grid_lane_span(grid.state, span[0], span[1], lw)
        tot = sum(cnt.values())
        frac = {k: v / tot for k, v in cnt.items()}
        out["a_blackout_coverage"].append({"frame": p.name, "span": span,
                                           **{k: round(v, 3)
                                              for k, v in frac.items()}})
        # 内域 blocked 的最近 |lane|（距路心最近 = 侵入最深）
        intr = None
        if cnt["blocked"]:
            x_lo, x_hi = span[0] * lw, span[1] * lw
            ix_lo = max(0, int(np.ceil((x_lo + dg.GRID_X_MAX) / dg.GRID_CELL)))
            ix_hi = min(dg.GRID_NX,
                        int(np.ceil((x_hi + dg.GRID_X_MAX) / dg.GRID_CELL)))
            sub = grid.state[:, ix_lo:ix_hi] == dg.GRID_BLOCKED
            if sub.any():
                ixs = np.nonzero(sub.any(axis=0))[0]
                xs = (-dg.GRID_X_MAX + (ixs + ix_lo + 0.5) * dg.GRID_CELL)
                intr = float(np.min(np.abs(xs)) / lw)
        out["b_blocked_intrusion"].append({"frame": p.name, "blocked_in": cnt[
            "blocked"], "min_abs_lane": None if intr is None else round(intr, 3),
            "gold_edge": round(min(abs(span[0]), abs(span[1])), 3)})
        u = frac["unknown"]
        tiles.append((p.name, grid.state, span, frac))
        print(f"A {p.name} 内域[{span[0]:+.2f},{span[1]:+.2f}]道: "
              f"unknown={u:.0%} drivable={frac['drivable']:.0%} "
              f"blocked={frac['blocked']:.0%}"
              + (f" | B 侵入最深 {intr:.2f} 道" if intr else " | B 无侵入"))

    nA = len(out["a_blackout_coverage"])
    if nA:
        u_med = float(np.median([x["unknown"] for x in
                                 out["a_blackout_coverage"]]))
        verdict_a = "FAIL(unknown主导)" if u_med > 0.5 else "PASS"
        print(f"== 检验 A：{nA} 帧内域 unknown 中位 {u_med:.0%} → {verdict_a}")
        n_intr = sum(1 for x in out["b_blocked_intrusion"]
                     if x["blocked_in"] > 0)
        print(f"== 检验 B：{n_intr}/{nA} 帧内域 blocked 侵入"
              f"（侵入深度明细见 json；目视裁决 → bev_sheet）")
        bev_sheet(tiles, lw, sbf.OUT / "s2b_bev_sheet.png")

    # ── 检验 C：3 会话逐帧边界层稳定性 ───────────────────────────────
    for sess in C_SESSIONS:
        t0 = time.monotonic()
        stems = sbf._session_stems(sess)
        seqs = {"L": [], "R": []}   # (idx, x_m|None)
        for i, st in enumerate(stems):
            pts, fx, fy, ego, obj = sbf.assemble(st)
            rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                        object_mask=obj, fy=fy)
            grid = dg.drivable_grid_from_points(pts, fx, fy, ego=ego, obj=obj,
                                                coef=rd.coef)
            if grid.coef is None:
                continue
            for side, key in ((-1, "L"), (1, "R")):
                seqs[key].append(nearest_blocked(grid.state, side, lw))
        st_out = {}
        for key, seq in seqs.items():
            pairs = [(seq[i], seq[i + 1]) for i in range(len(seq) - 1)
                     if seq[i] is not None and seq[i + 1] is not None]
            d = np.array([abs(p2 - p1) / lw for p1, p2 in pairs]) \
                if pairs else np.array([])
            gaps = sum(1 for x in seq if x is None)
            st_out[key] = {"n": len(seq), "无blocked帧": gaps,
                           "flip率": round(float(np.mean(d > FLIP_LANE)), 3)
                           if d.size else None,
                           "帧间|Δ|中位_道": round(float(np.median(d)), 3)
                           if d.size else None,
                           "p90_道": round(float(np.percentile(d, 90)), 3)
                           if d.size else None}
            print(f"C {sess[12:]} {key}: {st_out[key]}")
        out["c_stability"][sess] = st_out
        print(f"   ({time.monotonic()-t0:.0f}s)")

    dst = sbf.OUT / "s2b_falsify.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"明细 → {dst}")


if __name__ == "__main__":
    main()
