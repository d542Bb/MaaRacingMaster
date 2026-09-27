# -*- coding: utf-8 -*-
"""181518 局金标量测准备（C 线：先量测、再校准守卫）。

背景：2026-09-27 181518 实机局仍撞墙。trace 取证指认消费层守卫（HW_MIN 等）
与深度链在本路型的结构边宽语义冲突——配对半宽 p50=1.15 道被 HW_MIN=1.5 拒收，
只有垃圾宽配对活下来喂坏 hw EMA。用户拍板走 C：对本局 depth_debug 证据包做
金标标注，量出真边界（车道单位）后再提守卫数值，不许拍脑袋改数。

本脚本两段（一次跑完）：
1. prep：debug 三行堆叠 jpg → 裁顶行驾驶帧 + 纯色覆盖层（读数黄线/掩码描边/
   白字）inpaint 清洗 → 标注帧 + gold_frames.csv（gold_annotate.py 可直接消费）。
   清洗不追求完美（描边残晕无害），只求不把程序读数亮给标注者（防锚定）。
2. replay：band.npy+mask.npy 逐包喂生产 reading_from_map（与实机同一代码路径，
   尺度自标定在函数内自跑），dump 聚合读数 + 逐行明细（v / u_l / u_r / occ /
   zmed）→ replay.json，供标注完成后金标对比。

已知折扣：pack 里只有 ego|object 合并掩码（_write_debug 存的是 merged），重放
把 merged 当 object_mask（侧遮挡判据来源）——ego 静态掩码 |X|<1.5m 不触发 occ，
对读数无实际影响；逐行明细里 occ 语义按此读。

用法（仓库根 .venv）：
  python tools/experiments/speedrush_vision/probe_gold_181518.py
输出 = %APPDATA%/MaaRacingMaster/data/speedrush/depth_review/gold181518/
标注：python tools/experiments/speedrush_vision/gold_annotate.py \
        --list <输出目录>/gold_frames.csv
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DIAG_Y1, Y0, _cloud_band, _plane_fit_band, _row_scan, _dig_band,
    DepthRoadObserver, reading_from_map)
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    Calib, load_calib, x_lane_of)

APP = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
SRC = APP / "control_traces" / "depth_debug_20260927_181518"
OUT = APP / "depth_review" / "gold181518"

# render_depth_debug 写进顶行的纯色（BGR 口径）：读数线黄 / ego 描边浅蓝 /
# 物体描边蓝（两描边都在 BGR 图上以 (255,128,0)/(255,0,0) 画出）
OVERLAY_BGR = ((0, 255, 255), (255, 128, 0), (255, 0, 0))
TEXT_ROWS = 34


def clean_top(frame_bgr: np.ndarray) -> np.ndarray:
    """裁顶行 720 并 inpaint 掉程序覆盖层（防标注者被程序读数锚定）。"""
    top = frame_bgr[:720].copy()
    dist = np.full(top.shape[:2], 255.0, np.float32)
    for c in OVERLAY_BGR:
        d = np.linalg.norm(top.astype(np.float32) - np.float32(c), axis=2)
        dist = np.minimum(dist, d)
    m = (dist < 60).astype(np.uint8) * 255
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=2)
    top = cv2.inpaint(top, m, 4, cv2.INPAINT_TELEA)
    top[:TEXT_ROWS] = 0
    return top


def prep() -> list[Path]:
    frames = OUT / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    rows = []
    for jpg in sorted(SRC.glob("d*.jpg")):  # 渲染堆叠图（_band/_mask 是 npy 不入）
        top = clean_top(cv2.imread(str(jpg)))
        dst = frames / (jpg.stem + ".png")
        cv2.imwrite(str(dst), top)
        rows.append((str(dst), "181518"))
    with (OUT / "gold_frames.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "stratum"])
        w.writerows(rows)
    print(f"[prep] {len(rows)} 帧 → {frames}")
    return [Path(p) for p, _ in rows]


def replay() -> None:
    cal = load_calib()
    # 老证据包没有 _ego.npy（导出后加的）；ego 静态掩码是确定性资产，直载等价
    ego = DepthRoadObserver._load_ego_mask()
    detail = {}
    for stem_path in sorted(SRC.glob("d*_band.npy")):
        stem = stem_path.stem[: -len("_band")]
        band = np.load(stem_path).astype(np.float32)
        m = np.full((720, 1280), np.inf, np.float32)
        m[Y0:DIAG_Y1] = band
        merged = np.unpackbits(np.load(SRC / f"{stem}_mask.npy"))\
            .astype(bool).reshape(720, 1280)
        r = reading_from_map(m, cal, ego_mask=ego, object_mask=merged)
        # 逐行明细（第二遍=生效标定系的重扫，与 reading_from_map 内部同参）
        X, Y, Z = _cloud_band(m, 24.06)
        coef, ground, above = _plane_fit_band(X, Y, Z, _dig_band(merged, 1280))
        rows = []
        if coef is not None:
            for rr in _row_scan(X, Z, ground, above, _dig_band(merged, 1280),
                                _dig_band(merged, 1280), coef):
                rows.append({
                    "v": rr.v,
                    "lane_l": x_lane_of(int(round(rr.u_l)), rr.v, cal),
                    "lane_r": x_lane_of(int(round(rr.u_r)), rr.v, cal),
                    "occ_l": rr.occ_l, "occ_r": rr.occ_r,
                    "zmed": round(rr.zmed, 2)})
        detail[stem] = {
            "reading": {"L": r.left_edge_lane, "R": r.right_edge_lane,
                        "rejects": list(r.rejects)},
            "rows": rows}
    (OUT / "replay.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    n2 = sum(1 for d in detail.values() if d["reading"]["L"] is not None
             and d["reading"]["R"] is not None)
    print(f"[replay] {len(detail)} 包，双侧在场 {n2} → {OUT / 'replay.json'}")


def score() -> None:
    """金标线 → lane 单位（A1 尺 x_lane_of，与深度链同一把尺），对比重放明细。

    对比口径：金标线段在行 v 处的 u → x_lane_of(u, v)，与 replay.json 逐行明细
    里最近 v 的行读数配对；聚合口径另列 production 聚合读数（分位+侧别门之后）。
    汇总：每侧 bias/MAD、金标配对半宽与 off 分布（守卫重校准的量测依据）。"""
    lab = OUT / "gold_labels.csv"
    if not lab.exists():
        print("[score] 无 gold_labels.csv——先跑 gold_annotate 标注")
        return
    rep = json.loads((OUT / "replay.json").read_text(encoding="utf-8"))
    cal = load_calib()
    rows = [r for r in csv.DictReader(lab.open(encoding="utf-8"))
            if r.get("l_nx") not in (None, "")]
    VS = (520, 560, 600, 640, 680)
    per = {"L": [], "R": []}          # (帧, v, 金标lane, 重放lane, 聚合lane)
    ghw, goff = [], []
    for r in rows:
        stem = "d" + Path(r["path"]).stem[1:]
        det = rep.get(stem)
        if det is None:
            continue
        gl = {}
        for side in ("l", "r"):
            if r[side + "cls"] == "skip":
                continue
            p1 = np.array([float(r[side + "_nx"]), float(r[side + "_ny"])])
            p2 = np.array([float(r[side + "_fx"]), float(r[side + "_fy"])])
            vals = {}
            for v in VS:
                t = (v - p1[1]) / (p2[1] - p1[1])
                if not (0.0 <= t <= 1.0):
                    continue
                u = p1[0] + t * (p2[0] - p1[0])
                vals[v] = x_lane_of(float(u), v, cal)
            gl[side.upper()] = vals
        if "L" in gl and "R" in gl:
            common = sorted(set(gl["L"]) & set(gl["R"]))
            if common:
                v = common[len(common) // 2]
                ghw.append(gl["R"][v] - gl["L"][v])
                goff.append(-(gl["L"][v] + gl["R"][v]) / 2)
        rr = det["rows"]
        for side in ("L", "l", "R", "r"):
            S = side.upper()
            if S not in gl or not gl[S]:
                continue
            agg = det["reading"][S]
            for v, glane in gl[S].items():
                cands = [d for d in rr
                         if not (d["occ_l"] if S == "L" else d["occ_r"])]
                near = min(cands, key=lambda d: abs(d["v"] - v)) if cands else None
                plan = None
                if near is not None:
                    plan = near["lane_l" if S == "L" else "lane_r"]
                per[S].append((stem, v, glane, plan, agg))
    out = []
    for S in ("L", "R"):
        ds = [(g - p) for _, _, g, p, _ in per[S] if p is not None]
        aggs = {(st) for st, _, _, _, a in per[S] if a is not None}
        if ds:
            out.append(f"{S} 逐行 n={len(ds)} bias={np.mean(ds):+.2f} "
                       f"MAD={np.median(np.abs(ds - np.median(ds))):.2f} "
                       f"p90={np.percentile(np.abs(ds), 90):.2f}"
                       f"（聚合在场帧 {len(aggs)}）")
    q = lambda a, p: float(np.percentile(a, p))
    if ghw:
        out.append(f"金标配对半宽 n={len(ghw)} p10/50/90 = "
                   f"{q(ghw,10):.2f}/{q(ghw,50):.2f}/{q(ghw,90):.2f}")
        out.append(f"金标 off n={len(goff)} p10/50/90 = "
                   f"{q(goff,10):+.2f}/{q(goff,50):+.2f}/{q(goff,90):+.2f}")
    print("\n".join(out))
    (OUT / "score.json").write_text(json.dumps(
        {"per": {k: v for k, v in per.items()},
         "gold_hw": ghw, "gold_off": goff}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    if "--score" in sys.argv:
        score()
    else:
        prep()
        replay()
