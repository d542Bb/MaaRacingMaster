#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探针：两路 1 秒位移预测（p1m 模型 / p1h 朝向）分四类切面复核。

生产 trace 的 `lat_ctrl` 里已有 `p1m`（模型保持当前杆值的 1 秒积分）、
`p1h`（朝向保持当前朝向 × 在线比例的 1 秒位移）、`psi`/`r`/`vsp`/`scf`。
本脚本把它们与「下一拍起的实际 1 秒位移」对齐，按四类切面比较：
直道 / 建立朝向 / 回正 / 弯道。

真值两条（互相校验，分歧大的窗丢弃）：
- `road_offset`：深度路心相对横位，跨越换道仍成立；只用两端都不是保持值的拍。
- 同 k 标线 ΔL：`x = k − L`，故 Δx = L_i − L_j；跨线时 k 集合断开，用不上。

用法：
    python probe_shadow_compare.py            # 只跑含影子列的会话
    python probe_shadow_compare.py --root D:/data/MaaRacingMaster
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path

LW = 3.5               # 米制道宽（与 lane_obs 同值）
WIN_S = 1.0            # 预测/真值窗长
SPD_TAU = 20.0         # 比例泄漏记忆（与产线同值）
SPD_DEN_MIN = 0.03
SPD_DEN_FULL = 0.10
SPD_MIN_TAN = 0.02
SPD_MIN_V = 0.05
PSI_TAU = 2.0
PSI_MIN_LINES = 2


def replay_heading(rows: list[dict]) -> list[dict]:
    """按修复后的口径离线重放 ψ / r / 在线比例（不依赖 trace 里的影子列）。

    修复前（20261030_181838）线横位整道跳会把朝向观测整体重置，比例被反复
    清零、`p1h` 只有 9% 的拍有值。本函数按「线帧重置不动朝向」重算一遍，
    用于在同一局上做 A/B。v_line 仍取 trace 记录值（线帧重置照旧清它）。"""
    out = []
    base = None
    seen_t = -1e9
    psi_prev = None
    psi_prev_t = -1e9
    r = None
    r_t = -1e9
    psi = None
    psi_t = -1e9
    num = den = 0.0
    t = 0.0
    last_fid = None
    for row in rows:
        dt = float(row.get("dt") or 0.05)
        t += dt
        decay = math.exp(-dt / SPD_TAU)
        num *= decay
        den *= decay
        if psi is not None and t - psi_t > 0.6:
            psi = None
        if r is not None and t - r_t > 0.3:
            r = None
        lo = row.get("lane_obs") or {}
        th = lo.get("theta")
        fid = lo.get("det_fid")
        nl = int(lo.get("n_locked") or 0)
        fresh = (th is not None and lo.get("state") == "TRACK"
                 and nl >= PSI_MIN_LINES
                 and (fid is None or fid != last_fid))
        if fresh:
            last_fid = fid
            if base is None:
                base = th
            else:
                gap = min(max(t - seen_t, 1e-3), 0.5)
                base += (1.0 - math.exp(-gap / PSI_TAU)) * (th - base)
            seen_t = t
            rel = max(-0.6, min(0.6, th - base))
            if psi is not None and t - psi_prev_t > 1e-3:
                gap = t - psi_prev_t
                rate = (rel - psi) / gap
                if r is None or gap > 0.3:
                    r = rate
                else:
                    r += (1.0 - math.exp(-gap / 0.15)) * (rate - r)
                r_t = t
            psi_prev_t = t
            psi = rel
            psi_t = t
            v = (row.get("lat_ctrl") or {}).get("v_line")
            tan = math.tan(rel)
            if v is not None and abs(tan) >= SPD_MIN_TAN \
                    and abs(v) >= SPD_MIN_V:
                num += tan * v
                den += tan * tan
        vsp = None
        conf = 0.0
        if den >= SPD_DEN_MIN:
            conf = min(1.0, den / SPD_DEN_FULL)
            sp = LW * num / den
            vsp = sp if 5.0 <= sp <= 120.0 else None
        p1h = (None if psi is None or vsp is None
               else vsp * math.tan(psi) / LW)
        out.append({"psi": psi, "r": r, "vsp": vsp, "scf": conf,
                    "p1h": p1h})
    return out



FD_JUMP = 0.3
AGREE_LANE = 0.3      # 两条真值允许的分歧（道）
CURVE_SLOPE = 0.10    # 弯道判据：路心随纵深的横移率（m/m ≈ 5.7°）


def rows_of(trace: Path) -> list[dict]:
    out = []
    with open(trace, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("lat_ctrl") and "psi" in r["lat_ctrl"]:
                out.append(r)
    return out


def lines_at(r: dict) -> dict[int, float]:
    lo = r.get("lane_obs") or {}
    return {int(c["k"]): float(c["L"]) for c in (lo.get("lines") or [])
            if c.get("accepted") and c.get("k") is not None}


def line_disp(a: dict, b: dict) -> float | None:
    la, lb = lines_at(a), lines_at(b)
    common = [k for k in la if k in lb]
    if not common:
        return None
    d = [la[k] - lb[k] for k in common]
    if len(d) >= 2 and max(d) - min(d) > 0.4:
        return None
    return statistics.median(d)


def ro(r: dict) -> float | None:
    """深度路心相对横位（道）；保持值（历史/断供）不是本拍测量，不用。"""
    if r.get("ro_hold"):
        return None
    v = r.get("road_offset")
    return None if v is None else float(v)


def curvature(rows: list[dict], i: int) -> float | None:
    """路心随纵深的横移率（m/m）：带状走廊近端与远端中心差 ÷ 纵深差。"""
    road = (rows[i].get("scene") or {}).get("road") or {}
    bands = [b for b in (road.get("bands") or [])
             if b[2] is not None and b[3] is not None]
    if len(bands) < 2:
        return None
    bands.sort(key=lambda b: b[0])
    near, far = bands[0], bands[-1]
    dz = abs(far[0] - near[0])
    if dz < 1e-3:
        return None
    return ((far[2] + far[3]) - (near[2] + near[3])) / 2.0 / dz


def x_of(r: dict) -> float | None:
    """融合线横位（产线值，k−L 的中位）：跨换道仍连续，但重锚时会跳。"""
    lo = r.get("lane_obs") or {}
    return lo.get("x") if lo.get("state") == "TRACK" else None


def x_disp(rows: list[dict], i: int, j: int) -> float | None:
    """融合线横位的 1 秒位移；窗内出现 RELOCK 或单拍跳 >0.5 道即作废。"""
    for k in range(i, j + 1):
        if (rows[k].get("lane_obs") or {}).get("state") == "RELOCK":
            return None
    for k in range(i + 1, j + 1):
        a, b = x_of(rows[k - 1]), x_of(rows[k])
        if a is not None and b is not None and abs(b - a) > 0.5:
            return None
    a, b = x_of(rows[i]), x_of(rows[j])
    if a is None or b is None or abs(b - a) > 0.9:
        return None
    return b - a


def windows(rows: list[dict], replay: list[dict] | None = None) -> list[dict]:
    t = [0.0]
    for r in rows:
        t.append(t[-1] + float(r.get("dt") or 0.05))
    out = []
    n = len(rows)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and t[j + 1] < t[i] + WIN_S:
            j += 1
        if t[j] < t[i] + WIN_S * 0.85:
            break
        if t[j] - t[i] > WIN_S * 1.3:
            i += 1
            continue
        fd0 = (rows[i].get("plan") or {}).get("fd")
        fd1 = (rows[j].get("plan") or {}).get("fd")
        # 只判窗两端的帧偏移：本局 plan:frame_shift 占三成，窗内偶尔一拍不能
        # 否定整段位移（真值另有线/ro 互证把关）。
        if not (fd0 is not None and fd1 is not None
                and abs(fd1 - fd0) > FD_JUMP):
            a, b = ro(rows[i]), ro(rows[j])
            d_ro = None if a is None or b is None else b - a
            d_line = line_disp(rows[i], rows[j])
            d_x = x_disp(rows, i, j)
            cands = [d for d in (d_ro, d_line, d_x) if d is not None]
            truth = None
            if cands:
                med = statistics.median(cands)
                if all(abs(d - med) <= AGREE_LANE for d in cands):
                    truth = med          # 多条真值互证；打架的窗丢弃
            lc = replay[i] if replay is not None else rows[i]["lat_ctrl"]
            if truth is not None and abs(truth) < 1.5:
                out.append({
                    "t": t[i], "truth": truth, "ro": d_ro, "line": d_line,
                    "x": d_x,
                    "p1m": rows[i]["lat_ctrl"].get("p1m"),
                    "p1h": lc.get("p1h"),
                    "psi": lc.get("psi"), "r": lc.get("r"),
                    "vsp": lc.get("vsp"), "scf": lc.get("scf"),
                    "curv": curvature(rows, i),
                    "merging": (rows[i].get("route") or {}).get("ph")
                    == "merging",
                })
        # 重叠窗：4 拍步进（~0.2s），否则 1 秒窗在 80 秒局里只有 40 个样本
        i += 4
    return out


def regime(q: dict) -> str:
    psi, r_ = q["psi"], q["r"]
    if q["curv"] is not None and abs(q["curv"]) >= CURVE_SLOPE:
        return "弯道"
    if psi is None or abs(psi) < 0.02:
        return "直道"
    if r_ is None:
        return "直道"
    return "建立朝向" if psi * r_ > 0 else "回正"


def stat(vals):
    vals = sorted(vals)
    return None if not vals else (statistics.median(vals),
                                  vals[int(len(vals) * 0.9)])


def report(name: str, wins: list[dict]) -> None:
    print("\n[%s] 1 秒窗 %d（并线中 %d，有 p1h 的 %d）"
          % (name, len(wins), sum(1 for q in wins if q["merging"]),
             sum(1 for q in wins if q["p1h"] is not None)))
    groups: dict[str, list[dict]] = {}
    for q in wins:
        groups.setdefault(regime(q), []).append(q)
    for key in ("直道", "建立朝向", "回正", "弯道"):
        sub = groups.get(key) or []
        if len(sub) < 8:
            print("  %-6s n=%3d（不足）" % (key, len(sub)))
            continue
        mag, _ = stat([abs(q["truth"]) for q in sub])
        row = ["  %-6s n=%3d 实测|Δ|中位 %.2f道" % (key, len(sub), mag)]
        for lab, f in (("p1m", lambda q: q["p1m"]),
                       ("p1h", lambda q: q["p1h"])):
            vals = [(q, f(q)) for q in sub if f(q) is not None]
            if len(vals) < 8:
                row.append("%s 不足" % lab)
                continue
            st = stat([abs(p - q["truth"]) for q, p in vals])
            same = sum(1 for q, p in vals
                       if abs(p) > 0.05 and (p > 0) == (q["truth"] > 0))
            nz = sum(1 for q, p in vals if abs(p) > 0.05)
            row.append("%s 中位 %.2f p90 %.2f 同号 %d%%"
                       % (lab, st[0], st[1],
                          100 * same / max(1, nz)))
        print(" | ".join(row))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    ap.add_argument("--replay", action="store_true",
                    help="按修复后口径离线重放 ψ/r/比例（用于修复前的会话）")
    args = ap.parse_args()
    root = Path(args.root) / "logs"
    runs = sorted(d.name for d in root.iterdir()
                  if d.is_dir() and d.name[0].isdigit()
                  and (d / "trace" / "speedrush.jsonl").is_file())
    allw = []
    for run in runs:
        rows = rows_of(root / run / "trace" / "speedrush.jsonl")
        if not rows:
            continue
        rep = replay_heading(rows) if args.replay else None
        w = windows(rows, rep)
        if w:
            report(run + ("(重放)" if args.replay else ""), w)
            allw.extend(w)
    if not allw:
        print("没有含朝向影子列（lat_ctrl.psi）的会话——需要先跑一局新版本。")
        return
    print("\n" + "=" * 70)
    report("合计", allw)


if __name__ == "__main__":
    main()
