# -*- coding: utf-8 -*-
"""簇跳变门实测事件率（评审方法论建议：先数再立项，不论证「必然误清」）。

trace 的 dist 列逐车带 why（module.py:963，UNKNOWN 时必填）——
主簇跳变清锚（tracking.py:618）与亚门限丢锚（tracking.py:627）共用
CLUSTER_SWITCH 标签，但前者清锚（车变 UNKNOWN、dist why=CLUSTER_SWITCH），
后者保旧锚（车保持 ANCHOR、why=None）——dist.why=CLUSTER_SWITCH 恰为主
跳变清锚事件；亚门限丢弃只在该车后来再丢锚时以陈旧标签溢出（口径注）。
逐事件回放 last 锚 m → 下一锚 m 得触发 Δz 与锚间 dt，对照两道门阈值：
  簇门 cluster_switch_m=20m；亚门 z_jump=2.0+45·dt。
存续组 score=None 拍占比不在本探针（逐组 conf_min 未入 trace，无法离线
复算——缺口记文档）。
只读 debug 产物，不改生产。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
SESS = ["20261017_193629_p1", "20261017_193714_p2"]
REANCHOR_WIN_S = 2.0          # 事件后在此窗内重锚才算恢复
CLUSTER_M = 20.0              # 与 tracking.py 参数一致
ZJ_BASE, ZJ_MPS = 2.0, 45.0


def _p50(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def _p90(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.9))] if xs else float("nan")


def main() -> None:
    for s in SESS:
        tr = [json.loads(l) for l in
              (DBG / s / f"trace_{s}.jsonl").read_text(encoding="utf-8").splitlines()]
        n = len(tr)
        why_dist = {}
        ev_rows = []            # (帧序, car_id)
        for i, r in enumerate(tr):
            for d in (r.get("dist") or []):
                w = d.get("why")
                if w:
                    why_dist[w] = why_dist.get(w, 0) + 1
                if w == "CLUSTER_SWITCH":
                    ev_rows.append((i, d["id"]))
        # 逐车回放 last 锚 m / 下一锚 m
        dzs, dts, vs = [], [], []
        unrecovered = 0
        subgate_too = 0         # Δz 连亚门也过不了（两道门都拒）
        for i, cid in ev_rows:
            last_m = None
            last_ts = None
            for j in range(i - 1, -1, -1):
                for d in (tr[j].get("dist") or []):
                    if d["id"] == cid and d.get("m") is not None:
                        last_m = d["m"]
                        last_ts = tr[j]["ts_ns"]
                        break
                if last_m is not None:
                    break
            next_m = None
            next_ts = None
            for j in range(i + 1, len(tr)):
                if (tr[j]["ts_ns"] - tr[i]["ts_ns"]) / 1e9 > REANCHOR_WIN_S:
                    break
                for d in (tr[j].get("dist") or []):
                    if d["id"] == cid and d.get("m") is not None:
                        next_m = d["m"]
                        next_ts = tr[j]["ts_ns"]
                        break
                if next_m is not None:
                    break
            if last_m is None or next_m is None:
                unrecovered += 1
                continue
            dz = abs(next_m - last_m)
            dt = (next_ts - last_ts) / 1e9
            dzs.append(dz)
            dts.append(dt)
            vs.append(dz / dt if dt > 0 else float("nan"))
            if dz < ZJ_BASE + ZJ_MPS * dt:
                subgate_too += 1
        n_ev = len(ev_rows)
        print(f"== {s} == 拍数 {n}")
        print(f"  dist.why 分布: {why_dist}")
        ev_frames = len({i for i, _ in ev_rows})
        print(f"  CLUSTER_SWITCH 事件 {n_ev} 次 / {ev_frames} 拍 "
              f"({ev_frames / n * 100:.1f}% 拍有事件)")
        print(f"  EXPIRED {why_dist.get('EXPIRED', 0)} / TRACK_LOST "
              f"{why_dist.get('TRACK_LOST', 0)} / ASSOC_FAIL "
              f"{why_dist.get('ASSOC_FAIL', 0)}")
        if dzs:
            print(f"  触发 Δz |Δ| m: P50 {_p50(dzs):.1f} P90 {_p90(dzs):.1f} "
                  f"max {max(dzs):.1f}")
            print(f"  锚间 dt s: P50 {_p50(dts):.2f} P90 {_p90(dts):.2f} "
                  f"max {max(dts):.2f}")
            print(f"  隐含闭速 m/s: P50 {_p50(vs):.1f} P90 {_p90(vs):.1f} "
                  f"max {max(vs):.1f}  (>34={sum(1 for v in vs if v > 34)})")
            print(f"  重锚失败(窗{REANCHOR_WIN_S}s) {unrecovered}; "
                  f"Δz 连亚门都不过 {subgate_too}/{len(dzs)}")
        else:
            print(f"  无可回放事件对（重锚失败 {unrecovered}）")
        # 存续组分母代理：n_groups>0 拍占比（逐组 conf_min 未入 trace）
        grp = sum(1 for r in tr if (r.get("n_groups") or 0) > 0)
        print(f"  n_groups>0 拍占比 {grp / n * 100:.1f}%（分母代理；"
              f"存续组 score=None 占比需逐组 conf_min 入 trace 后复算）")


if __name__ == "__main__":
    main()
