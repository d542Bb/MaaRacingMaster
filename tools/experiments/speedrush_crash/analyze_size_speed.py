# -*- coding: utf-8 -*-
"""离线影子：分析 60fps 采样产物（撞车复盘实验 6）。

输入 = probe_size_speed.py 的 jsonl。不跑模型、不接控制。

1) 尺寸—距离：car / coin / bonus 三类各自的 log(z)–log(h) 斜率、R²、
   残差倍数（实测 z / 曲线预测 z）分位——回答“框高能不能当距离标尺”。
2) 接近速度：单帧差分被深度噪声主导，改用 0.25s / 0.5s 滑窗回归：
   - 纵深斜率：v_z = −dz/dt
   - 框高斜率：v_h = −z_mid × d(log h)/dt（不依赖新锚点）
3) 跳变门模拟：按生产锚间节奏（dt 0.15~0.35s）取锚对，
   门限 = 2 + 45×dt；用 ±0.5s 长窗拟合的速度算“真运动应走的 Δz”，
   与实测 Δz 比较——分离“门杀了真运动”与“锚点自己跳”。
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np

FPS = 60.0
GATE_BASE_M = 2.0
GATE_MPS = 45.0


def _tracks(rows):
    """按最近邻 + 尺寸相近做逐帧关联（60fps 下位移很小）。"""
    by_frame = {}
    for r in rows:
        by_frame.setdefault(r["frame"], []).append(r)
    tracks = []
    active = {}
    for f in sorted(by_frame):
        cur = by_frame[f]
        used = set()
        for tid, tr in list(active.items()):
            last = tr[-1]
            best = None
            for k, r in enumerate(cur):
                if k in used or r["cls"] != last["cls"]:
                    continue
                if abs(r["cx"] - last["cx"]) > 30:
                    continue
                ratio = r["h"] / max(last["h"], 1)
                if not 0.6 < ratio < 1.6:
                    continue
                score = abs(r["cx"] - last["cx"]) + 20 * abs(math.log(ratio))
                if best is None or score < best[0]:
                    best = (score, k, r)
            if best is not None:
                used.add(best[1])
                tr.append(best[2])
            else:
                tracks.append(tr)
                del active[tid]
        for k, r in enumerate(cur):
            if k not in used:
                active[len(tracks) + k + 100000] = [r]
    tracks.extend(active.values())
    return [t for t in tracks if len(t) >= 5]


def _fit(rows, cls, min_h, feat="h", min_conf=0.4):
    xs, ys = [], []
    for r in rows:
        if r["cls"] != cls or not r.get("z") or r["h"] < min_h \
                or r["conf"] < min_conf:
            continue
        f = (r["h"] if feat == "h" else
             r["w"] if feat == "w" else r["w"] * r["h"])
        if f <= 0:
            continue
        xs.append(math.log(f))
        ys.append(math.log(r["z"]))
    if len(xs) < 20:
        return None
    X, Y = np.array(xs), np.array(ys)
    b, a = np.polyfit(X, Y, 1)
    pred = a + b * X
    ss_res = float(((Y - pred) ** 2).sum())
    ss_tot = float(((Y - Y.mean()) ** 2).sum()) or 1.0
    resid = np.exp(Y - pred)          # 实测 / 预测
    return {"slope": b, "r2": 1 - ss_res / ss_tot, "n": len(xs),
            "p50": float(np.percentile(resid, 50)),
            "p90": float(np.percentile(resid, 90)),
            "zmin": float(np.exp(Y).min()), "zmax": float(np.exp(Y).max()),
            "fmin": float(np.exp(X).min()), "fmax": float(np.exp(X).max())}


def _win_speed(win):
    """0.25/0.5s 窗口内 z 与 log h 的回归斜率（正值=接近）。

    z ∝ 1/h ⇒ v = −dz/dt = +z × d(log h)/dt：框变大=接近。
    """
    tt = np.array([(r["frame"] - win[0]["frame"]) / FPS for r in win])
    hh = np.array([r["h"] for r in win], float)
    zz = np.array([r["z"] for r in win], float)
    dz = float(np.polyfit(tt, zz, 1)[0])
    dlh = float(np.polyfit(tt, np.log(hh), 1)[0])
    zm = float(np.median(zz))
    return (-dz, zm * dlh)


def _gate_sim(tracks):
    """锚对模拟：dt 0.15~0.35s；真运动速度取锚对中点 ±0.5s 的长窗拟合。"""
    out = []
    for tr in tracks:
        for k in range(0, len(tr) - 12, 6):
            pair_dt = [j - k for j in range(k + 9, min(len(tr), k + 22))]
            for j in pair_dt:
                dt = (tr[j]["frame"] - tr[k]["frame"]) / FPS
                if not 0.15 <= dt <= 0.35:
                    continue
                a, b = tr[k], tr[j]
                if not (a.get("z") and b.get("z")):
                    continue
                if min(a["h"], b["h"]) < 12:
                    continue
                if min(a["conf"], b["conf"]) < 0.4:
                    continue
                lo, hi = max(0, k - 15), min(len(tr), j + 16)
                win = tr[lo:hi]
                if len(win) < 20 or not all(r.get("z") for r in win):
                    continue
                _vz, vh = _win_speed(win)
                dz_real = vh * dt
                dz_obs = b["z"] - a["z"]
                gate = GATE_BASE_M + GATE_MPS * dt
                out.append({"dt": dt, "gate": gate, "dz_obs": dz_obs,
                            "dz_real": dz_real, "v": vh,
                            "fired": abs(dz_obs) > gate,
                            "real_over": abs(dz_real) > gate})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="+")
    args = ap.parse_args()
    for path in args.jsonl:
        rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        print(f"== {path.rsplit(chr(92), 1)[-1]}")
        print(f"   帧 {len({r['frame'] for r in rows})}  框 {len(rows)}  "
              f"车框 {sum(1 for r in rows if r['cls'] == 'car')}")
        for cls, mh in (("car", 10), ("coin", 6), ("bonus", 6)):
            for feat, label in (("h", "高"), ("area", "面积")):
                f = _fit(rows, cls, mh, feat)
                if f:
                    print(f"   [{cls}/{label}] n={f['n']} 斜率 {f['slope']:.2f} "
                          f"R² {f['r2']:.2f} 残差 P50×{f['p50']:.2f} "
                          f"P90×{f['p90']:.2f} z {f['zmin']:.0f}~{f['zmax']:.0f}m "
                          f"f {f['fmin']:.0f}~{f['fmax']:.0f}px")
                else:
                    print(f"   [{cls}/{label}] 样本不足")
        cars = [r for r in rows if r["cls"] == "car"]
        tracks = _tracks(cars)
        print(f"   车轨迹 {len(tracks)}（≥5 帧）")
        for W in (15, 30):
            vz, vh = [], []
            for tr in tracks:
                for s in range(0, len(tr) - W + 1, 5):
                    win = tr[s:s + W]
                    if not all(r.get("z") for r in win):
                        continue
                    if min(r["h"] for r in win) < 12 \
                            or min(r["conf"] for r in win) < 0.4:
                        continue
                    a, b = _win_speed(win)
                    vz.append(a)
                    vh.append(b)
            for name, v in (("纵深", vz), ("框高", vh)):
                arr = np.array([x for x in v if np.isfinite(x)])
                pos = arr[arr > 0]
                if not pos.size:
                    continue
                print(f"   {W/60:.2f}s窗 {name}：n={arr.size} 正接近 {pos.size} "
                      f"P50 {np.percentile(pos,50):.1f} P90 {np.percentile(pos,90):.1f} "
                      f"P99 {np.percentile(pos,99):.1f} >45m/s {int((pos>45).sum())}")
        ev = _gate_sim(tracks)
        if ev:
            fired = [e for e in ev if e["fired"]]
            real = [e for e in fired if e["real_over"]]
            fake = [e for e in fired if not e["real_over"]]
            err = np.array([abs(e["dz_obs"] - e["dz_real"]) for e in ev])
            print(f"   门模拟：锚对 {len(ev)}，门触发 {len(fired)} "
                  f"({len(fired)*100/len(ev):.0f}%)；其中真运动也超门 {len(real)}，"
                  f"疑似锚点跳 {len(fake)}")
            print(f"   锚对 |实测Δz−真运动Δz|：P50 {np.percentile(err,50):.1f}m "
                  f"P90 {np.percentile(err,90):.1f}m")
            for dt in (0.15, 0.2, 0.25, 0.3, 0.35):
                sel = [e for e in ev if abs(e["dt"] - dt) <= 0.025]
                if not sel:
                    continue
                f2 = [e for e in sel if e["fired"]]
                r2 = [e for e in f2 if e["real_over"]]
                print(f"     dt={dt:.2f}s：{len(sel)} 对，触发 {len(f2)}，"
                      f"真运动超门 {len(r2)}，最大实测Δz "
                      f"{max(abs(e['dz_obs']) for e in sel):.1f}m")


if __name__ == "__main__":
    main()
