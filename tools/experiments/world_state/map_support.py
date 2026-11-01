# -*- coding: utf-8 -*-
"""地图支撑曲线：用旧局 trace 的走廊带复算「预测距离 → 中心误差」。

生产侧 `lane_map.py` 是同一套预测/校准算法的实现；本脚本按实验区纪律
自包含（不 import 业务代码），常量与生产一致，用于回答方案第 2 阶段的
验收问题：**现有走廊带在多远、隔多久之后还准**。

输入：`scene.road.bands`（车道级实测，米/自车系）、`cor_far`（16~80m 粗带）、
`v`（在线前向速度 m/s）、`v_lat`（横速 道/s）、`dt`。

输出：按距离桶的误差样本（|误差| 中位/p90、偏置）与支撑距离分布。

口径：每拍先按自车运动平移地图（前进 v·dt、横移 −v_lat·lane_w·dt），
用平移后、校正前的图与**新鲜实测带**比较取误差，再用本拍带校正。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402

MAX_S = 80.0
STEP = 2.0
TAU_CONF_S = 1.0
AGE_DECAY_MS = 500.0
CONF_FLOOR = 0.05
MAX_AGE_MS = 2500.0
COARSE_CONF = 0.3
BUCKETS = ((0, 10), (10, 20), (20, 30), (30, 50), (50, 80))


class Node:
    __slots__ = ("center", "conf", "age", "src")

    def __init__(self):
        self.center = None
        self.conf = 0.0
        self.age = math.inf
        self.src = "none"


class Map:
    def __init__(self):
        self.s = [i * STEP for i in range(int(MAX_S / STEP) + 1)]
        self.n = [Node() for _ in self.s]

    def _index(self, s):
        p = min(max(s, 0.0), MAX_S) / STEP
        i0 = min(int(p), len(self.s) - 1)
        return i0, min(i0 + 1, len(self.s) - 1), p - i0

    def at(self, s):
        i0, i1, w = self._index(s)
        a, b = self.n[i0], self.n[i1]
        if a.center is None and b.center is None:
            return None
        if a.center is None:
            return b
        if b.center is None:
            return a
        c = Node()
        c.center = a.center + (b.center - a.center) * w
        c.conf = a.conf + (b.conf - a.conf) * w
        c.age = min(a.age, b.age)
        c.src = a.src if a.conf >= b.conf else b.src
        return c

    def step_map(self, v, v_lat, lane_w, dt):
        dt = max(0.0, min(dt, 0.2))
        ds = max(0.0, v or 0.0) * dt
        dx = -(v_lat or 0.0) * lane_w * dt
        old = self.n
        new = []
        decay = math.exp(-dt / TAU_CONF_S)
        for s in self.s:
            src_s = s + ds
            if src_s > MAX_S:
                n = Node()
            else:
                i0, i1, w = self._index(src_s)
                a, b = old[i0], old[i1]
                n = Node()
                if a.center is None and b.center is None:
                    pass
                elif a.center is None:
                    n.center = b.center
                elif b.center is None:
                    n.center = a.center
                else:
                    n.center = a.center + (b.center - a.center) * w
                n.conf = a.conf + (b.conf - a.conf) * w
                n.age = min(a.age, b.age)
                n.src = a.src if a.conf >= b.conf else b.src
                if n.center is not None:
                    n.center += dx
            n.conf *= decay
            if n.age != math.inf:
                n.age += dt * 1000.0
            new.append(n)
        self.n = new

    def correct(self, bands, coarse):
        for b in coarse:
            self._apply(b, coarse=True)
        for b in bands:
            if b.get("src") == "inferred":
                continue
            self._apply(b, coarse=False)

    def _apply(self, b, *, coarse):
        z0, z1 = b["z0"], b["z1"]
        left, right = b["l"], b["r"]
        if left is None or right is None:
            return
        center = 0.5 * (left + right)
        age = b.get("age") or 0.0
        q = COARSE_CONF if coarse else max(b.get("conf") or 0.0, 0.5)
        q *= math.exp(-age / AGE_DECAY_MS)
        if q <= 0:
            return
        i0 = max(0, int(math.ceil(z0 / STEP - 1e-9)))
        i1 = min(len(self.n) - 1, int(math.floor(z1 / STEP + 1e-9)))
        if i1 < i0:
            i0 = i1 = min(range(len(self.n)),
                          key=lambda i: abs(self.s[i] - 0.5 * (z0 + z1)))
        for i in range(i0, i1 + 1):
            n = self.n[i]
            if coarse and n.conf > q and n.src != "inferred":
                continue
            if coarse:
                if n.center is None:
                    n.center = center
                n.conf = min(q, COARSE_CONF)
            else:
                n.center = center
                n.conf = q
            n.age = age
            n.src = "inferred" if coarse else str(b.get("src"))

    def expire(self):
        for n in self.n:
            if n.center is not None and (n.conf < CONF_FLOOR
                                         or n.age > MAX_AGE_MS):
                n.center, n.conf, n.age, n.src = None, 0.0, math.inf, "none"

    def support(self, conf_min=0.4):
        last = 0.0
        for i, n in enumerate(self.n):
            if n.src != "inferred" and n.center is not None                     and n.conf >= conf_min:
                last = self.s[i]
        return last


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _bands(scene):
    out = []
    for b in ((scene.get("road") or {}).get("bands") or []):
        if not b or len(b) < 7 or b[2] is None or b[3] is None:
            continue
        out.append({"z0": b[0], "z1": b[1], "l": b[2], "r": b[3],
                    "src": b[4], "age": b[5], "conf": b[6]})
    return out


def _coarse(row):
    out = []
    for b in row.get("cor_far") or []:
        if not b or len(b) < 5 or b[2] is None or b[3] is None:
            continue
        out.append({"z0": b[0], "z1": b[1], "l": b[2], "r": b[3],
                    "src": "inferred", "age": 0.0, "conf": 0.0})
    return out


def _resolve(args):
    if args.trace:
        return Path(args.trace)
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / args.session / "trace" / "speedrush.jsonl"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261101_113145")
    ap.add_argument("--trace")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rows = list(iter_rows(_resolve(args)))
    m = Map()
    buckets = {b: [] for b in BUCKETS}
    supports = []
    for row in rows:
        scene = row.get("scene") or {}
        lane_w = scene.get("lane_w") or 3.5
        dt = row.get("dt") or 0.05
        v = row.get("v_ego") or 0.0
        v_lat = row.get("v_lat") or 0.0
        m.step_map(v, v_lat, lane_w, dt)
        bands = _bands(scene)
        # 误差：校正前的图 vs 本拍新鲜实测
        for b in bands:
            if b["age"] is not None and b["age"] > 100.0:
                continue
            z = 0.5 * (b["z0"] + b["z1"])
            g = m.at(z)
            if g is None or g.center is None:
                continue
            ref = 0.5 * (b["l"] + b["r"])
            err = g.center - ref
            for lo, hi in BUCKETS:
                if lo <= z < hi:
                    buckets[(lo, hi)].append(err)
                    break
        m.correct(bands, _coarse(row))
        m.expire()
        supports.append(m.support())
    has_speed = any(r.get("v_ego") is not None for r in rows)
    rep = {
        "session": args.session if not args.trace
        else Path(args.trace).parent.parent.name,
        "rows": len(rows),
        "forward_speed": "v_ego" if has_speed else
        "missing（旧局无 v_ego：只算同弧长校正误差，不是跨距离预测误差）",
        "support_m": {
            "median": round(statistics.median(supports), 1) if supports else 0,
            "p90": round(_pct(supports, 90), 1) if supports else 0,
            "max": round(max(supports), 1) if supports else 0,
        },
        "err_by_distance": {},
    }
    for b, xs in buckets.items():
        key = f"{b[0]}-{b[1]}m"
        rep["err_by_distance"][key] = {
            "n": len(xs),
            "abs_median": None if not xs else round(
                statistics.median([abs(x) for x in xs]), 3),
            "abs_p90": None if not xs else round(
                _pct([abs(x) for x in xs], 90), 3),
            "bias_median": None if not xs else round(
                statistics.median(xs), 3),
        }
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
