# -*- coding: utf-8 -*-
"""走廊收敛影子对比（2026-10-20 用户定案的实验）：同一批实帧上并排跑两条走廊链路，
量「栅格单真源 + 跨帧平滑」（B）与「栅格近场 + 远区吃产线找边读数」（C）
能否替代「栅格 + 墙融合」（A=产线现状）。C=「找墙搬进栅格远行」的实验版：
远区方向特征不新造，用产线逐箱找边扫描（ZBIN 已扫到 30m、近场读数只吃 ≤16m）
的远箱读数，标推断级供数。

- **A 现状**：WallTrack 逐帧累积（产线语义）→ build_corridor（绿区+墙+空气融合）。
- **B 收敛**：不喂墙——build_corridor 的墙边界恒空，走廊退化为栅格投影
  （近场行=绿区实测，远场行=空气推断）；center/width 跨帧滑窗中位平滑
  （模拟消费端时间平滑，接住栅格逐帧翻面的已知坑）。

**已知偏差（如实记）**：原始帧是采样子集（每 ~7 拍一帧），A 的墙槽位累积与
B 的滑窗都被低采样率拉疏——本实验是方向级初探，不是逐拍复刻。结论只看相对量。

指标（逐帧，两链路同算）：
- 翻面率：相邻帧 |Δcenter| > 0.5 道（双方 center 均非 None）；
- 双侧可见率：走廊两侧边界均非 unknown 的帧占比；
- 远区覆盖：车头系 z≥10m 的带里边界非 unknown 的占比（方向级供数活着多少）；
- center 中位帧间抖动。

运行（PYTHONPATH 指向仓库根，脚本内不做向上遍历）：
    PYTHONPATH=. python tools/experiments/speedrush_vision/probe_corridor_converge.py \
        --dir "<APPDATA>/MaaRacingMaster/debug/speedrush/20261020_202843_p2"
输出：<数据目录> 下的 corridor_converge_summary.md 与 corridor_converge_frames.jsonl。
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.speedrush import corridor as cr
from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush.wall_track import WallTrack
from maaracing_master.plugins.speedrush.world_model import load_calib

SMOOTH_WIN = 3     # B/C 的滑窗帧数（采样帧 ≈ 每帧 1.7s，3 帧 ≈ 5s；方向级）
FAR_Z0 = 10.0      # 远区带下界（车头系，米）
FLIP_LANE = 0.5    # 翻面判据（道）
FAR_Z_CAM = 15.0   # C 远区带下界（相机系；NEAR_Z=16 的带 15-18 从此起吃找边远箱）
FAR_CARRY_M = 6.0  # C 带内无找边读数时，向已测边借界的最大纵深（米，标推断）

WEIGHTS_DIR = Path(dg.__file__).resolve().parent / "resources" / "onnx" / "depth"
WEIGHTS_NAME = "moge2_vits_static_336x598_t1032_q4f16.onnx"


def band_stats(bands):
    """(非 unknown 带数, 双侧带数, 远区有证带数, 远区实测带数, 远区推断带数)
    ——车头系 z0≥FAR_Z0 算远区；实测=src 含 measured，推断=仅 inferred。"""
    n_known = n_both = n_far = n_meas = n_inf = 0
    for bd in bands:
        l_ok = bd.left.x_m is not None and bd.left.src != "unknown"
        r_ok = bd.right.x_m is not None and bd.right.src != "unknown"
        if l_ok or r_ok:
            n_known += 1
        if l_ok and r_ok:
            n_both += 1
        if bd.z0 >= FAR_Z0 and (l_ok or r_ok):
            n_far += 1
            meas = ((bd.left.src == "measured") if l_ok else False) or \
                   ((bd.right.src == "measured") if r_ok else False)
            if meas:
                n_meas += 1
            else:
                n_inf += 1
    return n_known, n_both, n_far, n_meas, n_inf


def _far_edge(edge_pts, side: int, z0: float, z1: float):
    """C 远区带边界：带内有找边读数取最靠路心者（实测）；带内无则向已测边
    借最近者（推断级，FAR_CARRY_M 内），再无则 unknown。"""
    inb = [x for (s, zc, x) in edge_pts if s == side and z0 <= zc < z1]
    if inb:
        return cr.BandBoundary(min(inb, key=abs), "measured", 0.5, None)
    zc_band = 0.5 * (z0 + z1)
    cand = [(abs(zc - zc_band), x) for (s, zc, x) in edge_pts
            if s == side and zc >= FAR_Z_CAM - 3.0
            and abs(zc - zc_band) <= FAR_CARRY_M]
    if cand:
        return cr.BandBoundary(min(cand)[1], "inferred", 0.0, None)
    return cr.BandBoundary(None, "unknown")


def c_bands(bands_b, edge_pts):
    """C 走廊带：近场沿用 B（栅格投影），远场带（相机系 z0≥FAR_Z_CAM）换成
    找边读数供的边界（产线扫描已在算、近场读数只吃 ≤NEAR_Z 的那批远箱）。"""
    out = []
    for bd in bands_b:
        z0c, z1c = bd.z0 + dg.CAM_Z_DATUM_M, bd.z1 + dg.CAM_Z_DATUM_M
        if z0c < FAR_Z_CAM:
            out.append(bd)
            continue
        out.append(cr.CorridorBand(
            bd.z0, bd.z1,
            _far_edge(edge_pts, -1, z0c, z1c),
            _far_edge(edge_pts, 1, z0c, z1c),
            bd.n_pts))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--fids", default=None, help="缺省=全部 raw 帧（按序）")
    a = ap.parse_args()
    d = Path(a.dir)
    if not d.is_dir():
        raise SystemExit("数据目录不存在")
    raw_dir = d / "raw"
    names = sorted(p.name for p in raw_dir.glob("*_raw.jpg"))
    if a.fids:
        keep = {int(x) for x in a.fids.split(",")}
        names = [n for n in names if int(n[:4]) in keep]
    print(f"{len(names)} frames")

    wf = WEIGHTS_DIR / WEIGHTS_NAME
    sess = dg.load_session(wf)
    cal = load_calib()

    wt_a = WallTrack(cal)          # A：逐帧累积
    hist_b = deque(maxlen=SMOOTH_WIN)
    hist_bw = deque(maxlen=SMOOTH_WIN)

    rows = []
    prev = {"A": None, "B": None}
    for name in names:
        fid = int(name[:4])
        img = cv2.imread(str(raw_dir / name))
        frame = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pts, fx, fy = dg.infer_points(sess, frame, native=True)
        grid = dg.drivable_grid_from_points(pts, fx, fy)
        reading = dg.reading_from_points(pts, fx, cal, fy=fy, coef=grid.coef)
        center_raw = dg.grid_center_lane(grid, cal.lane_w_m)

        # A：墙融合（产线现状）
        wt_a.update(reading, fid)
        corr_a = cr.build_corridor(
            reading, grid, wt_a, fid, center_lane=center_raw, width_lane=None,
            src="probe_a", lane_w_m=cal.lane_w_m)

        # B：栅格单真源（墙边界恒空）→ 投影走廊 + 跨帧平滑
        wt_b = WallTrack(cal)      # 从不 update → 墙边界恒空
        corr_b = cr.build_corridor(
            reading, grid, wt_b, fid, center_lane=center_raw, width_lane=None,
            src="probe_b", lane_w_m=cal.lane_w_m)
        b_raw = corr_b.center_lane
        if b_raw is not None:
            hist_b.append(b_raw)
        b_smooth = (float(np.median(hist_b)) if len(hist_b) == hist_b.maxlen
                    else None)
        if corr_b.width_lane is not None:
            hist_bw.append(corr_b.width_lane)
        bw_smooth = (float(np.median(hist_bw))
                     if len(hist_bw) == hist_bw.maxlen else None)

        ka, ba, fa, ama, ain = band_stats(corr_a.bands)
        kb, bb, fb, bma, binf = band_stats(corr_b.bands)
        bands_c = c_bands(corr_b.bands, reading.edge_pts)
        fc, cmc, cinf = band_stats(bands_c)[2:]
        row = {
            "fid": fid,
            "a_center": corr_a.center_lane, "a_w": corr_a.width_lane,
            "a_known": ka, "a_both": ba, "a_far": fa,
            "a_far_meas": ama, "a_far_inf": ain,
            "b_center_raw": b_raw, "b_center": b_smooth,
            "b_w_raw": corr_b.width_lane, "b_w": bw_smooth,
            "b_known": kb, "b_both": bb, "b_far": fb,
            "b_far_meas": bma, "b_far_inf": binf,
            "c_far": fc, "c_far_meas": cmc, "c_far_inf": cinf,
        }
        for k in ("A", "B"):
            c = row["a_center"] if k == "A" else row["b_center"]
            p = prev[k]
            row[f"{k.lower()}_flip"] = bool(
                c is not None and p is not None and abs(c - p) > FLIP_LANE)
            row[f"{k.lower()}_dcenter"] = (
                abs(c - p) if c is not None and p is not None else None)
            prev[k] = c
        rows.append(row)
        print(f"{fid}: A far={fa}({ama}实) B far={fb}({bma}实) "
              f"C far={fc}({cmc}实)")

    def rate(key):
        v = [r[key] for r in rows if r[key] is not None]
        return len(v), (len(v) / len(rows) if rows else 0.0)

    def med(vals):
        v = [x for x in vals if x is not None]
        return float(np.median(v)) if v else None

    flips = {k: sum(1 for r in rows if r[f"{k.lower()}_flip"])
             for k in ("A", "B")}
    both = {k: rate(f"{k.lower()}_both")[0] for k in ("A", "B")}
    far_any = {k: sum(1 for r in rows if r[f"{k.lower()}_far"] > 0)
               for k in ("A", "B", "C")}
    far_med = {k: med([r[f"{k.lower()}_far"] for r in rows])
               for k in ("A", "B", "C")}
    far_meas_med = {k: med([r[f"{k.lower()}_far_meas"] for r in rows])
                    for k in ("A", "B", "C")}
    dc_med = {k: med([r[f"{k.lower()}_dcenter"] for r in rows])
              for k in ("A", "B")}
    n = len(rows)

    lines = [
        "# 走廊收敛影子对比（方向级初探）", "",
        f"帧数 {n}（采样子集，每 ~7 拍一帧——墙累积与滑窗都被拉疏，只看相对量）", "",
        "| 指标 | A 现状（栅格+墙融合） | B 收敛（栅格单真源） | C 收敛+找边远区 |",
        "|---|---|---|---|",
        f"| 翻面帧数（|Δcenter|>{FLIP_LANE} 道） | {flips['A']} | {flips['B']} | — |",
        f"| 帧间抖动中位（道） | {dc_med['A']} | {dc_med['B']} | — |",
        f"| 双侧边界可见帧数 | {both['A']} | {both['B']} | — |",
        f"| 远区（z≥{FAR_Z0}m）有证据帧数 | {far_any['A']} | {far_any['B']} | {far_any['C']} |",
        f"| 远区有证带数中位（/4） | {far_med['A']} | {far_med['B']} | {far_med['C']} |",
        f"| 远区实测带数中位（/4） | {far_meas_med['A']} | {far_meas_med['B']} | {far_meas_med['C']} |",
        "",
        "## 判读",
        "",
    ]
    if far_med["C"] is not None and far_med["A"] is not None \
            and far_med["C"] >= far_med["A"] * 0.9:
        lines.append(f"- C 远区覆盖追平 A（{far_med['C']} vs {far_med['A']}）——"
                     "「找边读数进栅格远行」补回了墙融合的远区价值，拆墙三件套成立。")
    elif far_med["C"] is not None and far_med["A"] is not None \
            and far_med["C"] >= far_med["A"] * 0.7:
        lines.append(f"- C 远区覆盖接近 A（{far_med['C']} vs {far_med['A']}）——"
                     "方向成立但仍有缺口，看逐帧 JSONL 定位缺在哪个带。")
    else:
        lines.append(f"- C 远区覆盖仍明显低于 A（{far_med['C']} vs {far_med['A']}）——"
                     "找边远箱供数不够，墙融合的跨帧累积仍有真实价值。")
    lines.append("- 已知偏差：采样子集、无 YOLO 物体掩码、B 滑窗 3 帧≈5s（"
                 "比产线控制周期慢，平滑收益被低估）。")

    summary = d / "corridor_converge_summary.md"
    summary.write_text("\n".join(lines), encoding="utf-8")
    frames_jsonl = d / "corridor_converge_frames.jsonl"
    frames_jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8")
    print("\n".join(lines[5:]))
    print(f"written {summary}")


if __name__ == "__main__":
    main()
