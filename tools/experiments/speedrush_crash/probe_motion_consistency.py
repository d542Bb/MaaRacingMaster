# -*- coding: utf-8 -*-
"""离线影子：横向运动模型与路缘观测位移的一致性检查（撞车复盘实验 1）。

只读控制 trace，不 import 生产代码、不发控制指令。判据：
模型横向速度 |v_lat_est| ≥ 0.6 道/秒，而路缘观测在 0.22~0.45 秒窗口反推的
速度不到其 35%，连续触发算一段。

路缘观测在 trace 里是 ``ro_off``（自车相对路心，道，右正），每 0.1~0.2 秒才
更新一次、一次跳 0.07~0.15 道——反推速度噪声约 ±0.3 道/秒，只能抓大偏差。
"""

from __future__ import annotations

import argparse
import glob
import json
import os


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def analyze(path: str, min_model_v: float, ratio: float,
            window_s: tuple[float, float]):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    t = [0.0]
    for r in rows[1:]:
        t.append(t[-1] + (r.get("dt") or 0.05))
    obs = []
    for i, r in enumerate(rows):
        ro = r.get("ro_off")
        if ro is None:
            obs.append(None)
            continue
        v = None
        for j in range(i - 1, -1, -1):
            dt = t[i] - t[j]
            if dt < window_s[0]:
                continue
            if dt > window_s[1]:
                break
            if rows[j].get("ro_off") is not None:
                v = (ro - rows[j]["ro_off"]) / dt
                break
        obs.append(v)
    flags, detail = [], []
    for i, r in enumerate(rows):
        mv, ov = r.get("v_lat_est"), obs[i]
        if mv is None or ov is None:
            continue
        if abs(mv) >= min_model_v and abs(ov) < ratio * abs(mv):
            flags.append(i)
            detail.append((i, r["fid"], mv, ov, r.get("steer_norm")))
    segs = []
    for i in flags:
        if segs and i - segs[-1][-1] <= 3:
            segs[-1].append(i)
        else:
            segs.append([i])
    return rows, t, detail, segs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--min-model-v", type=float, default=0.6)
    ap.add_argument("--ratio", type=float, default=0.35)
    ap.add_argument("--window", default="0.22-0.45")
    args = ap.parse_args()
    a, b = (float(x) for x in args.window.split("-"))
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        rows, t, detail, segs = analyze(path, args.min_model_v, args.ratio, (a, b))
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        print(f"   触发段 {len(segs)}  命中拍 {len(detail)}/{len(rows)}")
        by_row = {i: d for d in detail for i in [d[0]]}
        for seg in segs:
            i0, i1 = seg[0], seg[-1]
            d0 = by_row[i0]
            print(f"   拍{i0}-{i1}  {(t[i1]-t[i0]):.2f}s  fid {rows[i0]['fid']}-{rows[i1]['fid']}"
                  f"  模型v {d0[2]:+.2f}  观测v {d0[3]:+.2f}  杆 {d0[4]:+.2f}")


if __name__ == "__main__":
    main()
