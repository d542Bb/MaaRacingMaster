# -*- coding: utf-8 -*-
"""门控重标定探针：估计接近速度 Z×Δlog h 能否替代 Δlog h 当混合测距的门控量。

**背景**（刀一实机首验，docs/plan/speedrush-hybrid-ranging-design.md §8.1）：
Δlog h 门控（T=0.046/拍，近距标定）在远距接近段全部漏报——looming 率≈接近速度/
距离，是距离相关的量。刀二候选门控量 = Z_anchor × Δlog h / dt（m/s，距离无关）。
本探针用同一局数据（trace 锚值流 + demo 帧料离线 h 流）回答三个问题：
  Q1 有效性：h 比例推算的接近速度 v̂_h = Z×ln(h2/h1)/dt 与 MoGe 锚值实测接近速度
      v_z = (Z1−Z2)/dt 是否一致（误差分位 + 相关系数）；
  Q2 分层分布：v̂_h 在「锚值下降段（真接近）」vs「平/升段」的分布，阈值可分性；
  Q3（隐含）：Youden 扫阈给刀二阈值起值。
**标尺独立性**：真接近与否的标签来自 trace 的 MoGe 锚值序列（Z 降幅），门控量
v̂_h 来自 2D 框高比例——两个独立信源，非循环论证。
**自包含**：不 import maaracing_master；YOLO/关联复用同目录 probe_approach_ab。
产物只走 stdout（标定数字直接打印；图按需后补）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_gate_recal.py \
        --demo <demo目录名> --trace <trace.jsonl文件名> [--conf-gap 10]
输入只接受 demos/ 与 control_traces/ 白名单域内的目录名/文件名。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.experiments.speedrush_vision import probe_approach_ab as ab  # noqa: E402

_DATA_ROOT = (Path.home() / "AppData" / "Roaming" / "MaaRacingMaster"
              / "data" / "speedrush").resolve()
_DEMOS_ROOT = _DATA_ROOT / "demos"
_TRACES_ROOT = _DATA_ROOT / "control_traces"
_DEMO_NAME_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_-]*\Z")
_FILE_NAME_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.-]*\Z")


def _under_root(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def load_frames(demo: Path) -> list[dict]:
    rows = [json.loads(l) for l in open(demo / "frames.jsonl", encoding="utf-8")]
    rows.sort(key=lambda r: r["frame_id"])
    return rows


def offline_h_stream(demo: Path, rows: list[dict]) -> dict[int, list[dict]]:
    """离线 YOLO 逐帧车框 → IoU 关联（ab.associate）→ 轨迹池。返回 fid → 车框。"""
    import cv2
    ysess, names = ab.yolo_session()
    per_fid: dict[int, list[dict]] = {}
    tracks: list[dict] = []
    for r in rows:
        fname = r["file"]
        if not _FILE_NAME_RE.fullmatch(fname) or ".." in fname:
            continue
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / fname)),
                           cv2.COLOR_BGR2RGB)
        dets = [d for d in ab.yolo_detect(ysess, names, img) if d["cls"] == "car"]
        for d in dets:
            d["fid"] = r["frame_id"]
        ab.associate(tracks, dets, r["frame_id"])
        per_fid[r["frame_id"]] = dets
    return per_fid


def load_trace_anchors(trace: Path) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for l in open(trace, encoding="utf-8"):
        r = json.loads(l)
        for d in r.get("dist") or []:
            if d["src"] == "ANCHOR" and d["m"] is not None:
                out.setdefault(r["fid"], []).append(d)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--conf-gap", type=float, default=10.0)
    args = ap.parse_args()
    if not _DEMO_NAME_RE.fullmatch(args.demo) or ".." in args.demo:
        raise SystemExit("demo 名不合法")
    if not _FILE_NAME_RE.fullmatch(args.trace) or ".." in args.trace:
        raise SystemExit("trace 文件名不合法")
    demo = (_DEMOS_ROOT / args.demo).resolve()
    trace = (_TRACES_ROOT / args.trace).resolve()
    if not _under_root(demo, _DEMOS_ROOT) or not _under_root(trace, _TRACES_ROOT):
        raise SystemExit("输入路径越界")

    rows = load_frames(demo)
    per_fid = offline_h_stream(demo, rows)
    anchors = load_trace_anchors(trace)

    # 锚平台宽 3~4 拍：demo 帧 fid 就近取 ≤6 fid（≈200ms，锚点年龄窗内）的锚拍
    anchor_fids = sorted(anchors)
    import bisect

    def nearest_anchor_m(fid: int):
        i = bisect.bisect_left(anchor_fids, fid)
        for cand in anchor_fids[max(0, i - 3):i + 3]:
            if abs(cand - fid) <= 6 and len(anchors[cand]) == 1:
                return anchors[cand][0]["m"]
        return None

    joined: dict[int, tuple[float, float]] = {}
    multi = 0
    for fid, dets in per_fid.items():
        m = nearest_anchor_m(fid)
        if len(dets) == 1 and m is not None:
            b = dets[0]["box"]
            joined[fid] = (b[3] - b[1], m)
        elif dets or m is not None:
            multi += 1
    fids = sorted(joined)
    print(f"demo={args.demo} trace={args.trace}")
    print(f"帧 {len(rows)}  单车道对齐帧 {len(joined)}  多车/缺对齐帧 {multi}")
    if len(fids) < 6:
        print("对齐样本不足，无法标定")
        return

    pairs = []
    for f1, f2 in zip(fids, fids[1:]):
        if f2 - f1 > 90:
            continue
        h1, z1 = joined[f1]
        h2, z2 = joined[f2]
        dt = (f2 - f1) / 30.0
        if h1 <= 0 or h2 <= 0 or dt <= 0:
            continue
        pairs.append({
            "fid1": f1, "fid2": f2, "dt": dt, "Z": z1,
            "v_h": z1 * math.log(h2 / h1) / dt,
            "v_z": (z1 - z2) / dt,
        })
    vh = np.array([p["v_h"] for p in pairs])
    vz = np.array([p["v_z"] for p in pairs])
    ok = np.isfinite(vh) & np.isfinite(vz)
    vh, vz = vh[ok], vz[ok]
    lab = np.array([1 if p["v_z"] > args.conf_gap else 0 for p in pairs])[ok]
    err = vh - vz
    print(f"Q1 对数 n={len(vh)}")
    print(f"  v_h p50={np.median(vh):.1f}  v_z p50={np.median(vz):.1f} m/s")
    print(f"  误差 p50={np.median(err):.1f}  p90abs={np.percentile(np.abs(err),90):.1f} m/s")
    if len(vh) > 8:
        print(f"  相关系数 r={np.corrcoef(vh, vz)[0, 1]:.3f}")
    ap_v, an_v = vh[lab == 1], vh[lab == 0]
    print(f"Q2 接近 n={lab.sum()}  非接近 n={(1 - lab).sum()}")
    for name, v in (("接近", ap_v), ("非接近", an_v)):
        if len(v):
            print(f"  {name}: p10={np.percentile(v, 10):.1f} "
                  f"p50={np.median(v):.1f} p90={np.percentile(v, 90):.1f} m/s")
    best = (0.0, None)
    for t in np.unique(np.round(vh, 1)):
        tp = float((ap_v > t).mean()) if len(ap_v) else 0.0
        fp = float((an_v > t).mean()) if len(an_v) else 0.0
        if tp - fp > best[0]:
            best = (tp - fp, (float(t), tp, fp))
    if best[1]:
        print(f"  Youden T={best[1][0]} m/s  TPR={best[1][1]:.2f} FPR={best[1][2]:.2f} J={best[0]:.2f}")

    # 产物：字面量文件名写当前工作目录（demo/trace 名记在 json 内容里，覆盖式）
    json.dump({"demo": args.demo, "trace": args.trace,
               "pairs": pairs, "labels": lab.tolist(),
               "aligned": len(joined), "multi": multi},
              open("gate_recal.json", "w"))
    print("产物: gate_recal.json（当前工作目录）")


if __name__ == "__main__":
    main()
