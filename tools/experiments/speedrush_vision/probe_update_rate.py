# -*- coding: utf-8 -*-
"""控制更新率下限探针：全油门+转向控制最少需要外部数据多快更新（第 0 步，定深度预算）。

问题：不考虑控制链路设计，全油门+转向要控得住，感知（外部数据）最低多少 Hz 更新？
结论用来定调深度模型的最大单帧预算（预算 = 拍周期 − 感知以外成本）。

证据两路（全部来自 demos 落盘，不跑模型；路缘检测已裁掉——不可信）：
A. pads.jsonl（200Hz 手柄，lx 为 ±32768 int16 原始轴值需归一化）：转向输入
   自身的节奏——事件时长、间隔、起手斜率。这是"输入侧"参照。
B. tracking_scan.jsonl（42 会话 YOLO 车框）：只取**高可信片段**——完整接近
   事件（min h≤120px → max h≥250px、时长≥1s），关联约束：单帧跳变 ≤0.3×箱宽
   （串车即弃）、高度近单调增（允许 10% 回缩）、歧义车共存帧 ≤2。
   尺度锚：同帧双车 |Δcx| 分布 = 近场车道横向分离尺度（决策分辨的最小单位）。
   陈旧容差取该尺度的 1/4。另抽最可信片段逐帧表落盘供目检。

用法：python tools/experiments/speedrush_vision/probe_update_rate.py
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

import numpy as np

DATA = Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush"
OUT = DATA / "vision_review"

STEER_TH = 0.15   # 归一化后的 engaged 阈值
GAP_MS = 60.0
FAR_H = 120       # 入场箱高上限
NEAR_H = 250      # 贴身箱高下限
TRACK_GAP = 0.25  # 关联断档上限 (s)
MIN_DUR = 1.0     # 片段最短时长 (s)
TOL_FRAC = 0.25   # 陈旧容差 = 车道分离尺度 × 1/4


def _pct(xs, q):
    xs = [x for x in xs if x == x]
    return float(np.percentile(xs, q)) if xs else float("nan")


# ------------------------------------------------ A. 转向输入节奏（200Hz）
def pads_stats(sess_dir: Path):
    engaged, events, ev, slews = 0, [], [], []
    prev, total = None, 0
    for line in open(sess_dir / "pads.jsonl", encoding="utf-8"):
        d = json.loads(line)
        t = d["ts_ns"] / 1e6
        lx = d["lx"] / 32768.0
        total += 1
        if prev is not None and t > prev[0]:
            slews.append(abs(lx - prev[1]) / (t - prev[0]) * 1000)
        prev = (t, lx)
        if abs(lx) > STEER_TH:
            engaged += 1
            if ev and t - ev[-1] > GAP_MS:
                events.append((ev[0], ev[-1]))
                ev = []
            if not ev:
                ev.append(t)
            ev.append(t)
    if ev:
        events.append((ev[0], ev[-1]))
    durs = [(b - a) for a, b in events]
    gaps = [events[i + 1][0] - events[i][1] for i in range(len(events) - 1)]
    return {
        "engaged_frac": engaged / max(total, 1),
        "n_events": len(events),
        "dur_p50": _pct(durs, 50), "dur_p90": _pct(durs, 90),
        "gap_p50": _pct(gaps, 50),
        "slew_p95": _pct(slews, 95),
    }


# ------------------------------------------------ B. YOLO 目标动力学
def extract_tracks(rows):
    """rows: [(ts_s, cars[])]；返回高可信接近片段（见模块 docstring）。"""
    active, done = [], []
    for ts_s, cars in rows:
        cars = sorted(cars, key=lambda c: -c[3])
        for c in cars:
            x, y, w, h = c[0], c[1], float(c[2]), float(c[3])
            cx = x + w / 2
            best, bi = None, None
            for i, a in enumerate(active):
                if ts_s - a["ts"][-1] > TRACK_GAP:
                    continue
                d = abs(cx - a["cx"][-1])
                if d <= 0.3 * max(w, a["w"][-1]) and h >= a["h"][-1] * 0.9:
                    if best is None or d < best:
                        best, bi = d, i
            if bi is None:
                active.append({"ts": [ts_s], "cx": [cx], "h": [h],
                               "w": [w], "amb": 0})
            else:
                a = active[bi]
                a["ts"].append(ts_s); a["cx"].append(cx)
                a["h"].append(h); a["w"].append(w)
        for a in active:
            h_now, w_now, cx_now = a["h"][-1], a["w"][-1], a["cx"][-1]
            for c in cars:
                x, y, w2, h2 = c[0], c[1], float(c[2]), float(c[3])
                cx2 = x + w2 / 2
                if abs(h2 - h_now) < 0.4 * h_now and 0 < abs(cx2 - cx_now) < 1.5 * w_now:
                    a["amb"] += 1
        still = []
        for a in active:
            if ts_s - a["ts"][-1] > TRACK_GAP:
                done.append(a)
            else:
                still.append(a)
        active = still
    done.extend(active)
    segs = []
    for a in done:
        h = a["h"]
        if len(h) < 5 or min(h) > FAR_H or max(h) < NEAR_H:
            continue
        if (a["ts"][-1] - a["ts"][0]) < MIN_DUR or a["amb"] > 2:
            continue
        segs.append(a)
    return segs


def seg_stats(a):
    ts = np.array(a["ts"]); cx = np.array(a["cx"])
    h = np.array(a["h"]); w = np.array(a["w"])
    m = h > NEAR_H * 0.7
    vx, tv = [], []
    for i in range(1, len(ts)):
        if m[i] and m[i - 1]:
            dt = ts[i] - ts[i - 1]
            if 0.02 < dt < 0.25:
                vx.append((cx[i] - cx[i - 1]) / dt)
                tv.append(ts[i])
    vx = np.array(vx); tv = np.array(tv)
    ax = np.abs(np.diff(vx) / np.diff(tv)) if len(vx) >= 3 else np.array([])
    # 决策窗：h 首次 ≥100 → 首次 ≥300
    def t_at(hq):
        idx = np.where(h >= hq)[0]
        return ts[idx[0]] if len(idx) else None
    t100, t300 = t_at(100), t_at(300)
    return {
        "dur": ts[-1] - ts[0],
        "n": len(ts),
        "vx_med": float(np.median(np.abs(vx))) if len(vx) else float("nan"),
        "vx_p95": _pct(np.abs(vx), 95) if len(vx) else float("nan"),
        "ax_p50": _pct(ax, 50) if len(ax) else float("nan"),
        "w_near": float(np.median(w[m])) if m.any() else float("nan"),
        "window": (t300 - t100) if (t100 is not None and t300 is not None) else float("nan"),
        "h": h, "cx": cx, "ts": ts,
    }


def main():
    # A
    sessions = sorted(p for p in (DATA / "demos").iterdir() if p.is_dir())
    pad_stats = []
    for s in sessions:
        if (s / "pads.jsonl").exists():
            st = pads_stats(s)
            st["sess"] = s.name
            pad_stats.append(st)
    print(f"[A] 转向输入节奏（{len(pad_stats)} 会话，200Hz，lx/32768 归一化）")
    print(f"  engaged 占比: p50={_pct([p['engaged_frac'] for p in pad_stats], 50):.2f}")
    print(f"  事件时长 ms: p50={_pct([p['dur_p50'] for p in pad_stats], 50):.0f} "
          f"p90={_pct([p['dur_p90'] for p in pad_stats], 90):.0f}")
    print(f"  事件间隔 ms: p50={_pct([p['gap_p50'] for p in pad_stats], 50):.0f}")
    print(f"  lx 斜率 1/s: p95={_pct([p['slew_p95'] for p in pad_stats], 95):.1f}")

    # B
    tr = collections.defaultdict(list)
    for line in open(DATA / "tracking_scan.jsonl", encoding="utf-8"):
        d = json.loads(line)
        tr[d["sess"]].append((d["ts_ns"] / 1e9, d["boxes"]["car"]))
    all_segs = []
    for sess, rows in sorted(tr.items()):
        rows.sort()
        all_segs.extend(seg_stats(a) for a in extract_tracks(rows))
    print(f"\n[B] 高可信接近片段：{len(all_segs)} 条 / {len(tr)} 会话")

    # 同帧双车横向分离尺度（近场两车 h 均>150）
    seps = []
    for sess, rows in sorted(tr.items()):
        for ts_s, cars in rows:
            big = [c for c in cars if c[3] > 150]
            for i in range(len(big)):
                for j in range(i + 1, len(big)):
                    ci = big[i][0] + big[i][2] / 2
                    cj = big[j][0] + big[j][2] / 2
                    seps.append(abs(ci - cj))
    sep_p50 = _pct(seps, 50)
    print(f"  同帧近场双车 |Δcx| px: n={len(seps)} p25={_pct(seps,25):.0f} "
          f"p50={sep_p50:.0f} p75={_pct(seps,75):.0f}")

    vx50 = [s["vx_med"] for s in all_segs if s["vx_med"] == s["vx_med"]]
    vx95 = [s["vx_p95"] for s in all_segs if s["vx_p95"] == s["vx_p95"]]
    ax50 = [s["ax_p50"] for s in all_segs if s["ax_p50"] == s["ax_p50"]]
    wins = [s["window"] for s in all_segs if s["window"] == s["window"] and s["window"] > 0]
    print(f"  近场横移 |vx|（事件中位）px/s: p50={_pct(vx50,50):.0f} p90={_pct(vx50,90):.0f} max={max(vx50):.0f}")
    print(f"  近场横移 |vx|（事件 p95）px/s:  p50={_pct(vx95,50):.0f} p90={_pct(vx95,90):.0f} max={max(vx95):.0f}")
    print(f"  近场横移加速度 |ax| px/s²: p50={_pct(ax50,50):.0f} p90={_pct(ax50,90):.0f}")
    print(f"  决策窗 h100→300 (s): p05={_pct(wins,5):.2f} p50={_pct(wins,50):.2f} min={min(wins):.2f} (n={len(wins)})")

    tol = sep_p50 * TOL_FRAC
    print(f"\n  陈旧容差（分离尺度 p50 × 1/4）= {tol:.0f}px")
    for tag, vxs in (("vx 事件p50 分布", vx50), ("vx 事件p95 分布", vx95)):
        stale = sorted(tol / v for v in vxs if v > 0)
        print(f"  按 {tag}: 可容陈旧 ms p05={_pct(stale,5):.0f} p50={_pct(stale,50):.0f} min={stale[0]:.0f}")
    # 加速度项：½·ax·Δt² 与横移项同量级时的 Δt
    a90 = _pct(ax50, 90)
    dt_eq = 2 * (_pct(vx95, 50) / 2) / a90 if a90 > 0 else float("nan")
    print(f"  加速度漂移=横移漂移的 Δt（vx={_pct(vx95,50):.0f}px/s, ax={a90:.0f}px/s²）: {dt_eq*1000:.0f} ms")
    print(f"  决策窗 p05={_pct(wins,5):.2f}s → 窗内 ≥3 次更新 ⇒ ≥{3/_pct(wins,5):.0f} Hz")

    # 最可信片段逐帧表（前 5 条按 vx 中位 + 时长）
    good = sorted([s for s in all_segs if s["vx_med"] == s["vx_med"]],
                  key=lambda s: -(s["dur"] * (s["vx_med"] > 0)))
    lines = []
    for s in good[:5]:
        lines.append(f"# dur={s['dur']:.2f}s vx_med={s['vx_med']:.0f} window={s['window']:.2f}s")
        for t, hh, cc in zip(s["ts"], s["h"], s["cx"]):
            lines.append(f"{t:.3f} {hh:.0f} {cc:.0f}")
        lines.append("")
    (OUT / "update_rate_tracks.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nsaved: {OUT / 'update_rate_summary.json'} / update_rate_tracks.txt")


if __name__ == "__main__":
    main()
