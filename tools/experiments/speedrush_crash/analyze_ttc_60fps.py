# -*- coding: utf-8 -*-
"""离线验证：框高扩张率估 TTC（撞车复盘实验 10，60fps 干净条件）。

输入 = probe_size_speed.py 的逐帧框几何 + MoGe 纵深。只读数据，不接控制。

方法：
- 对每条车轨，0.5s 滑窗（60fps，W=30，步长 5 帧）做 log(h) 的稳健斜率
  （Theil–Sen：全部成对斜率取中位，抗框突变）；
- 图像 TTC：τ_h = 1 / d(log h)/dt（目标近似刚性、尺度变化主要来自接近）；
- 参照 TTC：τ_z = z_mid / (−dz/dt)，用同窗纵深斜率；
- 只在两者都为正、τ 在 0.05~10s 时比较；另报 R²、覆盖率、连续窗跳变。

限制：60fps 录像非撞车局、人工驾驶、无控制数据；这里只验证“图像运动
能不能给出稳定且量级正确的 TTC”，不验证在线预警收益。
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np

from analyze_size_speed import _tracks

FPS = 60.0
W = 30          # 0.5s
STRIDE = 5


def _robust_line(tt, yy):
    """Theil–Sen 斜率 + 稳健截距 + R²。"""
    slopes = []
    n = len(tt)
    for i in range(n):
        for j in range(i + 1, n):
            dt = tt[j] - tt[i]
            if dt > 1e-9:
                slopes.append((yy[j] - yy[i]) / dt)
    if not slopes:
        return None
    b = float(np.median(slopes))
    a = float(np.median(yy - b * tt))
    pred = a + b * tt
    ss_res = float(((yy - pred) ** 2).sum())
    ss_tot = float(((yy - yy.mean()) ** 2).sum()) or 1.0
    return b, a, 1.0 - ss_res / ss_tot


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def analyze(rows):
    cars = [r for r in rows if r["cls"] == "car"]
    tracks = _tracks(cars)
    both, valid_h, valid_z, all_win = [], 0, 0, 0
    ratios, corr = [], ([], [])
    seq = {}            # track idx -> 上一窗的 τ_h
    stable = []
    bands = {}
    art = []
    for ti, tr in enumerate(tracks):
        for s in range(0, len(tr) - W + 1, STRIDE):
            win = tr[s:s + W]
            if not all(r.get("z") for r in win):
                continue
            if min(r["h"] for r in win) < 10 \
                    or min(r["conf"] for r in win) < 0.4:
                continue
            all_win += 1
            tt = np.array([(r["frame"] - win[0]["frame"]) / FPS for r in win])
            hh = np.array([r["h"] for r in win], float)
            zz = np.array([r["z"] for r in win], float)
            lh = _robust_line(tt, np.log(hh))
            lz = _robust_line(tt, zz)
            if not lh or not lz:
                continue
            sh, lz_slope = lh[0], lz[0]
            zh = 1.0 / sh if sh > 1e-4 else None
            zz_mid = float(np.median(zz))
            tz = (zz_mid / -lz_slope
                  if lz_slope < -1e-3 else None)
            okh = zh is not None and 0.05 <= zh <= 10.0 and lh[2] >= 0.5
            okz = tz is not None and 0.05 <= tz <= 10.0 and lz[2] >= 0.5
            valid_h += int(okh)
            valid_z += int(okz)
            # 框突变窗口（窗内框高极差 >1.5 倍）
            jump = hh.max() / max(hh.min(), 1.0) > 1.5
            if okh and okz:
                both.append((zh, tz))
                ratios.append(zh / tz)
                corr[0].append(zh)
                corr[1].append(tz)
                band = ("0-15" if zz_mid < 15 else "15-30" if zz_mid < 30
                        else "30-60" if zz_mid < 60 else "60+")
                rec = bands.setdefault(band, {"n": 0, "err": [], "cov": []})
                rec["n"] += 1
                rec["err"].append(abs(zh - tz) / max(tz, 1e-3))
                rec["cov"].append(1.0)
                if jump:
                    art.append((abs(zh - tz) / max(tz, 1e-3), zh, tz))
                prev = seq.get(ti)
                if prev is not None and prev > 0:
                    stable.append(abs(zh - prev) / prev)
                seq[ti] = zh
    return {"all": all_win, "vh": valid_h, "vz": valid_z,
            "both": both, "ratio": ratios, "corr": corr,
            "stable": stable, "bands": bands, "art": art}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="+")
    args = ap.parse_args()
    for path in args.jsonl:
        rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        res = analyze(rows)
        print(f"== {path.rsplit(chr(92), 1)[-1]}")
        print(f"   可用窗口 {res['all']}；τ_h 有效 {res['vh']} "
              f"({res['vh']*100/(res['all'] or 1):.0f}%)；τ_z 有效 {res['vz']} "
              f"({res['vz']*100/(res['all'] or 1):.0f}%)；两者同时有效 "
              f"{len(res['both'])}")
        if res["ratio"]:
            r = res["ratio"]
            print(f"   τ_h/τ_z：P10 {_pct(r,.1):.2f}  P50 {_pct(r,.5):.2f}  "
                  f"P90 {_pct(r,.9):.2f}；±20% 内 "
                  f"{sum(1 for x in r if 0.8<=x<=1.2)*100/len(r):.0f}%  "
                  f"±50% 内 "
                  f"{sum(1 for x in r if 0.5<=x<=1.5)*100/len(r):.0f}%")
            a, b = res["corr"]
            if len(a) > 2:
                ra = np.argsort(np.argsort(a))
                rb = np.argsort(np.argsort(b))
                rho = float(np.corrcoef(ra, rb)[0, 1])
                print(f"   Spearman 相关 {rho:.2f}")
        if res["stable"]:
            s = res["stable"]
            print(f"   相邻有效窗 τ_h 相对跳变：P50 {_pct(s,.5)*100:.0f}%  "
                  f"P90 {_pct(s,.9)*100:.0f}%；>50% 的窗口 "
                  f"{sum(1 for x in s if x>0.5)*100/len(s):.0f}%")
        for k in ("0-15", "15-30", "30-60", "60+"):
            rec = res["bands"].get(k)
            if rec:
                print(f"   z {k}m：n={rec['n']} τ_h 相对误差中位 "
                      f"{_pct(rec['err'],.5)*100:.0f}% P90 "
                      f"{_pct(rec['err'],.9)*100:.0f}%")
        if res["art"]:
            print(f"   框高突变窗（窗内极差>1.5 倍）{len(res['art'])} 个，"
                  f"相对误差中位 {_pct([a for a,_,_ in res['art']],.5)*100:.0f}%")


if __name__ == "__main__":
    main()
