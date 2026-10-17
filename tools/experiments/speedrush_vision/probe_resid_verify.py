# -*- coding: utf-8 -*-
"""投影残差复验（修正口径：dgeo_focal + 框底接地点，DEPTH_CHAIN_LATENCY §1.2）。

修正口径下基线（5-10m 段近场轨）：残差 P50=3px、m−m_pix P50=+0.05m，
近场真实误差是离散度（P10 −3.1m / P90 +4.9m），不是系统性偏大。
本探针对比改前基线局与锚龄钟对齐后新局：resid_m 符号中心（偏置是否≈0）、
离散度 P10/P90、HOLD vs RANGE_PROP 分开（外推是否在补滞后）。

只读 debug 产物，不改生产。
"""
from __future__ import annotations

import collections
import json
import os
import statistics as st
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
BATCHES = {
    "改前基线": ["20261016_222406_p1", "20261016_222451_p2",
                 "20261016_222615_p1", "20261016_222700_p2",
                 "20261016_224328_p1", "20261016_224412_p2"],
    "本次（钟对齐+提速后）": ["20261017_170531_p1", "20261017_170616_p2"],
}

Y_H = 324.2


def _safe(d: str) -> Path:
    """trace 路径校验（固定目录 + 禁 .. 分量，越界即拒绝）。"""
    if ".." in Path(d).parts:
        raise ValueError(f"路径分量含 ..：{d}")
    p = (DBG / d).resolve()
    if DBG.resolve() not in p.parents:
        raise ValueError(f"路径越界：{d}")
    return p


def _p(xs, q):
    xs = sorted(xs)
    return float("nan") if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def bucket(m: float) -> str:
    if m < 10:
        return "5-10m"
    if m < 20:
        return "10-20m"
    return "20m+"


def report(label: str, traces: list[str]) -> None:
    rows: list[dict] = []
    for d in traces:
        rows += [json.loads(line) for line in
                 open(_safe(d) / f"trace_{d}.jsonl", encoding="utf-8")]

    # 批内 focal 兜底（个别拍 dgeo_focal 缺失时用批内 P50）
    focals = [r["dgeo_focal"] for r in rows
              if r.get("dgeo_focal") is not None]
    focal_fb = _p(focals, 0.5) if focals else 589.7

    # (bucket, method) -> [(resid_m, resid_px)]
    res = collections.defaultdict(list)
    age_h = collections.defaultdict(list)
    n_rows = 0
    for r in rows:
        cam_h = r.get("dgeo_cam_h")
        if cam_h is None:
            continue
        focal = r.get("dgeo_focal") or focal_fb
        ch = {c["id"]: c for c in (r.get("car_h") or [])}
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        for x in (r.get("dist") or []):
            m = x.get("m")
            tid = x.get("id")
            src = x.get("src") or ""
            method = x.get("method") or "-"
            if m is None or src not in ("ANCHOR", "RESCALED_ANCHOR"):
                continue
            c = cp.get(tid)
            h = ch.get(tid, {}).get("h")
            if c is None or c.get("cy") is None or not h:
                continue   # 缺框高时不按 h=0 兜底——框底错半框高，近场残差被放大
            cy_ground = c["cy"] + h / 2.0
            den = cy_ground - Y_H
            if den <= 5:
                continue
            resid_px = cy_ground - (Y_H + focal * cam_h / m)
            m_pix = focal * cam_h / den
            res[(bucket(m), method)].append((m - m_pix, resid_px))
            if x.get("age") is not None:
                age_h[method].append(x["age"])
            n_rows += 1

    lags = [r["asheet"]["fid_lag"] for r in rows
            if r.get("asheet") and r["asheet"].get("fid_lag") is not None]
    dms = [r["dgeo_ms"] for r in rows if r.get("dgeo_ms") is not None]
    dams = [r["dgeo_age_ms"] for r in rows if r.get("dgeo_age_ms") is not None]

    print(f"\n=== {label} === 局={len(traces)} 拍={len(rows)} "
          f"有锚车·拍={n_rows} focal P50={focal_fb:.1f}")
    print(f"  dgeo_ms P50={_p(dms, .5):.1f} | dgeo_age_ms P50={_p(dams, .5):.0f} | "
          f"dist.age P50={_p([a for v in age_h.values() for a in v], .5):.0f}")
    print(f"  fid_lag P50={_p(lags, .5):.0f} 均值={sum(lags) / max(len(lags), 1):.2f} "
          f"分布={dict(sorted(collections.Counter(lags).items()))}")
    for key in sorted(res):
        vals = sorted(v[0] for v in res[key])
        px = sorted(abs(v[1]) for v in res[key])
        if len(vals) < 8:
            continue
        print(f"  [{key[0]:>6} {key[1]:>12}] n={len(vals):4d} "
              f"resid_m P10={_p(vals, .1):+.2f} P50={_p(vals, .5):+.2f} "
              f"P90={_p(vals, .9):+.2f} | |resid_px| P50={_p(px, .5):.1f}")


for label, traces in BATCHES.items():
    report(label, traces)
