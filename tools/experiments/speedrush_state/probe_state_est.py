# -*- coding: utf-8 -*-
"""目标状态插值离线探针（只读 trace，不 import 生产代码）。

输入：生产控制 trace 的 ``mcars`` / ``car_h`` / ``scene.road.bands`` 列。
输出：锚间 z 插值方法的误差、横向锁道提前量、道路切线覆盖率与抖动。
判据与口径见同目录 README。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
GATE0 = os.path.join(ROOT, "maaracing_master", "plugins", "speedrush",
                     "resources", "calibration", "gate0.json")


def load_lane_w():
    """车道宽读几何真源；实验不复制标定常量（读不到才用文件注记值）。"""
    try:
        with open(GATE0, encoding="utf-8") as fh:
            return float(json.load(fh)["lane_w_m"])
    except (OSError, KeyError, ValueError):
        return 3.38


LANE_W_M = load_lane_w()
DATA_ROOT = os.path.expandvars(
    r"%APPDATA%\MaaRacingMaster\debug\speedrush")
OUT_ROOT = os.path.expandvars(
    r"%APPDATA%\MaaRacingMaster\data\speedrush\state")
BOX_JUMP = 0.35          # 单拍框高相对跳变上限（框头/框全车翻转）
Z_GATE_MPS = 45.0        # 生产锚点跳变门的闭速界（tracking.z_jump_mps）
Z_GATE_SLACK_M = 2.0     # 生产跳变门基数（tracking.z_jump_base_m）
FRESH_LOOKBACK_S = 0.15  # 评估点找最近一次新鲜框高的回看窗
MAX_INTERVAL_S = 1.5     # 超过此间隔的锚对不评估（真值本身不可信）
NEAR_BAND_M = (3.0, 12.0)
MID_BAND_M = (12.0, 24.0)
LAT_BANDS = (40.0, 30.0, 20.0, 15.0)
X_DOMAIN_M = 10.0        # 横向域门：路面外目标不进锁道统计（远距误检可达 17 m）
LOCK_BAND = 0.6          # 滞回锁道：偏离超过 0.6 道
LOCK_STAY = 3            # 连续 3 拍才换锁


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def med(vals):
    return statistics.median(vals) if vals else None


def rnd(v, n=3):
    return None if v is None else round(float(v), n)


@dataclass
class Tick:
    t: float
    fid: int
    src: str | None
    x: float | None
    z: float | None
    v: float | None
    age: float | None
    h: float | None
    seen: int | None


@dataclass
class Anchor:
    t: float
    z: float
    v: float | None
    h: float | None
    idx: int
    fid: int
    age_ms: float | None = None


@dataclass
class Track:
    tid: int
    ticks: list[Tick] = field(default_factory=list)


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_tracks(rows):
    t0 = rows[0]["ts_ns"]
    tracks = {}
    for r in rows:
        t = (r["ts_ns"] - t0) / 1e9
        hs = {e["id"]: e for e in (r.get("car_h") or [])}
        for m in (r.get("mcars") or []):
            tid = m["id"]
            e = hs.get(tid) or {}
            tr = tracks.setdefault(tid, Track(tid))
            tr.ticks.append(Tick(
                t=t, fid=r.get("fid"), src=m.get("src"),
                x=m.get("x"), z=m.get("z"), v=m.get("v"),
                age=m.get("age"), h=e.get("h"), seen=e.get("seen")))
    return tracks, (rows[-1]["ts_ns"] - t0) / 1e9


def find_anchors(ticks):
    out = []
    for i, tk in enumerate(ticks):
        if tk.src != "ANCHOR" or tk.z is None:
            continue
        if out and abs(tk.z - out[-1].z) <= 1e-9:
            continue
        out.append(Anchor(tk.t, tk.z, tk.v, tk.h, i, tk.fid, tk.age))
    return out


def fresh_h_at(ticks, i0, t_eval):
    """t_eval 之前最近一次新鲜框高（seen==0），回看窗内；不含 t_eval 本身，
    避免把评估点的真值测量泄漏进预测。"""
    for j in range(i0, -1, -1):
        tk = ticks[j]
        if tk.t >= t_eval - 1e-6:
            continue
        if t_eval - tk.t > FRESH_LOOKBACK_S:
            break
        if tk.h is not None and tk.seen == 0:
            return tk.h
    return None


def box_alive_until(ticks, i0, i1, h0):
    """从锚点 i0 起，框高是否保持连续（无 >35% 单拍翻跳）。

    返回 (alive_frac, first_break_t)。只在新鲜框高拍上判跳变。
    """
    prev = h0
    n = 0
    ok = 0
    for j in range(i0 + 1, i1):
        tk = ticks[j]
        if tk.h is None or tk.seen != 0:
            continue
        n += 1
        step = abs(tk.h - prev) / max(prev, 1.0)
        if step > BOX_JUMP:
            return (ok / n if n else 0.0), tk.t
        ok += 1
        prev = tk.h
    return (ok / n if n else 0.0), None


def eval_point(ticks, i0, i1, t_eval):
    """在 t_eval 评估四种 z 方法（只用 t_eval 之前的信息）。"""
    a_tick = ticks[i0]
    z0 = a_tick.z
    v0 = a_tick.v or 0.0
    dt = t_eval - a_tick.t
    h0 = a_tick.h
    h1 = fresh_h_at(ticks, i1 - 1, t_eval)
    _, break_t = box_alive_until(ticks, i0, i1, h0)
    box_ok = (h0 is not None and h1 is not None
              and (break_t is None or t_eval <= break_t + 1e-6))
    z_cv = z0 - v0 * dt
    z_box = z0 * h0 / h1 if box_ok else None
    if z_box is not None and not (0.4 * z0 <= z_box <= 2.0 * z0):
        z_box = None
    z_fuse = 0.5 * (z_cv + z_box) if z_box is not None else z_cv
    return {"hold": z0, "cv": z_cv, "box": z_box, "fuse": z_fuse,
            "box_ok": z_box is not None}


def max_h_step(ticks, i0, i1):
    """区间内单拍框高最大相对步进（区分“平滑接近”与“换了个框”）。"""
    prev = None
    worst = 0.0
    for j in range(i0, i1 + 1):
        h = ticks[j].h
        if h is None:
            continue
        if prev:
            worst = max(worst, abs(h - prev) / prev)
        prev = h
    return worst


def analyse_z(tracks):
    rows = []
    alive_fracs = []
    noise = []
    n_intervals = 0
    for tr in tracks.values():
        ticks = tr.ticks
        anchors = find_anchors(ticks)
        if len(anchors) < 2:
            continue
        for a0, a1 in zip(anchors, anchors[1:]):
            dt = a1.t - a0.t
            if not (0.0 < dt <= MAX_INTERVAL_S):
                continue
            n_intervals += 1
            f, _ = box_alive_until(ticks, a0.idx, a1.idx, a0.h)
            alive_fracs.append(f)
            dz = abs(a1.z - a0.z)
            noise.append((dt, dz))
            plausible = dz <= Z_GATE_MPS * dt + Z_GATE_SLACK_M
            h_step = max_h_step(ticks, a0.idx, a1.idx)
            # 评估点只用区间内部：中点（真值=两锚线性插值）与 20/10 m 跨点。
            # 不在 a1 时刻评估——那里框高可能就来自锚点同一帧，等于泄漏真值。
            points = []
            if dt >= 0.12:
                points.append(("mid", a0.t + 0.5 * dt,
                               0.5 * (a0.z + a1.z)))
            lo, hi = (a0.z, a1.z) if a0.z >= a1.z else (a1.z, a0.z)
            for cross in (20.0, 10.0):
                if lo <= cross < hi:
                    frac = (a0.z - cross) / (a0.z - a1.z)
                    points.append((f"z{cross:.0f}", a0.t + frac * dt, cross))
            for kind, t_eval, truth in points:
                est = eval_point(ticks, a0.idx, a1.idx, t_eval)
                rows.append({"kind": kind, "truth": truth, "dt": dt,
                             "lead": t_eval - a0.t, "plausible": plausible,
                             "h_step": h_step, **est})
    return rows, alive_fracs, noise, n_intervals


def by_band(rows, key):
    bands = {"0-20": [], "20-40": [], "40-80": [], "80+": []}
    for r in rows:
        t = r["truth"]
        b = ("0-20" if t < 20 else "20-40" if t < 40
             else "40-80" if t < 80 else "80+")
        if r.get(key) is not None:
            bands[b].append(r[key] - r["truth"])
    return bands


def summarise_z(rows, alive_fracs):
    methods = ("hold", "cv", "box", "fuse")
    out = {"n_eval": len(rows), "box_valid_frac": (
        sum(1 for r in rows if r["box_ok"]) / len(rows) if rows else None),
        "alive_frac_median": med(alive_fracs)}
    tab = {}
    for m in methods:
        errs = [abs(r[m] - r["truth"]) for r in rows if r.get(m) is not None]
        bias = [r[m] - r["truth"] for r in rows if r.get(m) is not None]
        tab[m] = {"n": len(errs), "mae_med": med(errs),
                  "mae_p90": pct(errs, 0.9), "bias_med": med(bias)}
    out["methods"] = tab
    valid = [r for r in rows if r["box_ok"]]
    out["valid_only"] = {
        "n": len(valid),
        **{m: med([abs(r[m] - r["truth"]) for r in valid
                   if r.get(m) is not None]) for m in methods}}
    out["by_band"] = {k: {m: med([abs(e) for e in v]) if v else None
                          for m, v in
                          ((m, by_band(rows, m)[k]) for m in methods)}
                      for k in ("0-20", "20-40", "40-80", "80+")}
    out["by_gap"] = {}
    for lo, hi, name in ((0.12, 0.2, "0.12-0.2s"),
                         (0.2, 0.35, "0.2-0.35s"),
                         (0.35, 9.9, ">0.35s")):
        sub = [r for r in rows if lo <= r["dt"] < hi]
        out["by_gap"][name] = {
            "n": len(sub),
            **{m: med([abs(r[m] - r["truth"]) for r in sub
                       if r.get(m) is not None]) for m in methods}}
    out["by_plausible"] = {}
    for flag, name in ((True, "物理可能"), (False, "跳变被门挡/换目标")):
        sub = [r for r in rows if r["plausible"] is flag]
        out["by_plausible"][name] = {
            "n": len(sub),
            "h_step_med": med([r["h_step"] for r in sub]),
            **{m: med([abs(r[m] - r["truth"]) for r in sub
                       if r.get(m) is not None]) for m in methods}}
    out["by_kind"] = {}
    for kind in sorted({r["kind"] for r in rows}):
        sub = [r for r in rows if r["kind"] == kind]
        out["by_kind"][kind] = {
            m: {"mae_med": med([abs(r[m] - r["truth"]) for r in sub
                                if r.get(m) is not None]),
                "n": sum(1 for r in sub if r.get(m) is not None)}
            for m in methods}
    return out


def road_center_at(bands, z):
    pts = []
    for b in bands or []:
        if len(b) < 4 or b[2] is None or b[3] is None:
            continue
        pts.append((0.5 * (b[0] + b[1]), 0.5 * (b[2] + b[3])))
    if not pts:
        return None
    pts.sort()
    if z <= pts[0][0]:
        return pts[0][1]
    if z >= pts[-1][0]:
        return pts[-1][1]
    for (z0, c0), (z1, c1) in zip(pts, pts[1:]):
        if z0 <= z <= z1:
            f = (z - z0) / (z1 - z0) if z1 > z0 else 0.0
            return c0 + f * (c1 - c0)
    return None


def heading_of(bands, lo, hi):
    pts = []
    for b in bands or []:
        if len(b) < 4 or b[2] is None or b[3] is None:
            continue
        zc = 0.5 * (b[0] + b[1])
        if lo <= zc <= hi:
            pts.append((zc, 0.5 * (b[2] + b[3])))
    if len(pts) < 3:
        return None
    n = len(pts)
    mz = sum(p[0] for p in pts) / n
    mc = sum(p[1] for p in pts) / n
    num = sum((p[0] - mz) * (p[1] - mc) for p in pts)
    den = sum((p[0] - mz) ** 2 for p in pts)
    return num / den if den > 0 else None


def analyse_heading(rows):
    near, mid, curv = [], [], []
    coverage = 0
    car_rows = 0
    prev = None
    jitter = []
    for r in rows:
        bands = ((r.get("scene") or {}).get("road") or {}).get("bands") or []
        if not bands:
            continue
        car_rows += 1
        hn = heading_of(bands, *NEAR_BAND_M)
        hm = heading_of(bands, *MID_BAND_M)
        if hn is not None:
            coverage += 1
            near.append(hn)
            if prev is not None and abs(r["ts_ns"] - prev[0]) <= 2e8:
                jitter.append(abs(hn - prev[1]))
            prev = (r["ts_ns"], hn)
        if hm is not None:
            mid.append(hm)
            if hn is not None:
                curv.append(hm - hn)
    return {
        "rows_with_bands": car_rows,
        "coverage": coverage / car_rows if car_rows else None,
        "near_slope_med": med(near),
        "near_slope_abs_p90": pct([abs(v) for v in near], 0.9),
        "mid_slope_med": med(mid),
        "curvature_diff_p90": pct([abs(v) for v in curv], 0.9),
        "jitter_p95": pct(jitter, 0.95),
        "jitter_p95_deg": (None if not jitter else
                           math.degrees(pct(jitter, 0.95))),
        "n_near": len(near), "n_mid": len(mid),
    }


def box_series(ticks):
    """逐拍框高桥接 z（锚点起步、框高翻跳即失效、下一锚重基）；None=沿用发布 z。"""
    anchors = find_anchors(ticks)
    out = [None] * len(ticks)
    ai = 0
    a = None
    alive = True
    prev_h = None
    for i, tk in enumerate(ticks):
        while ai < len(anchors) and anchors[ai].idx <= i:
            a = anchors[ai]
            ai += 1
            alive = True
            prev_h = a.h
        if a is None or tk.z is None or a.h is None or tk.h is None:
            continue
        if tk.seen == 0:
            if prev_h and abs(tk.h - prev_h) / max(prev_h, 1.0) > BOX_JUMP:
                alive = False
            prev_h = tk.h
        if not alive:
            continue
        z = a.z * a.h / tk.h
        if 0.4 * a.z <= z <= 2.0 * a.z:
            out[i] = z
    return out


def lateral_variant(tracks, rows_by_fid, use_center, use_box=False):
    """横向锁道统计；use_center=True 走路心系，False 走相机系（ego 轴=0 道）。"""
    avail = defaultdict(lambda: [0, 0])   # band -> [z ticks, x valid]
    jumps = []
    lat_rates = []
    raw_rates = []
    lock_rates = []
    lock_counts = []
    lead_agree = {b: [0, 0] for b in LAT_BANDS}   # band -> [agree, n]
    leads = []
    n_tracks = 0
    for tr in tracks.values():
        ticks = [t for t in tr.ticks if t.z is not None]
        if len(ticks) < 8:
            continue
        zs = box_series(tr.ticks) if use_box else None
        if use_box:
            idx = {id(t): i for i, t in enumerate(tr.ticks)}
            zs = [zs[idx[id(t)]] for t in ticks]
        n_tracks += 1
        span = max(ticks[-1].t - ticks[0].t, 1e-6)
        prev_x = None
        prev_t = None
        rels = []      # (t, z, rel_lane or None)
        for k, tk in enumerate(ticks):
            z_eff = (zs[k] if use_box and zs[k] is not None else tk.z)
            x_eff = tk.x
            if use_box and zs[k] is not None and tk.x is not None \
                    and tk.z:
                x_eff = tk.x * z_eff / tk.z
            if x_eff is not None and abs(x_eff) > X_DOMAIN_M:
                x_eff = None      # 路面外：锁道统计不采样
            band = ("0-20" if z_eff < 20 else "20-40" if z_eff < 40
                    else "40-80" if z_eff < 80 else "80+")
            avail[band][0] += 1
            center = 0.0
            if use_center:
                bands = ((rows_by_fid.get(tk.fid) or {}).get("scene") or {}) \
                    .get("road", {}).get("bands") or []
                center = road_center_at(bands, z_eff)
            rel = (None if x_eff is None or center is None
                   else (x_eff - center) / LANE_W_M)
            if rel is not None:
                avail[band][1] += 1
            if x_eff is not None:
                if prev_x is not None and prev_t is not None \
                        and 0.0 < tk.t - prev_t <= 0.2:
                    d = abs(x_eff - prev_x)
                    jumps.append(d)
                    lat_rates.append(d / (tk.t - prev_t))
                prev_x, prev_t = x_eff, tk.t
            rels.append((tk.t, z_eff, rel))
        raw = [r for _, _, r in rels if r is not None]
        raw_flips = sum(1 for a, b in zip(raw, raw[1:])
                        if round(a) != round(b))
        raw_rates.append(raw_flips / span * 10.0)
        # 滞回锁道
        lock = None
        cand = 0
        lock_flips = 0
        locks = []
        for t, z, rel in rels:
            if rel is None:
                locks.append(lock)
                continue
            if lock is None:
                lock = round(rel)
            elif abs(rel - lock) > LOCK_BAND:
                cand += 1
                if cand >= LOCK_STAY:
                    if round(rel) != lock:
                        lock_flips += 1
                    lock = round(rel)
                    cand = 0
            else:
                cand = 0
            locks.append(lock)
        lock_rates.append(lock_flips / span * 10.0)
        lock_counts.append(lock_flips)
        final = None
        for (t, z, rel), lk in zip(rels, locks):
            if lk is not None and z is not None and z < 15.0:
                final = lk
        if final is None:
            continue
        for band in LAT_BANDS:
            cross = next(((t, z) for (t, z, _) in rels
                          if z is not None and z <= band), None)
            if cross is None:
                continue
            lk = next((lk for (t, _, _), lk in zip(rels, locks)
                       if t >= cross[0] and lk is not None), None)
            if lk is None:
                continue
            lead_agree[band][1] += 1
            if lk == final:
                lead_agree[band][0] += 1
        # 锁道提前量：跨 15 m 那一刻往前，锁值最后一次连续等于最终车道的起点
        cross15 = next((t for (t, z, _) in rels
                        if z is not None and z <= 15.0), None)
        if cross15 is None:
            continue
        t_read = None
        for (t, z, _), lk in reversed(list(zip(rels, locks))):
            if t > cross15 or lk is None:
                continue
            if lk == final:
                t_read = t
            else:
                break
        if t_read is not None and t_read < cross15:
            leads.append(cross15 - t_read)
    return {
        "n_tracks": n_tracks,
        "jump_p95_m": pct(jumps, 0.95),
        "jump_p99_m": pct(jumps, 0.99),
        "flips_raw_per_track_10s": med(raw_rates),
        "flips_lock_per_track_10s": med(lock_rates),
        "flips_lock_med_count": med(lock_counts),
        "lat_rate_p95_mps": pct(lat_rates, 0.95),
        "fast5_share": (sum(1 for v in lat_rates if v > 5.0) / len(lat_rates)
                        if lat_rates else None),
        "fast10_share": (sum(1 for v in lat_rates if v > 10.0) / len(lat_rates)
                         if lat_rates else None),
        "availability": {b: (v[1] / v[0] if v[0] else None)
                         for b, v in avail.items()},
        "avail_n": {b: dict(zip(("z_ticks", "x_valid"), v))
                    for b, v in avail.items()},
        "lock_agree": {f"{b:.0f}m": (v[0] / v[1] if v[1] else None)
                       for b, v in lead_agree.items()},
        "lock_agree_n": {f"{b:.0f}m": v[1] for b, v in lead_agree.items()},
        "lock_lead_s_med": med(leads),
        "lock_lead_s_p25": pct(leads, 0.25),
        "lock_lead_s_p75": pct(leads, 0.75),
        "lock_lead_n": len(leads),
    }


def analyse_lateral(tracks, rows_by_fid):
    return {
        "pub_cam": lateral_variant(tracks, rows_by_fid, False, False),
        "pub_road": lateral_variant(tracks, rows_by_fid, True, False),
        "box_cam": lateral_variant(tracks, rows_by_fid, False, True),
        "box_road": lateral_variant(tracks, rows_by_fid, True, True),
    }


def analyse_pairs(tracks):
    """锚对诊断：真实测量间隔下的隐含闭速 + 框高比与锚点的分歧。"""
    speeds = []
    resid = []
    disagree_dz = []
    agree_dz = []
    for tr in tracks.values():
        anchors = find_anchors(tr.ticks)
        for a, b in zip(anchors, anchors[1:]):
            if a.age_ms is None or b.age_ms is None:
                continue
            dt = (b.t - b.age_ms / 1000.0) - (a.t - a.age_ms / 1000.0)
            if not (0.0 < dt <= MAX_INTERVAL_S):
                continue
            dz = b.z - a.z
            speeds.append(abs(dz) / dt)
            if not a.h or not b.h:
                continue
            pred = a.z * a.h / b.h
            rel = abs(b.z - pred) / max(b.z, 1.0)
            resid.append(rel)
            if rel > 0.3:
                disagree_dz.append(abs(dz))
            else:
                agree_dz.append(abs(dz))
    return {
        "n": len(speeds),
        "speed_p50_mps": med(speeds),
        "speed_p90_mps": pct(speeds, 0.9),
        "speed_p95_mps": pct(speeds, 0.95),
        "speed_gt45_share": (sum(1 for v in speeds if v > 45.0) / len(speeds)
                             if speeds else None),
        "speed_gt80_share": (sum(1 for v in speeds if v > 80.0) / len(speeds)
                             if speeds else None),
        "box_resid_p50": med(resid),
        "box_resid_p90": pct(resid, 0.9),
        "disagree_share": (sum(1 for r in resid if r > 0.3) / len(resid)
                           if resid else None),
        "disagree_dz_p50_m": med(disagree_dz),
        "agree_dz_p50_m": med(agree_dz),
    }


def analyse_staleness(rows):
    ages = []
    stale_m = []
    for r in rows:
        for m in (r.get("mcars") or []):
            a = m.get("age")
            if a is None:
                continue
            ages.append(a)
            if m.get("v"):
                stale_m.append(a / 1000.0 * abs(m["v"]))
    return {
        "n": len(ages),
        "age_p50_ms": med(ages),
        "age_p90_ms": pct(ages, 0.9),
        "age_gt300_share": (sum(1 for a in ages if a > 300.0) / len(ages)
                            if ages else None),
        "stale_dist_p50_m": med(stale_m),
        "stale_dist_p90_m": pct(stale_m, 0.9),
    }


def run_session(path):
    rows = load_rows(path)
    if not rows:
        return None
    tracks, dur = build_tracks(rows)
    rows_by_fid = {r.get("fid"): r for r in rows}
    z_rows, alive, noise, n_iv = analyse_z(tracks)
    return {
        "trace": os.path.basename(path),
        "rows": len(rows),
        "duration_s": rnd(dur, 1),
        "n_tracks": len(tracks),
        "anchor_intervals": n_iv,
        "anchor_noise": {
            "dt_med_s": med([d for d, _ in noise]),
            "dz_med_m": med([z for _, z in noise]),
            "dz_p90_m": pct([z for _, z in noise], 0.9),
            "dz_at_short_dt_med_m": med([z for d, z in noise if d <= 0.1]),
        },
        "z": summarise_z(z_rows, alive),
        "pairs": analyse_pairs(tracks),
        "staleness": analyse_staleness(rows),
        "heading": analyse_heading(rows),
        "lateral": analyse_lateral(tracks, rows_by_fid),
    }


def pool(sessions):
    """会话级汇总：按会话等权，不把长局样本当重复观测池化。"""
    def agg(key_path):
        vals = []
        for s in sessions:
            cur = s
            for k in key_path:
                cur = (cur or {}).get(k) if isinstance(cur, dict) else None
            if cur is not None:
                vals.append(cur)
        return med(vals)
    return {
        "sessions": len(sessions),
        "z_mae_med": {m: agg(("z", "methods", m, "mae_med"))
                      for m in ("hold", "cv", "box", "fuse")},
        "z_mae_p90": {m: agg(("z", "methods", m, "mae_p90"))
                      for m in ("hold", "cv", "box", "fuse")},
        "box_valid_frac": agg(("z", "box_valid_frac")),
        "heading_coverage": agg(("heading", "coverage")),
        "heading_jitter_deg_p95": agg(("heading", "jitter_p95_deg")),
        "lateral_avail_20_40": agg(("lateral", "pub_road",
                                    "availability", "20-40")),
        "lateral_avail_40_80": agg(("lateral", "pub_road",
                                    "availability", "40-80")),
        "lateral_jump_p95_m": agg(("lateral", "pub_road", "jump_p95_m")),
        "lock_flips_per_track_10s": agg(
            ("lateral", "pub_road", "flips_lock_per_track_10s")),
        "lock_flips_cam_per_track_10s": agg(
            ("lateral", "pub_cam", "flips_lock_per_track_10s")),
        "lock_agree_20m": agg(("lateral", "pub_road", "lock_agree", "20m")),
        "lock_agree_15m": agg(("lateral", "pub_road", "lock_agree", "15m")),
        "lock_lead_s_med": agg(("lateral", "pub_road", "lock_lead_s_med")),
        "bridge_lock_flips_cam": agg(
            ("lateral", "box_cam", "flips_lock_per_track_10s")),
        "bridge_lock_flips_road": agg(
            ("lateral", "box_road", "flips_lock_per_track_10s")),
        "bridge_jump_p95_m": agg(("lateral", "box_road", "jump_p95_m")),
        "bridge_agree_20m": agg(("lateral", "box_road", "lock_agree", "20m")),
        "bridge_lead_s_med": agg(("lateral", "box_road", "lock_lead_s_med")),
    }


def print_report(sessions, pooled):
    print("\n== 会话 ==")
    for s in sessions:
        z = s["z"]
        print(f"\n{s['trace']}  {s['duration_s']}s  tracks={s['n_tracks']}  "
              f"锚间隔={s['anchor_intervals']}  "
              f"锚间dz p50={rnd(s['anchor_noise']['dz_med_m'], 2)}m "
              f"p90={rnd(s['anchor_noise']['dz_p90_m'], 2)}m")
        print("  z 方法     n    mae_p50  mae_p90  bias_p50  框有效")
        for m in ("hold", "cv", "box", "fuse"):
            v = z["methods"][m]
            print(f"  {m:<8} {v['n']:>4}  "
                  f"{rnd(v['mae_med'], 2)!s:>7}  {rnd(v['mae_p90'], 2)!s:>7}  "
                  f"{rnd(v['bias_med'], 2)!s:>8}  "
                  f"{rnd(z['box_valid_frac'], 2) if m == 'box' else ''}")
        print("  分距离带 mae_p50: " + "  ".join(
            f"{b}: {rnd(z['by_band'][b]['fuse'], 2)}"
            for b in ("0-20", "20-40", "40-80", "80+")))
        print("  分锚间隔 mae_p50(hold/fuse): " + "  ".join(
            f"{g}: {rnd(v['hold'], 2)}/{rnd(v['fuse'], 2)} (n={v['n']})"
            for g, v in z["by_gap"].items()))
        vo = z["valid_only"]
        print("  框有效子集 mae_p50: " + "  ".join(
            f"{m}={rnd(vo[m], 2)}" for m in ("hold", "cv", "box", "fuse")
            ) + f"  (n={vo['n']})")
        for name, v in z["by_plausible"].items():
            print(f"  {name}(n={v['n']}, 框高单拍步进p50={rnd(v['h_step_med'], 2)}): "
                  + "  ".join(f"{m}={rnd(v[m], 2)}"
                              for m in ("hold", "cv", "box", "fuse")))
        h = s["heading"]
        st = s["staleness"]
        print(f"  锚龄: p50={rnd(st['age_p50_ms'], 0)}ms "
              f"p90={rnd(st['age_p90_ms'], 0)}ms "
              f">300ms={rnd(st['age_gt300_share'], 2)}  "
              f"陈旧距离 p50={rnd(st['stale_dist_p50_m'], 1)}m "
              f"p90={rnd(st['stale_dist_p90_m'], 1)}m")
        pr = s["pairs"]
        print(f"  锚对: 隐含闭速 p50={rnd(pr['speed_p50_mps'], 0)} "
              f"p90={rnd(pr['speed_p90_mps'], 0)} "
              f">45m/s={rnd(pr['speed_gt45_share'], 2)} "
              f">80m/s={rnd(pr['speed_gt80_share'], 2)}  "
              f"框高比分歧>30%={rnd(pr['disagree_share'], 2)} "
              f"(分歧组dz中位 {rnd(pr['disagree_dz_p50_m'], 1)}m / "
              f"一致组 {rnd(pr['agree_dz_p50_m'], 1)}m)")
        print(f"  切线: 覆盖={rnd(h['coverage'], 2)}  "
              f"近带斜率p50={rnd(h['near_slope_med'], 4)}  "
              f"逐拍抖动p95={rnd(h['jitter_p95_deg'], 2)}°")
        l = s["lateral"]
        print("  横向        20-40可得 40-80可得 跳变p95 横速p95 >5m/s占比 锁翻转/10s 一致@20m 提前p50")
        for name in ("pub_road", "pub_cam", "box_road", "box_cam"):
            v = l[name]
            print(f"  {name:<10}  "
                  f"{rnd(v['availability']['20-40'], 2)!s:>8}  "
                  f"{rnd(v['availability']['40-80'], 2)!s:>8}  "
                  f"{rnd(v['jump_p95_m'], 2)!s:>6}  "
                  f"{rnd(v['lat_rate_p95_mps'], 1)!s:>6}  "
                  f"{rnd(v['fast5_share'], 2)!s:>8}  "
                  f"{rnd(v['flips_lock_per_track_10s'], 2)!s:>9}  "
                  f"{rnd(v['lock_agree']['20m'], 2)!s:>7}  "
                  f"{rnd(v['lock_lead_s_med'], 2)!s:>6}")
    print("\n== 汇总（会话中位） ==")
    for k, v in pooled.items():
        print(f"  {k}: {rnd(v, 3) if isinstance(v, (int, float)) else v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", default=[],
                    help="trace_*.jsonl，可重复")
    ap.add_argument("--latest", type=int, default=4,
                    help="无 --trace 时取最新的 N 个会话")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    paths = args.trace
    if not paths:
        paths = sorted(glob.glob(os.path.join(DATA_ROOT, "*", "trace_*.jsonl")),
                       key=os.path.getmtime)[-args.latest:]
    sessions = [s for s in (run_session(p) for p in paths) if s]
    pooled = pool(sessions)
    print_report(sessions, pooled)
    out_dir = args.out or os.path.join(
        OUT_ROOT, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "state_est.json")
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "traces": paths, "pooled": pooled,
                   "sessions": sessions}, fh, ensure_ascii=False, indent=1)
    print(f"\n→ {out_file}")


if __name__ == "__main__":
    main()
