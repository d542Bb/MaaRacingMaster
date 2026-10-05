# -*- coding: utf-8 -*-
"""黑视金标计分：产线找边读数 vs 人工金标（黑视帧集，第二刀 S2-B 前的验值底座）。

考卷 = blackout_gold/gold_labels.csv（14 行 / 10 唯一帧，人工在半幅 _frame.jpg
上点两下成线的碰撞边界金标）。考生 = 产线同源复算链（evid→assemble→
reading_from_points，ego/object 掩码同喂）给出的单侧缘读数 edge_lane。

口径（全部产线同源，不自造折算）：
- 金标半幅像素 → ×2 折全幅 → 路面平面反投影：u=(x−W/2)/fx, v=(y−H/2)/fy，
  Z = c/(v−a·u−b)，lane = X/lane_w_m。平面与 lane_w_m 都来自同一帧复算的
  reading.coef 与 gate0.json——与产线消费的 edge_lane 同一坐标系（车道量，
  原点=车，左负右正）。
- 查询带 y_half ∈ {280,300,320}（全幅 560/600/640，同 gold_score 的近带，
  半幅折算：这批金标底图都是老包半幅 640×360）。
- 误差 err = 候选缘 − 金标缘（车道量）。**正 = 候选把边界报得更靠外** =
  余量高估 = 危险方向（车会以为还有空间而撞上真实边界）：
  左缘（负值域）err<−TOL 危险，右缘 err>+TOL 危险。
- 平面拟合失败的帧，金标无法折算车道 → 如实弃权，不进统计（写进 CSV）。

用法：
  .venv/Scripts/python.exe tools/experiments/speedrush_vision/gold_blackout_score.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
import select_blackout_frames as sbf  # noqa: E402  （复用 assemble/_load_ego/OUT）

FULL_W, FULL_H = sbf.FULL_W, sbf.FULL_H
YS_HALF = (280, 300, 320)   # 金标半幅查询带（= 全幅 560/600/640 近带）
DANGER_TOL = 0.25           # 危险方向错误的最小车道量（≈0.84m，高于标注噪声底）
LONG_TAILS = (0.5, 1.0)     # 长尾档（车道量）——四硬条件协议的验值口径


def gold_x_at(r: dict, side: str, y_half: float) -> float | None:
    """金标线在 y_half（半幅像素）处的 x。同 gold_score 的像素插值口径，
    坐标按半幅（这批金标底图均为 640×360）。"""
    if r[side + "cls"] == "skip":
        return None
    nx, ny = float(r[side + "_nx"]), float(r[side + "_ny"])
    fx, fy = float(r[side + "_fx"]), float(r[side + "_fy"])
    if abs(ny - fy) < 1:
        return None
    return nx + (y_half - ny) * (fx - nx) / (fy - ny)


def lane_to_px(lane: float, coef, fx: float, fy: float, yh: int,
               lane_w_m: float) -> tuple[float, float]:
    """车道量 → 半幅像素（逆向折算的正向逆映射，同一平面）。y 取查询带行。"""
    a, b, c = coef
    x_m = lane * lane_w_m
    v = (yh * 2 - FULL_H / 2.0) / fy
    z = (c + a * x_m) / (v - b)
    return (fx * x_m / z + FULL_W / 2.0) / 2.0, yh


def edgept_to_px(s: int, z: float, x: float, coef, fx: float,
                 fy: float) -> tuple[float, float]:
    """edge_pts 检出 (side, z_m, x_m) → 半幅像素（地面点经平面定 y）。"""
    a, b, c = coef
    y_full = fy * (a * x + b * z + c) / z + FULL_H / 2.0
    x_full = fx * x / z + FULL_W / 2.0
    return x_full / 2.0, y_full / 2.0


def score_sheet(frame_ctx: dict, cal, out_path: Path) -> None:
    """计分可视化拼版：每帧底图上叠金标线/金标采样点/候选投影点/全部检出，
    人工目检确认 err 数字是真实几何差而非折算伪影。"""
    tiles = []
    for name, ctx in sorted(frame_ctx.items()):
        r, rd = ctx["r"], ctx["rd"]
        img = cv2.imread(name)
        if img is None:
            continue
        # 金标线（n→f 两点）与查询带采样点：L 绿 / R 橙
        for side, col in (("l", (0, 255, 0)), ("r", (0, 165, 255))):
            if r[side + "cls"] == "skip":
                continue
            p1 = (int(float(r[side + "_nx"])), int(float(r[side + "_ny"])))
            p2 = (int(float(r[side + "_fx"])), int(float(r[side + "_fy"])))
            cv2.line(img, p1, p2, col, 1)
            for yh in YS_HALF:
                gx = gold_x_at(r, side, yh)
                if gx is not None:
                    cv2.circle(img, (int(gx), yh), 4, col, 1)
        # 候选读数投影（红叉=R，蓝叉=L）与全部检出序列（L 青 / R 黄小点）
        if rd.coef is not None:
            for side, col in ((1, (0, 0, 255)), (-1, (255, 0, 0))):
                lane = rd.right_edge_lane if side == 1 else rd.left_edge_lane
                if lane is not None:
                    for yh in YS_HALF:
                        px, py = lane_to_px(lane, rd.coef, ctx["fx"], ctx["fy"],
                                            yh, cal.lane_w_m)
                        cv2.drawMarker(img, (int(px), int(py)), col,
                                       cv2.MARKER_TILTED_CROSS, 9, 2)
                for s, z, x in rd.edge_pts:
                    if s != side:
                        continue
                    px, py = edgept_to_px(s, z, x, rd.coef, ctx["fx"],
                                          ctx["fy"])
                    cv2.circle(img, (int(px), int(py)), 2, (255, 255, 0)
                               if s == 1 else (255, 255, 255), -1)
        errs = [float(x["err"]) for x in ctx["rows"] if x["err"] != ""]
        cap = (f"{Path(name).parent.name[12:]} {Path(name).name[:-10]} "
               f"{r['stratum']} sides={rd.sides}")
        if errs:
            cap += f" err={np.median(errs):+.2f}道"
        cv2.rectangle(img, (0, 0), (img.shape[1] - 1, 20), (0, 0, 0), -1)
        cv2.putText(img, cap, (3, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                    (255, 255, 255), 1)
        tiles.append(img)
    cols, th = 2, 360 + 0
    rows_n = (len(tiles) + cols - 1) // cols
    canvas = np.full((rows_n * 360, cols * 640, 3), 30, np.uint8)
    for i, t in enumerate(tiles):
        rr, cc = divmod(i, cols)
        canvas[rr * 360:rr * 360 + t.shape[0],
               cc * 640:cc * 640 + t.shape[1]] = t
    ok, buf = cv2.imencode(".png", canvas)
    if ok:
        out_path.write_bytes(buf.tobytes())  # 非 ASCII 路径禁 imwrite
        print(f"计分拼版 → {out_path}")


def gold_lanes_at(r: dict, side: str, coef, fx: float, fy: float,
                  lane_w_m: float) -> dict[int, float | None]:
    """金标线 → 查询带各行的车道量（平面反投影，与产线 edge_lane 同坐标系）。
    coef None / 反投影异常的行值为 None。"""
    if coef is None or r[side + "cls"] == "skip":
        return {yh: None for yh in YS_HALF}
    a, b, c = coef
    out: dict[int, float | None] = {}
    for yh in YS_HALF:
        gx = gold_x_at(r, side, yh)
        if gx is None:
            out[yh] = None
            continue
        u = (gx * 2.0 - FULL_W / 2.0) / fx
        v = (yh * 2.0 - FULL_H / 2.0) / fy
        den = v - a * u - b
        z = c / den if den != 0 else float("nan")
        out[yh] = (u * z) / lane_w_m if (np.isfinite(z) and z > 0.5) else None
    return out


def main() -> None:
    cal = load_calib()
    labels = list(csv.DictReader((sbf.OUT / "gold_labels.csv")
                                 .open(encoding="utf-8")))
    rows: list[dict] = []
    frame_ctx: dict[str, dict] = {}   # 逐帧上下文（画计分拼版用）
    for r in labels:
        p = Path(r["path"])
        stem = p.with_name(p.name[:-len("_frame.jpg")])
        pts, fx, fy, ego, obj = sbf.assemble(stem)
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego,
                                    object_mask=obj, fy=fy)
        frame_ctx.setdefault(str(p), {"r": r, "rd": rd, "fx": fx, "fy": fy,
                                      "rows": []})
        n0 = len(rows)
        for side in ("l", "r"):
            cand = rd.left_edge_lane if side == "l" else rd.right_edge_lane
            g_lanes = gold_lanes_at(r, side, rd.coef, fx, fy, cal.lane_w_m)
            for yh in YS_HALF:
                gx_half = gold_x_at(r, side, yh)
                if gx_half is None:
                    continue
                row = {"path": p.name, "stratum": r["stratum"], "side": side,
                       "y_half": yh, "cand_lane": "" if cand is None
                       else round(cand, 3), "gold_lane": "", "err": "",
                       "danger": "", "note": ""}
                gold_lane = g_lanes[yh]
                if gold_lane is None:
                    row["note"] = ("平面拟合失败,金标不可折算"
                                   if rd.coef is None else "反投影异常")
                    rows.append(row)
                    continue
                gold_lane = float(gold_lane)
                row["gold_lane"] = round(gold_lane, 3)
                if cand is None:
                    row["note"] = "候选弃权(该侧无读数)"
                    rows.append(row)
                    continue
                err = cand - gold_lane
                danger = (err < -DANGER_TOL) if side == "l" \
                    else (err > DANGER_TOL)
                row["err"] = round(err, 3)
                row["danger"] = int(danger)
                if abs(gold_lane) > 10 or abs(err) > 10:
                    row["note"] = "量级异常,人工复核"
                rows.append(row)
        frame_ctx[str(p)]["rows"].extend(rows[n0:])

        print(f"{p.parent.name}/{p.name} sides={rd.sides} "
              f"L={rd.left_edge_lane} R={rd.right_edge_lane} "
              f"rej={rd.rejects}")

    out = sbf.OUT / "gold_blackout_score.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # ── 汇总：分 stratum × side，只统计有 err 的可比行 ────────────────
    comp = [r for r in rows if r["err"] != ""]
    print(f"\n== 黑视金标计分（{len(comp)}/{len(rows)} 行可比 → {out.name}）==")
    print(f"危险方向 = 候选把边界报得更靠外（余量高估）；TOL={DANGER_TOL} 道")
    strata = sorted({r["stratum"] for r in comp})
    for st in strata:
        for side in ("l", "r"):
            v = np.array([r["err"] for r in comp
                          if r["stratum"] == st and r["side"] == side])
            d = [r["danger"] for r in comp
                 if r["stratum"] == st and r["side"] == side]
            if len(v) == 0:
                continue
            tails = "/".join(f"{np.mean(np.abs(v) > t):.0%}"
                             for t in LONG_TAILS)
            print(f"{st:10s} {side.upper()} n={len(v):3d} "
                  f"err中位={np.median(v):+.2f} p90|err|={np.percentile(np.abs(v), 90):.2f} "
                  f"危险向={np.mean(d):.0%} 长尾>.5/1道={tails}")
    abst = [r for r in rows if r["err"] == ""]
    if abst:
        print(f"弃权/异常 {len(abst)} 行："
              f"{sorted({r['note'] for r in abst})}")
    # 唯一帧口径（行级会被多段共帧重复计权）：长尾帧占比才是四硬条件的验值面
    fr_err: dict[str, list[float]] = {}
    for r in comp:
        fr_err.setdefault(r["path"], []).append(float(r["err"]))
    tail_fr = {f for f, v in fr_err.items() if np.median(np.abs(v)) > 0.5}
    dang_fr = {f for f, v in fr_err.items()
               if any((float(e) < -DANGER_TOL if rr["side"] == "l"
                       else float(e) > DANGER_TOL)
                      for rr, e in zip((r for r in comp if r["path"] == f),
                                       v))}
    print(f"唯一帧口径：可比 {len(fr_err)} 帧，长尾(中位|err|>.5道) "
          f"{len(tail_fr)} 帧 {sorted(tail_fr)}，含危险向 {len(dang_fr)} 帧")
    score_sheet(frame_ctx, cal, sbf.OUT / "review_score_sheet.png")


if __name__ == "__main__":
    main()
