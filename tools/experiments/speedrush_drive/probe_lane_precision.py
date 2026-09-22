"""近场横向精度实测（设计稿 v2 §七.4 / §三 复议条件的实测量）。

**要回答的问题**（C 类）：A1 尺子在**近场**（贴掠发生距离）的读数误差带是多少——
0.05 车道级横向可控性是否可达。这是 coin-only 基线复议（§三"贴掠"启用与否）的
唯一实证前提：远处误差带 ±0.2–0.5 车道已有账（§6 误差带随分母收缩），近场缺数。

**为什么不做手工标注车道线**（设计稿原口径的替代）：金币排布是游戏路网格上的
**完美等距阵列**（§七.2 实测结题），即每列金币天然锚定一条车道线——把同列金币的
x_lane 读数做分带聚类，簇内散布 = 尺子几何误差 + 检测框抖动的合成误差带，
簇心间隔 = a_x 标定的免费交叉核对（真车道宽应恒 1.0）。自校准、可复现、无标注者偏差。

**两路读数**：
① 跨帧分带聚类：按分母 (cy−y_h) 分带，带内全部金币读数的稳健散布（MAD→σ）。
   含标定系统偏与曲率共模——直道局（p1）为主读数；
② 帧内共模消除差：同帧同列（x_lane 聚类）且行距 ≤60px 的金币对，读数差 Δx_lane。
   曲率/标定对同帧相邻金币近似共模，差分消掉——剩的是单帧可复现噪声，
   贴近控制器真正能吃到的精度（Δ/√2 = 单读数噪声）。

**输入**：demos 录制场（frames.jsonl + frames/）。感知与实机同路
（StreetPerception，插件自带模型，conf 0.35）。
**输出**（数据目录，不入库）：`lane_scan.jsonl`（逐帧金币/车框几何）+ stdout 读数表。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_lane_precision.py scan  --only 20260922
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_lane_precision.py analyze [--split stage]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    load_calib, x_lane_of)

_DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = _DATA / "demos"
SCAN = _DATA / "lane_scan.jsonl"
MODEL = (ROOT / "maaracing_master" / "plugins" / "speedrush"
         / "resources" / "onnx" / "model.onnx")

# 分母带（px）：MIN_DENOM=20 起，至自车行 v_ego−y_h≈392。近场=260+（屏幕下半部）。
BANDS = [(20, 60), (60, 120), (120, 200), (200, 300), (300, 420)]
CLUSTER_GAP = 0.25    # 1D 聚类切缝：车道宽 1.0 的四分之一，簇内散布不会到这个量级
PAIR_MAX_CY = 60.0    # 帧内列对的最大行距（同列相邻金币）


def cmd_scan(only: str) -> None:
    sessions = sorted(d for d in DEMOS.iterdir() if (d / "frames.jsonl").is_file())
    if only:
        sessions = [s for s in sessions if only in s.name]
    print(f"场次 {len(sessions)}: {[s.name for s in sessions]}")
    perc = StreetPerception(str(MODEL))
    t_all = time.time()
    with open(SCAN, "w", encoding="utf-8") as f:
        for si, sess in enumerate(sessions):
            index = [json.loads(x) for x in
                     (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
                     if x.strip()]
            t0 = time.time()
            for item in index:
                img = cv2.imread(str(sess / "frames" / item["file"]))
                if img is None:
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                res = perc.detect(rgb, frame_id=item["frame_id"], ts_ns=item["ts_ns"])
                f.write(json.dumps({
                    "sess": sess.name, "frame_id": item["frame_id"],
                    "coins": [list(b) for b in res.coins],
                    "cars": [list(b) for b in res.cars]}) + "\n")
            print(f"[{si + 1}/{len(sessions)}] {sess.name}: {len(index)} 帧 "
                  f"{time.time() - t0:.1f}s", flush=True)
    print(f"扫描完成 {time.time() - t_all:.0f}s → {SCAN}")


def _cluster_1d(vals: np.ndarray) -> list[np.ndarray]:
    """排序后按 CLUSTER_GAP 切缝，返回簇（每簇为读数数组）。"""
    if len(vals) == 0:
        return []
    s = np.sort(vals)
    cuts = np.where(np.diff(s) > CLUSTER_GAP)[0]
    return np.split(s, cuts + 1)


def _robust_stats(a: np.ndarray) -> dict:
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med)))
    return {"n": int(len(a)), "center": round(med, 3),
            "sigma": round(1.4826 * mad, 3),
            "p90": round(float(np.percentile(np.abs(a - med), 90)), 3)}


def _rows() -> list[tuple[str, int, int, float, str]]:
    """扫描记录 → (sess, stage, denom, x_lane, 帧键) 金币行。"""
    cal = load_calib()
    out = []
    for line in SCAN.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        stage = 2 if r["sess"].endswith("_p2") else 1
        for cx, cy, _w, _h, _conf in r["coins"]:
            denom = cy - cal.y_h
            if denom < cal.min_denom:
                continue
            out.append((r["sess"], stage, denom, x_lane_of(cx, cy, cal),
                        (r["sess"], r["frame_id"])))
    return out


def cmd_read(split_stage: bool) -> None:
    rows = _rows()
    if not rows:
        print("无数据：先跑 scan")
        return
    print(f"金币读数 n={len(rows)}（域内 denom≥{load_calib().min_denom}）")

    # ① 分带聚类
    scope = "全部" if not split_stage else ""
    stages = (1, 2) if split_stage else (0,)
    for st in stages:
        sub = [r for r in rows if st == 0 or r[1] == st]
        if not sub:
            continue
        tag = f"— 阶段 p{st} —" if split_stage else f"— {scope}（p1+p2）—"
        print(f"\n{tag}")
        print(f"{'带(denom)':>12s} {'读数':>6s} {'簇':>3s} {'簇心(稳健)':>10s} "
              f"{'σ_MAD':>6s} {'P90偏':>6s} {'偏格':>6s}")
        for lo, hi in BANDS:
            vals = np.array([r[3] for r in sub if lo <= r[2] < hi])
            if len(vals) < 50:
                print(f"{f'[{lo},{hi})':>12s} {len(vals):>6d}  （样本不足）")
                continue
            clusters = _cluster_1d(vals)
            cs = [_robust_stats(c) for c in clusters if len(c) >= 30]
            if not cs:
                continue
            sig = float(np.median([c["sigma"] for c in cs]))
            p90 = float(np.median([c["p90"] for c in cs]))
            ctrs = [c["center"] for c in cs]
            # 偏格：簇心到最近半车道格点（游戏路网格应落整数±0.5 系）的距离中位
            grid = float(np.median([abs(c - round(c * 2) / 2) for c in ctrs]))
            print(f"{f'[{lo},{hi})':>12s} {len(vals):>6d} {len(cs):>3d} "
                  f"{' '.join(f'{c:.2f}' for c in ctrs):>10s} "
                  f"{sig:>6.3f} {p90:>6.3f} {grid:>6.3f}")

    # 簇心间隔（跨帧同列读数应恒 1.0 车道宽）——a_x 标定交叉核对
    near = np.array([r[3] for r in rows if r[2] >= 200])
    centers = sorted(float(np.median(c)) for c in _cluster_1d(near) if len(c) >= 30)
    gaps = [round(centers[i + 1] - centers[i], 3) for i in range(len(centers) - 1)]
    print(f"\n近场(denom≥200)簇心: {[round(c, 2) for c in centers]}")
    print(f"相邻簇心间隔: {gaps}（真车道宽恒 1.0 → a_x 交叉核对）")

    # ② 帧内同列共模消除差
    cal = load_calib()
    diffs: list[float] = []
    by_frame: dict[tuple[str, int], list[tuple[float, float, float]]] = defaultdict(list)
    for line in SCAN.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        for cx, cy, _w, _h, _conf in r["coins"]:
            d = cy - cal.y_h
            if d >= cal.min_denom:
                by_frame[(r["sess"], r["frame_id"])].append(
                    (x_lane_of(cx, cy, cal), d, cy))
    for key, items in by_frame.items():
        xl = np.array([t[0] for t in items])
        for cl in _cluster_1d(xl):
            m = float(np.median(cl))
            pair = [items[i] for i, v in enumerate(xl) if abs(v - m) <= CLUSTER_GAP]
            for a in range(len(pair)):
                for b in range(a + 1, len(pair)):
                    if abs(pair[a][2] - pair[b][2]) <= PAIR_MAX_CY:
                        diffs.append(abs(pair[a][0] - pair[b][0]))
    if len(diffs) < 20:
        print(f"\n帧内同列对不足（n={len(diffs)}），共模差读数缺")
        return
    d = np.array(diffs)
    # 两次独立同 σ 读数之差 ~ N(0, 2σ²)；用 MAD 系估 σ_Δ 再 /√2 回到单读数
    sig_diff = 1.4826 * float(np.median(np.abs(d - np.median(d))))
    print(f"\n② 帧内同列行距≤{PAIR_MAX_CY:.0f}px 对: n={len(d)}  "
          f"|Δx_lane| 中位 {float(np.median(d)):.3f}  σ_Δ {sig_diff:.3f} → "
          f"单读数噪声 ≈{sig_diff / np.sqrt(2):.3f} 车道（共模已消）")
    print("判读口径：贴掠可控性 = ①近场σ 与 ②单读数噪声同量级取小者；"
          "①含曲率与标定系统偏，是保守上界，②是控制器能吃到的乐观值。")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--only", default="20260922")
    a = sub.add_parser("analyze")
    a.add_argument("--split", default="")
    args = p.parse_args()
    if args.cmd == "scan":
        cmd_scan(args.only)
    else:
        cmd_read(args.split == "stage")


if __name__ == "__main__":
    main()
