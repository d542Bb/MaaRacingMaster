# -*- coding: utf-8 -*-
"""选「脏数据」帧，产给 gold_annotate.py 的人工标注清单（方案 A：验不变量在脏数据上崩不崩）。

脏 = 两桶，都**独立于金标已有帧**、且都是驾驶画面：
  ① `dirty_steer`：未用于金标的 demo 里，**转向输入最凶**的帧（pads.jsonl 的 lx 极值；
     取帧时刻 ±120ms 窗内 |lx| 最大者 + 快速打舵（符号翻转）加权），每场限 2 帧、
     间隔 ≥0.25s —— 逼出手忙脚乱的机动（这正是"理想数据"里缺的）。
  ② `dirty_dark`：**最暗**的帧（夜间/黄昏/隧道）—— 边线对比度最差的一档。

注：`control_traces/badframes_*` **不用**——实测那些快照多为结算/菜单界面，不是驾驶画面，
标注不了边线。

输出：清单 CSV（列 path,stratum）+ 缩略图拼版 PNG（先看再标）。
用法：python tools/experiments/speedrush_vision/select_dirty_frames.py [--n 30]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

import cv2
import numpy as np

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"
TRACES = DATA / "control_traces"
REVIEW = DATA / "depth_review"
GOLD = REVIEW / "gold_labels.csv"
WIN_NS = 120_000_000          # ±120ms


def _gold_paths() -> set[str]:
    if not GOLD.exists():
        return set()
    return {r["path"] for r in csv.DictReader(GOLD.open(encoding="utf-8"))}


def _tx(d: Path) -> str:
    q = Path(d)
    return q.parent.parent.name if q.parent.name == "frames" else q.parent.name


def _read_jsonl(p: Path):
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _steer_extremes(fr: list[dict], pads: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """逐帧 → (帧时刻 ±120ms 窗内 |lx| 最大值, 窗内 lx 符号翻转次数)。"""
    pt = np.array([p["ts_ns"] for p in pads], np.int64)
    lx = np.array([abs(p.get("lx", 0)) for p in pads], np.float64)
    sgn = np.sign([p.get("lx", 0) for p in pads])
    ft = np.array([f["ts_ns"] for f in fr], np.int64)
    lo = np.searchsorted(pt, ft - WIN_NS, "left")
    hi = np.searchsorted(pt, ft + WIN_NS, "right")
    mx = np.array([lx[a:b].max() if b > a else 0.0 for a, b in zip(lo, hi)])
    flips = np.array([int(np.abs(np.diff(sgn[a:b])).sum()) if b - a > 2 else 0
                      for a, b in zip(lo, hi)])
    return mx, flips


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--per-demo", type=int, default=2)
    ap.add_argument("--min-gap-s", type=float, default=0.25)
    args = ap.parse_args()
    gold = _gold_paths()
    gold_demos = {_tx(p) for p in gold}

    # ① 未用过的 demo 里挑"最凶"的帧 + 最暗的帧
    steer_c: list[tuple[float, Path, str]] = []
    dark_c: list[tuple[float, Path, str]] = []
    for d in sorted(DEMOS.iterdir()):
        if not d.is_dir() or d.name in gold_demos or d.name.startswith("badframes"):
            continue
        fp, pp = d / "frames.jsonl", d / "pads.jsonl"
        if not (fp.exists() and pp.exists()):
            continue
        fr, pads = _read_jsonl(fp), _read_jsonl(pp)
        if len(fr) < 20 or len(pads) < 20:
            continue
        mx, flips = _steer_extremes(fr, pads)
        ts = np.array([f["ts_ns"] for f in fr], np.int64)
        order = np.argsort(-(mx + 200.0 * flips))     # 大转向优先，快速打舵次之
        taken, last = 0, -np.inf
        for i in order:
            if taken >= args.per_demo:
                break
            if ts[i] - last < args.min_gap_s * 1e9:
                continue
            p = d / "frames" / fr[i]["file"]
            if p.exists() and str(p) not in gold:
                steer_c.append((float(mx[i]), p, f"dirty_steer|{d.name}"))
                last, taken = ts[i], taken + 1
        # 亮度：每场取一帧最暗（抽 12 帧看，省读图）
        step = max(1, len(fr) // 12)
        for i in range(0, len(fr), step):
            p = d / "frames" / fr[i]["file"]
            if not p.exists() or str(p) in gold:
                continue
            im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if im is not None:
                dark_c.append((float(im.mean()), p, f"dirty_dark|{d.name}"))

    # ② 组清单：暗帧占 ~1/6，其余按转向凶度；每场限 per-demo
    picked: list = []
    per: dict[str, int] = {}
    n_dark = max(3, args.n // 6)
    for sc, p, st in sorted(dark_c)[: max(n_dark * 4, 20)]:
        if len(picked) >= n_dark:
            break
        k = st.split("|")[1]
        if per.get(k, 0) >= 1:
            continue
        per[k] = per.get(k, 0) + 1
        picked.append((sc, p, st))
    for sc, p, st in sorted(steer_c, key=lambda c: -c[0]):
        if len(picked) >= args.n:
            break
        k = st.split("|")[1]
        if per.get(k, 0) >= args.per_demo:
            continue
        per[k] = per.get(k, 0) + 1
        picked.append((sc, p, st))
    picked = picked[: args.n]

    lst = REVIEW / "gold_dirty_frames.csv"
    with lst.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "stratum"])
        for sc, p, st in picked:
            w.writerow([str(p), f"{st}|s={sc:.0f}"])
    print(f"清单 {len(picked)} 帧 → {lst}")
    srcs: dict[str, int] = {}
    for _, _, st in picked:
        k = st.split("|")[1]
        srcs[k] = srcs.get(k, 0) + 1
    print("来源分布:", ", ".join(f"{k}×{v}" for k, v in sorted(srcs.items(), key=lambda kv: -kv[1])))

    cols = 5
    rows_n = (len(picked) + cols - 1) // cols
    sheet = np.zeros((rows_n * 180, cols * 320, 3), np.uint8)
    for idx, (sc, p, st) in enumerate(picked):
        img = cv2.imread(str(p))
        if img is None:
            continue
        t = cv2.resize(img, (320, 180))
        cv2.putText(t, f"{idx}|{st.split('|')[-1]}", (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1)
        r, c = divmod(idx, cols)
        sheet[r * 180:(r + 1) * 180, c * 320:(c + 1) * 320] = t
    shot = REVIEW / "gold_dirty_sheet.png"
    cv2.imwrite(str(shot), sheet)
    print(f"缩略图 → {shot}")


if __name__ == "__main__":
    main()
