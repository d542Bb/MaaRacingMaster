# -*- coding: utf-8 -*-
"""离线影子：有米制距离却没有速度 / 到站时间的原因拆解（撞车复盘实验 7）。

只读控制 trace，不 import 生产代码、不发控制指令。

代码语义（tracking.py / decision.py）：
  v_close 与锚点同生命周期；锚间 dt ≥ gate_eval_min_ms(150ms) 才发布，
  新锚不清 v_close（跨锚保留），簇跳变 / 超龄清空。
  t_meet = z / v_close；v_close ≤ 0 → None（掠过/远离=占用语义）；
  v_close None → 回退像素 t_meet；t_ov 只吃米制 t_meet（>0 且非桥接）。

因此「有距离但无 t_ov」有三种互斥原因：
  A 估计窗未开：本拍 v is None（刚换锚 / 簇跳后重开窗）
  B 目标在拉开：v ≤ 0（t_ov 按占用语义为空，不是缺数）
  C 横向缺失：x is None（z/v/t_ov 有，但消费端用不到横向）

本探针按车统计：首次 z → 首次 v → 首次 v>0 → 首次 t_ov → 首次决策可用
（x/z/t_ov 同时有）的间隔，以及 z≤24m 行动窗内各状态的拍数占比。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

ACTION_Z_M = 24.0
CONFLICT_Z_M = 18.0
ENVELOPE_M = 2.3


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def analyze(path: str):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    t = [0.0]
    for r in rows[1:]:
        t.append(t[-1] + (r.get("dt") or 0.05))
    cars: dict[int, list] = {}
    for i, r in enumerate(rows):
        dmap = {d["id"]: d for d in (r.get("dist") or [])}
        for m in r.get("mcars") or []:
            d = dmap.get(m["id"], {})
            has_z = m.get("z") is not None or d.get("m") is not None
            cars.setdefault(m["id"], []).append({
                "i": i, "t": t[i], "fid": r.get("fid"),
                "z": m.get("z") if m.get("z") is not None else d.get("m"),
                "x": m.get("x"), "v": m.get("v"), "tov": m.get("t_ov"),
                "why": d.get("why"), "src": m.get("src"),
                "age": m.get("age"), "has_z": has_z,
            })
    return cars, t


def state_of(c):
    if not c["has_z"]:
        return "no_z"
    if c["v"] is None:
        return "A_est_window"
    if c["v"] <= 0.0:
        return "B_receding"
    if c["tov"] is None:
        return "C_tov_absent"
    if c["x"] is None:
        return "D_no_x"
    return "usable"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--focus", type=int, default=None)
    args = ap.parse_args()
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        cars, t = analyze(path)
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        summary = []
        for cid, s in sorted(cars.items()):
            if not any(c["has_z"] for c in s):
                continue
            first_z = next(c for c in s if c["has_z"])
            first_v = next((c for c in s if c["v"] is not None), None)
            first_vpos = next((c for c in s if c["v"] is not None and c["v"] > 0), None)
            first_tov = next((c for c in s if c["tov"] is not None), None)
            first_use = next((c for c in s if state_of(c) == "usable"), None)
            # 行动窗（z≤24m）内的状态拍数
            act = [c for c in s if c["has_z"] and c["z"] is not None
                   and 0.0 <= c["z"] <= ACTION_Z_M]
            cnt = {}
            for c in act:
                cnt[state_of(c)] = cnt.get(state_of(c), 0) + 1
            summary.append({
                "cid": cid, "first_z": first_z, "first_v": first_v,
                "first_vpos": first_vpos, "first_tov": first_tov,
                "first_use": first_use, "act": act, "cnt": cnt,
            })
        # 1) 首次 z → 首次 v / t_ov / usable 的间隔
        gaps_v = [(x["first_v"]["t"] - x["first_z"]["t"])
                  for x in summary if x["first_v"]]
        gaps_tov = [(x["first_tov"]["t"] - x["first_z"]["t"])
                    for x in summary if x["first_tov"]]
        gaps_use = [(x["first_use"]["t"] - x["first_z"]["t"])
                    for x in summary if x["first_use"]]
        nz = len(summary)
        no_v = [x for x in summary if not x["first_v"]]
        no_tov = [x for x in summary if not x["first_tov"]]
        no_use = [x for x in summary if not x["first_use"]]
        print(f"   有米制距离的车 {nz}：无速度 {len(no_v)}，无 t_ov {len(no_tov)}，"
              f"无决策可用 {len(no_use)}")
        for name, g in (("首 z→首 v", gaps_v), ("首 z→首 t_ov", gaps_tov),
                        ("首 z→首可用", gaps_use)):
            if g:
                print(f"   {name}: 中位 {_pct(g, 0.5):.2f}s  P90 {_pct(g, 0.9):.2f}s "
                      f"最大 {max(g):.2f}s")
        # 2) 行动窗内状态占比（按拍聚合）
        tot = {}
        for x in summary:
            for k, v in x["cnt"].items():
                tot[k] = tot.get(k, 0) + v
        allt = sum(tot.values()) or 1
        print(f"   z≤{ACTION_Z_M:.0f}m 拍数 {allt}：")
        for k in sorted(tot):
            print(f"     {k}: {tot[k]} ({tot[k]*100/allt:.0f}%)")
        # 3) 全程无 t_ov 的车：是 A 还是 B
        print("   无 t_ov 车的成因：")
        buckets = {}
        for x in no_tov:
            vs = [c["v"] for c in x["act"] if c["has_z"]]
            if not vs:
                b = "行动窗外"
            elif all(v is None for v in vs):
                b = "A 全程估计窗未开"
            elif all(v is not None and v <= 0 for v in vs):
                b = "B 全程 v≤0"
            elif any(v is not None and v > 0 for v in vs):
                b = "C 有正 v 但 t_ov 始终为空"
            else:
                b = "A/B 混合"
            buckets[b] = buckets.get(b, 0) + 1
        for k in sorted(buckets):
            print(f"     {k}: {buckets[k]}")
        # 4) v 从有到无的断供事件（簇跳/丢轨），行动窗内
        for x in summary:
            for k in range(1, len(x["act"])):
                a, b = x["act"][k - 1], x["act"][k]
                if a["v"] is not None and b["v"] is None:
                    print(f"   car{x['cid']} v 断供 t={b['t']:.2f}s fid={b['fid']} "
                          f"z={b['z']:.1f} why={b['why']} age={b['age']}")
        # 5) 明细
        for x in summary:
            if not x["act"]:
                continue
            fz, fv, ft, fu = x["first_z"], x["first_v"], x["first_tov"], x["first_use"]
            sv = ("—" if not fv else
                  f"fid{fv['fid']} {fv['v']:.1f} ({fv['t']-fz['t']:.2f}s)")
            st = ("—" if not ft else
                  f"fid{ft['fid']} {ft['tov']:.2f} ({ft['t']-fz['t']:.2f}s)")
            su = ("—" if not fu else
                  f"fid{fu['fid']} ({fu['t']-fz['t']:.2f}s)")
            print(f"   car{x['cid']}: z fid{fz['fid']} {fz['z']:.1f}m"
                  f" | v={sv} | t_ov={st} | 可用={su}"
                  f" | 窗内拍 {len(x['act'])} {x['cnt']}")
        if args.focus is not None:
            x = next((x for x in summary if x["cid"] == args.focus), None)
            if x:
                print(f"   焦点 car{args.focus}:")
                for c in x["act"][:40]:
                    print(f"     fid{c['fid']} t={c['t']:.2f} z={c['z']:.1f} "
                          f"x={c['x']} v={c['v']} t_ov={c['tov']} "
                          f"why={c['why']} src={c['src']} age={c['age']} "
                          f"→ {state_of(c)}")


if __name__ == "__main__":
    main()
