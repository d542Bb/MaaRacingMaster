# -*- coding: utf-8 -*-
"""深度供数链路时延与新鲜度台账（改前/改后同一口径）。

一局/一批 trace 一屏出齐（只读 debug 产物，不改生产）：

- 端到端：`dgeo_ms`（observe 本体）、`dgeo_seg`（cap/queue/pub + stages）、
  控制拍节奏、新证据拍占比；
- 新鲜度：`fid_lag` 分布、新锚 ingest 的内容龄（锚龄钟核验）、`dist.age`；
- 读数面：`sides` 覆盖、供数主人分布、质量门拒绝率、路心拍间跳。

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_chain_latency.py``
环境变量 ``SR_TRACES``（逗号分隔局名）换批次；缺省 = 本轮改前 4 局 + 改后 2 局
对照输出。"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
BATCHES = {
    "改前": ["20261017_115032_p1", "20261017_115107_p2",
             "20261017_115223_p1", "20261017_115308_p2"],
    "改后": ["20261017_130315_p1", "20261017_130400_p2"],
}
if os.environ.get("SR_TRACES"):
    BATCHES = {"当前": [s for s in os.environ["SR_TRACES"].split(",") if s]}

IV_MS = 25.1          # 采集帧间隔（40fps；批次内实测 P50）


def _safe(d: str) -> Path:
    """trace 路径校验（安全扫描：固定目录 + 禁 .. 分量，越界即拒绝）。"""
    if ".." in Path(d).parts:
        raise ValueError(f"路径分量含 ..：{d}")
    p = (DBG / d).resolve()
    if DBG.resolve() not in p.parents:
        raise ValueError(f"路径越界：{d}")
    return p


def _p(xs, q):
    xs = sorted(xs)
    return float("nan") if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def _rows(traces: list[str]) -> list[dict]:
    rows: list[dict] = []
    for d in traces:
        rows += [json.loads(line) for line in
                 open(_safe(d) / f"trace_{d}.jsonl", encoding="utf-8")]
    return rows


def report(label: str, traces: list[str]) -> None:
    rows = _rows(traces)
    tick, dfid = [], []
    for a, b in zip(rows, rows[1:]):
        dt = (b["ts_ns"] - a["ts_ns"]) / 1e9
        if 0 < dt < 1:
            tick.append(dt * 1000.0)
            dfid.append(b["fid"] - a["fid"])

    lags: list = []
    st: dict = collections.defaultdict(list)
    seg: dict = collections.defaultdict(list)
    dms: list = []
    ages: list = []
    an: list = []
    with_reading = new_ev = 0
    sides = collections.Counter()
    src = collections.Counter()
    rej = collections.Counter()
    for r in rows:
        s = r.get("dgeo_seg")
        if s:
            for k, v in s.items():
                if k != "stages" and isinstance(v, (int, float)):
                    seg[k].append(v)
            for k, v in (s.get("stages") or {}).items():
                st[k].append(v)
        if r.get("dgeo_ms") is not None:
            dms.append(r["dgeo_ms"])
            with_reading += 1
            sides[r.get("dgeo_sides")] += 1
            src[r.get("ro_source")] += 1
            if r.get("dgeo_new"):
                new_ev += 1
            for t in (r.get("dgeo_rejects") or "").split(";"):
                if t:
                    rej[t.split(":")[0][:6]] += 1
        if r.get("dgeo_age_ms") is not None:
            ages.append(r["dgeo_age_ms"])
        if r.get("asheet") and r["asheet"].get("fid_lag") is not None:
            lags.append(r["asheet"]["fid_lag"])
        for x in (r.get("dist") or []):
            if x.get("age") is not None:
                an.append(x["age"])

    # 新锚 ingest 的内容龄（钟起点核验：≈lag×帧间隔 + 本拍帧龄）
    by = collections.defaultdict(list)
    for r in rows:
        for x in (r.get("dist") or []):
            by[x.get("id")].append((r, x))
    ingest, ratio = [], []
    for seq in by.values():
        for (r0, x0), (r1, x1) in zip(seq, seq[1:]):
            if x0.get("age") is None or x1.get("age") is None:
                continue
            if not (x1.get("src") == "ANCHOR" and x1.get("method") == "HOLD"):
                continue
            if x1["age"] >= x0["age"] - 1.0:
                continue
            ingest.append(x1["age"])
            lag = (r1.get("asheet") or {}).get("fid_lag")
            if lag:
                ratio.append(x1["age"] / (lag * IV_MS + (r1.get("oage") or 0.0)))

    print(f"\n=== {label} === 局={len(traces)} 拍={len(rows)} "
          f"拍间隔 P50={_p(tick, .5):.1f}ms 每拍帧增量 P50={_p(dfid, .5):.0f}")
    print(f"  dgeo_ms P50={_p(dms, .5):.1f} P90={_p(dms, .9):.1f} | "
          f"新证据拍 {new_ev}/{with_reading}（{100 * new_ev / max(with_reading, 1):.0f}%）")
    print("  stages: " + " ".join(f"{k}={_p(v, .5):.1f}"
                                  for k, v in sorted(st.items())))
    print("  seg: " + " ".join(f"{k}={_p(v, .5):.1f}"
                               for k, v in sorted(seg.items())
                               if not k.startswith("cap_")))
    print(f"  fid_lag P50={_p(lags, .5):.0f} 均值={sum(lags) / max(len(lags), 1):.2f} "
          f"P90={_p(lags, .9):.0f} 分布={dict(sorted(collections.Counter(lags).items()))}")
    print(f"  新锚 ingest 内容龄 P50={_p(ingest, .5):.0f} P90={_p(ingest, .9):.0f}ms"
          f"（n={len(ingest)}）| 钟比值 P50={_p(ratio, .5):.2f}（≈1=采集钟）")
    print(f"  dist.age P50={_p(an, .5):.0f}ms | dgeo_age_ms P50={_p(ages, .5):.1f}")
    print(f"  sides={dict(sorted(sides.items()))} ro_source={dict(src)}")
    print(f"  质量门拒绝={dict(rej.most_common(4))}")


for name, traces in BATCHES.items():
    report(name, traces)
