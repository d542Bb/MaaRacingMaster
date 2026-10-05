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


def intrusion_clusters(state: np.ndarray, counts: np.ndarray,
                       lane_lo: float, lane_hi: float, lw: float
                       ) -> tuple[list[int], list[int]]:
    """检验 B 收紧口径：近场带 z∈[3,9)（金标近带可信域）内域 blocked 按
    4-连通分片，≥3 格的片算「侵入墙」；片内任一格 8 邻域含无点格
    （counts==0：挖洞/无点云）的片记为洞边残点——平面中位在洞边失真是
    已知伪影源，不算「可走地被标红」。返回（侵入墙片格数列表, 残点片格数列表）。"""
    x_lo = lane_lo * lw - dg.GRID_CELL / 2
    x_hi = lane_hi * lw + dg.GRID_CELL / 2
    ix_lo = max(0, int(np.ceil((x_lo + dg.GRID_X_MAX) / dg.GRID_CELL)))
    ix_hi = min(dg.GRID_NX, int(np.ceil((x_hi + dg.GRID_X_MAX) / dg.GRID_CELL)))
    sub = state[:NEAR_IZ, ix_lo:ix_hi] == dg.GRID_BLOCKED
    cnts_sub = counts[:NEAR_IZ, ix_lo:ix_hi]
    h, w = sub.shape
    seen = np.zeros_like(sub, bool)
    walls, frags = [], []
    wall_cells: list[tuple[int, int]] = []   # 侵入墙格（近场带局部坐标）
    for i in range(h):
        for j in range(w):
            if not sub[i, j] or seen[i, j]:
                continue
            stack, cells = [(i, j)], []
            seen[i, j] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < h and 0 <= xx < w and sub[yy, xx] \
                                and not seen[yy, xx]:
                            seen[yy, xx] = True
                            stack.append((yy, xx))
            near_hole = any(counts_or_zero(cnts_sub, cy, cx)
                            for cy, cx in cells)
            (frags if near_hole else walls).append(len(cells))
            if not near_hole:
                wall_cells.extend(cells)
    return walls, frags, wall_cells


def counts_or_zero(cnts: np.ndarray, y: int, x: int) -> bool:
    """格 (y,x) 的 8 邻域（含自身）是否存在无点格。"""
    h, w = cnts.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w and cnts[yy, xx] == 0:
                return True
    return False


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


def load_trace_steers(session: str) -> dict[int, float | None]:
    """trace jsonl → {seq: steer_norm}（经 SEQ2FID 誊录；无 trace 的会话返回空）。"""
    f = next(sbf.TRACES.glob(f"trace_{session[len('depth_debug_'):]}*.jsonl"),
             None)
    if f is None:
        return {}
    by_fid = {r["fid"]: r for r in
              (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
               if l.strip())}
    return {seq: by_fid.get(fid, {}).get("steer_norm")
            for seq, fid in sbf.SEQ2FID.get(session, {}).items()}


def c_seq_sheet(c_seqs: dict, lw: float, out_path: Path) -> None:
    """检验 C 目视拼版：每侧最近 blocked 边界横向位置的时序折线。
    层翻转的形态=两级台阶来回跳（跳幅≈黄线↔护栏层距）；供数缺口的形态=
    缺口两侧单边跳；几何移动=缓变。人工分形定案，不看 flip 率数字下结论。"""
    PANEL_W, PANEL_H = 640, 180
    Y_MAX_L = 3.5            # 纵轴 ±道（GRID_X_MAX 10.5m ≈ ±3.1 道）
    tiles = []
    for sess, sides in c_seqs.items():
        for key, seq in sides.items():
            img = np.full((PANEL_H, PANEL_W, 3), 255, np.uint8)
            for gy in range(0, PANEL_H, PANEL_H // 4):
                cv2.line(img, (0, gy), (PANEL_W - 1, gy), (220, 220, 220), 1)
            cv2.line(img, (0, PANEL_H // 2), (PANEL_W - 1, PANEL_H // 2),
                     (150, 150, 150), 1)
            n = max(len(seq), 2)
            pts = []
            for i, (x, straight) in enumerate(seq):
                if x is None:
                    continue
                lane = x / lw
                px = int(i * (PANEL_W - 1) / (n - 1))
                py = int(PANEL_H / 2 - lane / Y_MAX_L * (PANEL_H / 2 - 4))
                col = (0, 160, 0) if straight else (180, 180, 180)
                cv2.circle(img, (px, py), 2, col, -1)
                pts.append((px, py))
            for p1, p2 in zip(pts, pts[1:]):
                cv2.line(img, p1, p2, (120, 120, 120), 1)
            cv2.putText(img, f"{sess[12:]} {key} n={len(seq)}",
                        (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                        (0, 0, 0), 1)
            tiles.append(img)
    cols = 2
    rows_n = (len(tiles) + cols - 1) // cols
    canvas = np.full((rows_n * PANEL_H, cols * PANEL_W, 3), 30, np.uint8)
    for i, t in enumerate(tiles):
        rr, cc = divmod(i, cols)
        canvas[rr * PANEL_H:rr * PANEL_H + PANEL_H,
               cc * PANEL_W:cc * PANEL_W + PANEL_W] = t
    ok, buf = cv2.imencode(".png", canvas)
    if ok:
        out_path.write_bytes(buf.tobytes())
        print(f"检验 C 时序拼版 → {out_path}")


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
        # 检验 B 收紧口径：近场成片侵入（孤点/洞边残点不算「可走地被标红」）
        walls, frags, wall_cells = intrusion_clusters(grid.state, grid.counts,
                                                      span[0], span[1], lw)
        out["b_blocked_intrusion"].append({
            "frame": p.name, "walls": walls, "hole_frags": frags,
            "wall_cells": [[int(iz), int(ix)] for iz, ix in wall_cells]})
        u = frac["unknown"]
        tiles.append((p.name, grid.state, span, frac))
        print(f"A {p.name} 内域[{span[0]:+.2f},{span[1]:+.2f}]道: "
              f"unknown={u:.0%} drivable={frac['drivable']:.0%} "
              f"blocked={frac['blocked']:.0%}"
              f" | B 近场侵入墙{walls} 洞边残点{frags}")

    nA = len(out["a_blackout_coverage"])
    if nA:
        u_med = float(np.median([x["unknown"] for x in
                                 out["a_blackout_coverage"]]))
        verdict_a = "FAIL(unknown主导)" if u_med > 0.5 else "PASS"
        print(f"== 检验 A：{nA} 帧内域 unknown 中位 {u_med:.0%} → {verdict_a}")
        n_intr = sum(1 for x in out["b_blocked_intrusion"] if x["walls"])
        print(f"== 检验 B（收紧口径：近场带+连通≥3格+排除洞边）："
              f"{n_intr}/{nA} 帧有成片侵入墙"
              f"（明细见 json；目视裁决 → bev_sheet）")
        bev_sheet(tiles, lw, sbf.OUT / "s2b_bev_sheet.png")

    # ── 检验 C：3 会话逐帧边界层稳定性（直道子集为主判据：弯道/横移时
    #    墙相对车的绝对横向位置本来就在动，只有直道帧才能暴露「层跳变」）──
    seqs_all: dict[str, dict] = {}
    for sess in C_SESSIONS:
        t0 = time.monotonic()
        stems = sbf._session_stems(sess)
        steers = load_trace_steers(sess)
        seqs: dict[str, list] = {"L": [], "R": []}
        for st in stems:
            seq = int(st.name[1:])
            steer = steers.get(seq)
            straight = steer is not None and abs(steer) <= 0.35
            pts, fx, fy, ego, obj = sbf.assemble(st)
            rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                        object_mask=obj, fy=fy)
            grid = dg.drivable_grid_from_points(pts, fx, fy, ego=ego, obj=obj,
                                                coef=rd.coef)
            if grid.coef is None:
                continue
            for side, key in ((-1, "L"), (1, "R")):
                seqs[key].append((nearest_blocked(grid.state, side, lw),
                                  straight))

        def _stat(pairs: list[tuple[float, float]]) -> dict:
            d = np.array([abs(p2 - p1) / lw for p1, p2 in pairs]) \
                if pairs else np.array([])
            return {"n": len(pairs),
                    "flip率": round(float(np.mean(d > FLIP_LANE)), 3)
                    if d.size else None,
                    "帧间|Δ|中位_道": round(float(np.median(d)), 3)
                    if d.size else None,
                    "p90_道": round(float(np.percentile(d, 90)), 3)
                    if d.size else None}

        st_out = {}
        for key, seq in seqs.items():
            allp = [(a, b) for (a, _), (b, _) in zip(seq, seq[1:])
                    if a is not None and b is not None]
            strp = [(a, b) for (a, sa), (b, sb_) in zip(seq, seq[1:])
                    if sa and sb_ and a is not None and b is not None]
            jumps = sorted((abs(b - a) / lw for a, b in strp
                            if abs(b - a) / lw > 0.75), reverse=True)
            st_out[key] = {"全量": _stat(allp), "直道": _stat(strp),
                           "无blocked帧": sum(1 for x, _ in seq if x is None),
                           "直道大跳_道": [round(x, 2) for x in jumps[:8]]}
            print(f"C {sess[12:]} {key}: {st_out[key]}")
        out["c_stability"][sess] = st_out
        seqs_all[sess] = seqs
        print(f"   ({time.monotonic()-t0:.0f}s)")
    out["c_seqs"] = {sess: {k: [(None if x is None else round(x, 2), bool(s))
                                for x, s in seq]
                            for k, seq in seqs_all[sess].items()}
                     for sess in C_SESSIONS}

    dst = sbf.OUT / "s2b_falsify.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    c_seq_sheet(out["c_seqs"], lw, sbf.OUT / "s2b_c_seq_sheet.png")
    print(f"明细 → {dst}")


if __name__ == "__main__":
    main()
