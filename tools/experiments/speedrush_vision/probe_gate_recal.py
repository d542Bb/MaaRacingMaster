# -*- coding: utf-8 -*-
"""门控重标定探针（trace-only）：估计接近速度 Z×Δlog h 的有效性验证与阈值标定。

**背景**（刀一实机首验，docs/plan/speedrush-hybrid-ranging-design.md §8.1）：
Δlog h 门控（T=0.046/拍，近距标定）在远距接近段全部漏报——looming 率≈接近速度/
距离，是距离相关的量。刀二候选门控量 = Z_anchor × Δlog h / dt（m/s，距离无关）。

**材料 = 一份 trace 自足**：dist 列提供逐轨锚值流（m），car_h 列提供逐轨框高流
（h）——同一轨道 id 天然绑定两个量，无需离线复算、无 demo 对齐、无 DML/CPU
边缘翻转问题（h 就是产线感知实际看到的那个）。标尺独立性：接近与否的标签来自
锚值序列的降幅（MoGe Z），门控量来自框高比例（2D）——独立信源，非循环论证。

回答三个问题：
  Q1 有效性：锚点对（Z1@t1 → Z2@t2）内，v̂_h = Z1×ln(h2/h1)/(t2−t1) 与实测
      接近速度 v_z = (Z1−Z2)/(t2−t1) 的一致性（误差分位 + 相关系数）；
  Q2 分层：v̂_h 在「真接近对（Z 降幅 > conf-gap）」vs「平/升对」的分布；
  Q3 阈值：Youden 扫阈给刀二门控阈值起值。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_gate_recal.py \
        --trace <control_traces 下的 jsonl 文件名> [--conf-gap 10]
输入只接受 control_traces/ 白名单域内的文件名（正则 + resolve 前缀校验）。
产物只走 stdout（标定数字直接打印；同 trace 复跑可复现，无需落盘）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np

_DATA_ROOT = (Path.home() / "AppData" / "Roaming" / "MaaRacingMaster"
              / "data" / "speedrush").resolve()
_TRACES_ROOT = _DATA_ROOT / "control_traces"
_FILE_NAME_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.-]*\Z")


def _under_root(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def load_trace(path: Path) -> list[dict]:
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    rows.sort(key=lambda r: r["fid"])
    return rows


def track_streams(rows: list[dict]) -> dict[int, list[dict]]:
    """逐轨时间线：id → [(ts_ns, h, m|None)]。h 来自 car_h 列，m 只取
    ANCHOR 拍的已接受锚值（RESCALED 的 m 是推算值不是测量，不进标尺）。"""
    streams: dict[int, list[dict]] = {}
    for r in rows:
        hmap = {c["id"]: c["h"] for c in (r.get("car_h") or [])}
        mmap = {d["target_id"]: d for d in (r.get("dist") or [])}
        for tid in set(hmap) | set(mmap):
            m = None
            d = mmap.get(tid)
            if d is not None and d["src"] == "ANCHOR" and d["m"] is not None:
                m = d["m"]
            streams.setdefault(tid, []).append(
                {"ts": r["ts_ns"], "h": hmap.get(tid), "m": m})
    return streams


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True,
                    help="control_traces 下的 trace jsonl 文件名")
    ap.add_argument("--conf-gap", type=float, default=10.0,
                    help="锚点对判「真接近」的 Z 降幅阈（m）")
    args = ap.parse_args()
    if not _FILE_NAME_RE.fullmatch(args.trace) or ".." in args.trace:
        raise SystemExit("trace 须是 control_traces 下的文件名")
    trace = (_TRACES_ROOT / args.trace).resolve()
    if not _under_root(trace, _TRACES_ROOT):
        raise SystemExit("输入路径越界")

    rows = load_trace(trace)
    streams = track_streams(rows)
    n_h = sum(1 for s in streams.values() if any(e["h"] for e in s))
    print(f"拍数 {len(rows)}  车轨 {len(streams)}  有h流轨 {n_h}")

    # 锚点对级样本：同一轨的两个相邻已接受锚（同平台重复锚去重：m 变了才算）
    pairs = []
    for tid, s in streams.items():
        last = None
        for e in s:
            if e["m"] is None:
                continue
            if last is not None and e["m"] != last["m"] and e["ts"] > last["ts"]:
                h1, h2 = last["h"], e["h"]
                if h1 and h2 and h1 > 0 and h2 > 0:
                    dt = (e["ts"] - last["ts"]) / 1e9
                    if 0.02 < dt < 2.0:
                        pairs.append({
                            "tid": tid, "dt": dt, "Z": last["m"],
                            "v_h": last["m"] * math.log(h2 / h1) / dt,
                            "v_z": (last["m"] - e["m"]) / dt,
                        })
            last = e
    vh = np.array([p["v_h"] for p in pairs])
    vz = np.array([p["v_z"] for p in pairs])
    ok = np.isfinite(vh) & np.isfinite(vz)
    vh, vz = vh[ok], vz[ok]
    lab = np.array([1 if p["v_z"] > args.conf_gap else 0 for p in pairs])[ok]
    if len(vh) < 10:
        print(f"锚点对仅 {len(vh)}，材料不足（正常跑一局带交通的路即可）")
        return
    err = vh - vz
    print(f"\nQ1 锚点对 n={len(vh)}")
    print(f"  v_h p50={np.median(vh):7.1f}  v_z p50={np.median(vz):7.1f} m/s")
    print(f"  误差(v_h-v_z) p50={np.median(err):6.1f}  p90abs={np.percentile(np.abs(err),90):5.1f} m/s")
    print(f"  相关系数 r={np.corrcoef(vh, vz)[0, 1]:.3f}")

    ap_v, an_v = vh[lab == 1], vh[lab == 0]
    print(f"\nQ2 分层（真接近=对内 Z 降幅>{args.conf_gap}m）  接近 n={lab.sum()}  非接近 n={(1-lab).sum()}")
    for name, v in (("接近", ap_v), ("非接近", an_v)):
        if len(v):
            print(f"  {name}: p10={np.percentile(v,10):6.1f} p50={np.median(v):6.1f} "
                  f"p90={np.percentile(v,90):6.1f} m/s")
    best = (0.0, None)
    for t in np.unique(np.round(vh, 1)):
        tp = float((ap_v > t).mean()) if len(ap_v) else 0.0
        fp = float((an_v > t).mean()) if len(an_v) else 0.0
        if tp - fp > best[0]:
            best = (tp - fp, (float(t), tp, fp))
    if best[1]:
        t, tp, fp = best[1]
        print(f"  Youden T={t} m/s  TPR={tp:.2f} FPR={fp:.2f} J={best[0]:.2f}")
    print("（标定数字只走 stdout；同 trace 复跑可复现）")


if __name__ == "__main__":
    main()
