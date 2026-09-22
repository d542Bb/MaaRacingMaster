# -*- coding: utf-8 -*-
"""只读探针：旧 racing 的"角度+半屏"边线判据，能否替 speedrush v2 剔掉 R2 的误配？

问的问题（C 类，外部系统跨画面表现，必须实测不得靠猜）：
  在 v2 已证实配错对（车道线/墙基被当成路缘）的坏帧上，被误选中的那条"假路缘"，
  是否在下列特征上与真路缘可分——
    A) 行覆盖率：真路缘=黄实线，沿扫描带行行连续；白虚线/遮挡碎片=有断口（覆盖率低）。
    B) 消失点像素距离：地面直线（路缘、车道线）投影必过消失点(vpx,y_h)；
       墙基立面线（垂直结构）一般不过——若可分，就是 v2 缺失的语义判据。
    C) x_lane 位置：v2 现在唯一在用的判据（宽度窗），看它到底分不分得开。

纪律：
  - 纯只读，不改生产代码、不 import 进生产；复刻 detect_boundary 的管线但留住每簇内部。
  - 帧源：AppData/Roaming/MaaRacingMaster/data/speedrush/control_traces/badframes_*/*.jpg（72 张真帧）。
  - 弱标签来自帧级 |road_offset|：>2.4=物理不可能=确信误配(BAD)，(1.2,2.4]=可疑(MID)，≤1.2=暂信(OK)，无双侧=NOEDGE。
    真值仍需对最差帧目视核对（探针顺带 dump overlay PNG）。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# 生产真源常量/几何件全部 import，不抄第二份（禁止建立第二份真相）
from maaracing_master.core.paths import data_dir
from maaracing_master.plugins.speedrush import boundary as B
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of

CAL = load_calib()


def detect_with_internals(frame_rgb: np.ndarray) -> dict | None:
    """复刻 boundary.detect_boundary 的管线，但返回**每个被接受簇的内部量**，
    供探针测特征。配对判据与生产逐行一致（同 gap/同窗/同吸收）。"""
    h, w = frame_rgb.shape[:2]
    y0 = max(int(CAL.y_h) + B.BAND_TOP_OFF, 0)
    y1 = min(int(CAL.y_h) + B.BAND_BOT_OFF, h)
    band = frame_rgb[y0:y1]
    hsv = cv2.cvtColor(band, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, B._HSV_LOW, B._HSV_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, B.KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, B.KERNEL)

    votes: list[tuple[float, float, int]] = []
    for y in range(y0, y1, B.BAND_STEP):
        for c in B._row_run_centers(mask[y - y0]):
            votes.append((x_lane_of(int(c), y, CAL), c, y))
    cl = B._cluster(votes)
    meds = [float(np.median([p[0] for p in c])) for c in cl]
    pair = None
    for i in range(len(cl)):
        for j in range(len(cl)):
            sep = meds[j] - meds[i]
            if B.ROAD_PAIR_MIN_LANE <= sep <= B.ROAD_PAIR_MAX_LANE:
                sc = len(cl[i]) + len(cl[j])
                if pair is None or sc > pair[0]:
                    pair = (sc, i, j)
    if pair is None:
        return None
    _, i, j = pair
    rest = [c for k, c in enumerate(cl) if k not in (i, j)]
    L = B._absorb(cl[i], rest, CAL.y_h)
    R = B._absorb(cl[j], rest, CAL.y_h)
    lf = _edge_feats(L, hsv, y0)
    rf = _edge_feats(R, hsv, y0)
    # 逐帧：两条缘拟合线的交点（= 隐含消失点）。地面线交点必落在地平线行 y_h 上，
    # 与车头航向无关（转向只沿地平线挪交点 x，不挪 y）——这是转弯不变的质量判据。
    da = lf["a"] - rf["a"]
    if abs(da) > 1e-6:
        y_int = (rf["b"] - lf["b"]) / da
        vp_y_off = y_int - CAL.y_h          # 交点离地平线多远（px）；杂线配对→爆表
        vp_x_int = lf["a"] * y_int + lf["b"]
    else:
        vp_y_off = float("nan")
        vp_x_int = float("nan")
    return {"L": lf, "R": rf,
            "offset": -(meds[i] + meds[j]) / 2.0,
            "width": meds[j] - meds[i],
            "vp_y_off": None if vp_y_off != vp_y_off else round(vp_y_off, 1),
            "vp_x_int": None if vp_x_int != vp_x_int else round(vp_x_int, 0)}


def _edge_feats(pts: list[tuple[float, float, int]],
                hsv_band: np.ndarray, y0: int) -> dict:
    """一条边（一组 (x_lane,x_px,y) 点）的候选特征，含像素饱和度/色相（验白线漏检）。"""
    u = np.array([p[0] for p in pts], dtype=float)          # x_lane
    ys = np.array([p[2] for p in pts], dtype=float)          # 行
    xs = np.array([p[1] for p in pts], dtype=float)          # px 列
    # 饱和度/色相：直接采每个 run 中心像素的 HSV（白线=低 S、黄昏染黄=中 S、真黄缘=高 S）
    hh = int(hsv_band.shape[0])
    ww = int(hsv_band.shape[1])
    sat, hue = [], []
    for (x_lane, x_px, y) in pts:
        yy = int(y) - y0
        xx = int(round(x_px))
        if 0 <= yy < hh and 0 <= xx < ww:
            sat.append(float(hsv_band[yy, xx, 1]))
            hue.append(float(hsv_band[yy, xx, 0]))
    mean_s = round(float(np.mean(sat)), 1) if sat else float("nan")
    mean_h = round(float(np.mean(hue)), 1) if hue else float("nan")
    # A) 行覆盖：扫描带总行数 vs 该边占据的不同行数（虚线/遮挡→覆盖低）
    rows_span = len(np.unique(ys.astype(int)))
    total_rows = (B.BAND_BOT_OFF - B.BAND_TOP_OFF) // B.BAND_STEP + 1
    coverage = rows_span / total_rows
    # 相邻采样行的最大缺口（连续实线≈1 步；有断口→大）
    uys = np.unique(ys.astype(int))
    max_gap = int(np.max(np.diff(uys))) if uys.size >= 2 else 0
    # B) 消失点距离：拟合线 x=a·y+b（polyfit），算 (vpx,y_h) 到线的垂直像素距离
    if len(pts) >= 4:
        a, b = np.polyfit(ys, xs, 1)
        d = abs(CAL.vpx - a * CAL.y_h - b) / math.sqrt(1.0 + a * a)
    else:
        a, b, d = 0.0, float(xs.mean()), float("nan")
    # 角度（度，从水平）：线方向 (dx,dy)=(a,1)
    ang = math.degrees(math.atan2(1.0, a)) if abs(a) < 1e9 else 90.0
    return {"med": float(np.median(u)), "std": float(np.std(u)),
            "coverage": round(coverage, 3), "max_gap_rows": max_gap,
            "vp_dist_px": round(d, 1), "angle_deg": round(ang, 1),
            "mean_s": mean_s, "mean_h": mean_h,
            "n": len(pts), "a": float(a), "b": float(b)}


def load_frames() -> list[Path]:
    root = data_dir() / "speedrush" / "control_traces"
    return sorted(root.glob("badframes_*/*.jpg"))


def main() -> None:
    frames = load_frames()
    out_rows: list[dict] = []
    worst: list[tuple[float, Path, dict]] = []
    t0 = time.perf_counter()
    for k, fp in enumerate(frames, 1):
        img = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
        r = detect_with_internals(img)
        if r is None:
            tag, off = "NOEDGE", None
        else:
            off = r["offset"]
            ao = abs(off)
            tag = "BAD" if ao > 2.4 else ("MID" if ao > 1.2 else "OK")
            worst.append((ao, fp, r))
        out_rows.append({"file": fp.name, "dir": fp.parent.name,
                         "tag": tag, "offset": None if off is None else round(off, 3),
                         "result": r})
        if k % 20 == 0:
            print(f"  ...{k}/{len(frames)} ({time.perf_counter()-t0:.1f}s)")
    print(f"复跑完成 {len(frames)} 帧 / {time.perf_counter()-t0:.1f}s\n")

    # 分层统计三特征（把 L/R 两缘各自并入所在帧的 tag 桶）
    import statistics as st
    buckets = {"BAD": [], "MID": [], "OK": []}
    for row in out_rows:
        if row["result"]:
            for side in ("L", "R"):
                buckets[row["tag"]].append(row["result"][side])
    print("特征分布（中位 / 均值 / 样本数）——按帧级 offset 分层")
    print(f"{'tag':5} {'n边':>4}  coverage  max_gap  vp_dist_px  |x_lane|  std")
    for tag in ("OK", "MID", "BAD"):
        e = buckets[tag]
        if not e:
            print(f"{tag:5} {0:>4}  (无)")
            continue
        def col(key, absv=False):
            vals = [abs(x[key]) if absv else x[key] for x in e]
            vals = [v for v in vals if v == v]
            return f"{st.median(vals):.2f}/{st.mean(vals):.2f}" if vals else "-"
        print(f"{tag:5} {len(e):>4}  "
              f"{col('coverage'):12} {col('max_gap_rows'):8} "
              f"{col('vp_dist_px'):12} {col('med', True):10} {col('std'):8}")

    # 帧级：隐含消失点离地平线的距离（转弯不变的地面线质量判据）——测"VP 这套稳不稳"
    print("\n帧级 |隐含VP − 地平线y_h|（px，转弯不变）——按 offset 分层：")
    print(f"{'tag':5} {'帧数':>4}  {'中位':>8} {'P90':>8} {'最大':>8}")
    for tag in ("OK", "MID", "BAD"):
        vals = [abs(r["result"]["vp_y_off"]) for r in out_rows
                if r["tag"] == tag and r["result"] and r["result"]["vp_y_off"] is not None]
        if not vals:
            print(f"{tag:5} {0:>4}")
            continue
        vs = sorted(vals)
        p90 = vs[min(len(vs) - 1, int(round(0.9 * (len(vs) - 1))))]
        print(f"{tag:5} {len(vs):>4}  {st.median(vs):>8.0f} {p90:>8.0f} {max(vs):>8.0f}")

    # 白线漏检验证：被选中缘的像素饱和度（低 S=白虚线漏进黄掩码）
    print("\n被选中缘的像素饱和度 mean_S（<~90 疑白线漏检）——按 offset 分层：")
    print(f"{'tag':5} {'n边':>4}  {'mean_S 中位':>12} {'<90占比':>8} {'mean_H 中位':>12}")
    for tag in ("OK", "MID", "BAD"):
        e = buckets[tag]
        ss = [x["mean_s"] for x in e if x["mean_s"] == x["mean_s"]]
        hh = [x["mean_h"] for x in e if x["mean_h"] == x["mean_h"]]
        if not ss:
            print(f"{tag:5} {0:>4}")
            continue
        low = sum(1 for v in ss if v < 90) / len(ss)
        print(f"{tag:5} {len(ss):>4}  {st.median(ss):>12.0f} {low*100:>7.0f}% "
              f"{st.median(hh):>12.1f}")

    # dump 最差的 6 帧逐缘向量 + overlay 供目视
    worst.sort(key=lambda x: -x[0])
    print("\n最差 8 帧（offset 最大=最确信误配）逐缘特征：")
    print(f"{'offset':>7} {'side':4} {'med':>7} {'cov':>5} {'gap':>3} {'vpx':>7} {'ang':>5} {'S':>5} {'n':>3}")
    out_dir = Path(__file__).parent
    for ao, fp, r in worst[:8]:
        for side in ("L", "R"):
            e = r[side]
            print(f"{r['offset']:>7.2f} {side:4} {e['med']:>7.2f} {e['coverage']:>5.2f} "
                  f"{e['max_gap_rows']:>3} {e['vp_dist_px']:>7.1f} {e['angle_deg']:>5.1f} "
                  f"{e['mean_s']:>5.0f} {e['n']:>3}")
    # 存 overlay：把 L/R 拟合线画回帧（蓝=左缘、红=右缘、黄字=该簇 |x_lane| 读数），
    # 绿点=标定消失点。人眼据此判断"被选中的到底是墙、白虚线还是金币群"。
    for idx, (ao, fp, r) in enumerate(worst[:6]):
        img = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
        y0 = int(CAL.y_h) + B.BAND_TOP_OFF
        y1 = int(CAL.y_h) + B.BAND_BOT_OFF
        for side, col in (("L", (255, 0, 0)), ("R", (0, 0, 255))):
            e = r[side]
            a, b = e["a"], e["b"]
            for yy, cc in ((y0, col), (y1, col)):
                xv = int(a * yy + b)
                cv2.circle(img, (xv, yy), 3, cc, -1)
            xm = int(a * ((y0 + y1) // 2) + b)
            cv2.putText(img, f"{side}={e['med']:.1f}", (xm - 20, (y0 + y1) // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2)
        cv2.circle(img, (int(CAL.vpx), int(CAL.y_h)), 6, (0, 255, 0), -1)
        cv2.putText(img, f"{fp.name} off={r['offset']:.2f}", (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.imwrite(str(out_dir / f"probe_worst_{idx}_{fp.name}"),
                    cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    print(f"\noverlay 落 {out_dir}/probe_worst_*.png（蓝=左缘线 红=右缘线 绿点=消失点）")
    with (out_dir / "probe_edge_features.jsonl").open("w", encoding="utf-8") as f:
        for row in out_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"逐帧特征 → {out_dir}/probe_edge_features.jsonl")


if __name__ == "__main__":
    sys.exit(main())
