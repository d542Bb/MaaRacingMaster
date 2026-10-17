# -*- coding: utf-8 -*-
"""锚龄钟与距离陈旧度核验（改前基线 / 改后验收同一口径）。

回答四件事（只读 debug 产物，不改生产）：

1. **锚龄钟起点**：新锚 ingest 那一拍的 ``dist.age`` 是「内容龄」（≈ fid_lag×
   帧间隔 + 本拍帧龄）还是「到达龄」（≈ 本拍帧龄）？判据是比值——
   内容龄/预期≈1 即采集钟，≈0 即到达钟。
2. **投影一致性残差**：``|cy − (y_h + vpx·cam_h/m)|`` 按距离分桶（与
   probe_hold_cf 同口径，可跨批对照）。
3. **残差来源分解**：同拍像素反推 ``m_proj = vpx·cam_h/(cy−y_h)`` 与暴露值
   ``m`` 的差、按距离桶×锚龄桶交叉——**桶内**随锚龄增长才叫"陈旧"，
   与锚龄无关的部分是尺度/几何偏置（MoGe 尺度或框心口径），不是时钟问题。
4. **持锚误差**：同轨旧锚 m 与接替锚 m 的差（含真实运动，故同时报区间）。

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_anchor_clock.py``
环境变量 ``SR_TRACES``（逗号分隔局名）可换批次，缺省 = 20261017 四局。"""
from __future__ import annotations

import collections
import json
import os
import statistics as st
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = [s for s in os.environ.get(
    "SR_TRACES",
    "20261017_115032_p1,20261017_115107_p2,"
    "20261017_115223_p1,20261017_115308_p2").split(",") if s]

Y_H, VPX = 324.2, 640.7
BUCKETS = ("5-10m", "10-20m", "20m+")


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
    return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def _bucket(m: float) -> str:
    return "5-10m" if m < 10 else ("10-20m" if m < 20 else "20m+")


def _age_bin(age: float) -> str:
    return ("age<100" if age < 100 else "age100-250" if age < 250
            else "age250-450" if age < 450 else "age>=450")


# ── 逐局读入 ──
rows: list[dict] = []
interval_ms: list[float] = []
for d in TRACES:
    rs = [json.loads(line) for line in
          open(_safe(d) / f"trace_{d}.jsonl", encoding="utf-8")]
    for r in rs:
        r["_trace"] = d
    rows.extend(rs)
    for a, b in zip(rs, rs[1:]):
        df, dt = b["fid"] - a["fid"], (b["ts_ns"] - a["ts_ns"]) / 1e9
        if df > 0 and dt > 0:
            interval_ms.append(dt / df * 1000.0)

IV = st.median(interval_ms) if interval_ms else 25.0
print(f"=== 数据面 === {len(TRACES)} 局 / {len(rows)} 拍，"
      f"帧间隔 P50={IV:.2f}ms（n={len(interval_ms)}）")

lags = [r["asheet"]["fid_lag"] for r in rows
        if r.get("asheet") and r["asheet"].get("fid_lag") is not None]
print(f"fid_lag P50={_p(lags, .5):.0f} 帧（{IV*_p(lags, .5):.0f}ms），"
      f"分布={dict(sorted(collections.Counter(lags).items()))}")

# ── 1. 锚龄钟起点：新锚 ingest 拍 ──
by_track = collections.defaultdict(list)
for r in rows:
    for x in (r.get("dist") or []):
        by_track[(r["_trace"], x.get("id"))].append((r, x))

clock_ratio, clock_rows = [], []
for key, seq in by_track.items():
    for (r0, x0), (r1, x1) in zip(seq, seq[1:]):
        a0, a1 = x0.get("age"), x1.get("age")
        lag = (r1.get("asheet") or {}).get("fid_lag")
        if a0 is None or a1 is None or lag is None:
            continue
        if not (x1.get("src") == "ANCHOR" and x1.get("method") == "HOLD"):
            continue
        if a1 >= a0 - 1.0:
            continue                      # 未回跳：不是新锚拍
        expect = lag * IV + (r1.get("oage") or 0.0)
        if expect <= 1.0:
            continue
        clock_ratio.append(a1 / expect)
        clock_rows.append((a1, expect, lag, r1.get("oage") or 0.0))

print("\n=== 1. 锚龄钟起点（新锚 ingest 拍） ===")
if clock_ratio:
    r50 = _p(clock_ratio, .5)
    verdict = ("采集钟（内容龄）" if r50 > 0.7 else
               "到达钟（到达龄）" if r50 < 0.3 else "混合/无法判定")
    print(f"n={len(clock_ratio)} 实测age/预期内容龄 P50={r50:.2f} "
          f"P10={_p(clock_ratio, .1):.2f} P90={_p(clock_ratio, .9):.2f} → {verdict}")
    print("  样本 age/预期(lag,oage)："
          + "; ".join(f"{a:.0f}/{e:.0f}({lag},{o:.0f})"
                      for a, e, lag, o in clock_rows[:6]))
else:
    print("无新锚回跳样本（锚未刷新或批次不含）")

# ── 2/3. 投影残差 + 分解（只取近场轨：car_pos.rel 非 None） ──
resid = collections.defaultdict(list)
dlt = collections.defaultdict(list)
cross = collections.defaultdict(list)
geo = collections.defaultdict(list)
for r in rows:
    cam_h = r.get("dgeo_cam_h")
    if not cam_h:
        continue
    cp = {c["id"]: c for c in (r.get("car_pos") or [])}
    hs = {c["id"]: c["h"] for c in (r.get("car_h") or [])}
    for x in (r.get("dist") or []):
        m, age = x.get("m"), x.get("age")
        if m is None or m <= 0.5 or age is None:
            continue
        if (x.get("src") or "") not in ("ANCHOR", "RESCALED_ANCHOR"):
            continue
        c = cp.get(x.get("id"))
        if c is None or c.get("cy") is None or c.get("rel") is None:
            continue                      # rel=None ⇒ 远场轨（框心不可靠）
        den = c["cy"] - Y_H
        if den <= 5:
            continue
        b = _bucket(m)
        resid[b].append(abs(c["cy"] - (Y_H + VPX * cam_h / m)))
        m_proj = VPX * cam_h / den
        dlt[b].append(m - m_proj)
        cross[(b, _age_bin(age))].append(m - m_proj)
        geo[b].append((m, m_proj, cam_h, c["cy"], hs.get(x.get("id"), 0)))

print("\n=== 2. 投影一致性残差（|cy − 预测行|，距离分桶，近场轨） ===")
for b in BUCKETS:
    v = resid[b]
    if not v:
        continue
    d = dlt[b]
    print(f"  {b}: n={len(v)} 残差 P50={_p(v, .5):.0f}px P90={_p(v, .9):.0f}px"
          f" | m−m_proj P50={_p(d, .5):+.2f}m P90={_p(d, .9):+.2f}m")

print("\n=== 2b. 桶内几何量（判残差是尺度偏置还是陈旧） ===")
for b in BUCKETS:
    g = geo[b]
    if not g:
        continue
    print(f"  {b}: n={len(g)} m P50={_p([x[0] for x in g], .5):.1f} "
          f"m_proj P50={_p([x[1] for x in g], .5):.1f} "
          f"cam_h P50={_p([x[2] for x in g], .5):.2f} "
          f"cy P50={_p([x[3] for x in g], .5):.0f} "
          f"h_px P50={_p([x[4] for x in g], .5):.0f} "
          f"cam_h_eff=(cy−y_h)·m/vpx P50="
          f"{_p([(x[3]-Y_H)*x[0]/VPX for x in g], .5):.2f}")

print("\n=== 3. 距离偏差按 距离桶×锚龄桶 交叉（桶内随龄增长=陈旧） ===")
for b in BUCKETS:
    cells = [(ab, cross[(b, ab)]) for ab in
             ("age<100", "age100-250", "age250-450", "age>=450")
             if cross[(b, ab)]]
    if not cells:
        continue
    print(f"  {b}: " + " | ".join(
        f"{ab} n={len(v)} P50={_p(v, .5):+.2f}" for ab, v in cells))

# ── 4. 持锚误差（旧锚 m − 接替锚 m；含真实运动，附区间） ──
hold = []
for key, seq in by_track.items():
    for (r0, x0), (r1, x1) in zip(seq, seq[1:]):
        m0, m1, a0, a1 = x0.get("m"), x1.get("m"), x0.get("age"), x1.get("age")
        if m0 is None or m1 is None or a0 is None or a1 is None:
            continue
        if x0.get("method") != "HOLD" or x1.get("method") != "HOLD":
            continue
        dt = (r1["ts_ns"] - r0["ts_ns"]) / 1e9
        if a1 >= a0 - 1.0 or dt > 2.0:
            continue                      # 只要"新锚接替旧锚"的瞬间
        hold.append((m0 - m1, dt, a0, x0.get("v")))

print("\n=== 4. 持锚误差（旧锚 m − 接替锚 m，>0=持锚偏大） ===")
if hold:
    e = [h[0] for h in hold]
    print(f"n={len(e)} 带符号 P50={_p(e, .5):+.2f}m P90={_p(e, .9):+.2f}m "
          f"|P50|={_p([abs(x) for x in e], .5):.2f}m")
    vs = [h[3] for h in hold if h[3] is not None and h[3] > 0]
    if vs:
        print(f"  同期有 v 的样本 n={len(vs)} v P50={_p(vs, .5):.1f}m/s "
              f"P90={_p(vs, .9):.1f}m/s")
else:
    print("无新锚接替样本")
