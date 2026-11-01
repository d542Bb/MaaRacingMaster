# -*- coding: utf-8 -*-
"""动态视界敏感度：开度分档会砍掉多少机会（第 4 期 4b 的离线前置）。

用旧局 `sel_dbg` 与 `scene` 复算：同一批选择帧输入，在 3.0/2.5/2.0 s
固定视界下各有多少拍/段会发出相邻请求；再按旧局的走廊宽与安全车近似
开度，给每拍一个拟议视界，统计相对固定 3.0 s 砍掉/新增多少。

只是**机会集合**的代理，不是闭环结果：没有自车位置/目标随视界变化的反馈，
不能当实车转化率。请求资格复用 request_cone 的复刻规则。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import request_cone as R  # noqa: E402
from speedrush_trace_reader import iter_rows  # noqa: E402

HORIZONS = (2.0, 2.5, 3.0)
OPEN_W, CROWD_W = 2.6, 2.0
OPEN_TTC, FAST_TTC = 4.0, 2.0
NEAR_M = 60.0


def _level(scene):
    lw = scene.get("lane_w") or 3.5
    road = scene.get("road") or {}
    width = None if road.get("w") is None else road["w"] / lw
    near = fast = 0
    ttc = None
    for o in scene.get("objs") or []:
        if not o or len(o) < 6 or o[0] != "car":
            continue
        if len(o) >= 16 and o[15] is False:      # seen=False 的旧对象不算安全
            continue
        z = o[7] if len(o) > 7 else None
        t = o[2] if len(o) > 2 else None
        if (z is not None and 0.0 <= z <= NEAR_M) or \
                (z is None and t is not None and t <= OPEN_TTC):
            near += 1
        if t is not None and t > 0.0:
            if t < FAST_TTC:
                fast += 1
            ttc = t if ttc is None else min(ttc, t)
    if width is None and ttc is None:
        return "unknown"
    if (width is not None and width <= CROWD_W) or fast >= 2:
        return "crowded"
    if (width is not None and width >= OPEN_W) and (ttc is None or ttc >= OPEN_TTC):
        return "open"
    return "normal"


def _horizon(level):
    return {"open": 3.0, "normal": 2.5, "crowded": 2.0}.get(level, 3.0)


def analyze(rows):
    base = R._policy()
    hit = {h: [] for h in HORIZONS}
    levels = {}
    switch = 0
    prev_level = None
    for row in rows:
        dbg = row.get("sel_dbg") or {}
        if not dbg or dbg.get("ego") is None:
            continue
        route = row.get("route") or {}
        if route.get("ph") == "merging":
            continue
        ego = dbg["ego"]
        c0 = dbg.get("c0") or 0.0
        ref = route.get("ref")
        commit = None if ref is None else ref + c0
        cars = dbg.get("car")
        for h in HORIZONS:
            c = dict(base, horizon=h)
            any_req = False
            for kind, key in (("reward", "rw"), ("coin", "coin")):
                for e in dbg.get(key) or []:
                    if e and len(e) >= 2:
                        _, nr = R._entry(kind, e[0], e[1], ego, commit, c, cars)
                        any_req = any_req or nr
            hit[h].append(bool(any_req))
        lvl = _level(row.get("scene") or {})
        levels[lvl] = levels.get(lvl, 0) + 1
        if prev_level is not None and lvl != prev_level:
            switch += 1
        prev_level = lvl

    def ticks(h):
        return sum(1 for v in hit[h] if v)

    def segs(h):
        n, cur = 0, False
        for v in hit[h]:
            if v and not cur:
                n += 1
            cur = v
        return n

    proposed_ticks = 0
    dropped = 0
    used = 0
    # 按行序重放：同一下标在 hit[2/2.5/3] 里对齐（同一次循环 append）
    idx = 0
    for row in rows:
        dbg = row.get("sel_dbg") or {}
        if not dbg or dbg.get("ego") is None:
            continue
        if (row.get("route") or {}).get("ph") == "merging":
            continue
        lvl = _level(row.get("scene") or {})
        h = _horizon(lvl)
        used += 1
        v = hit[h][idx]
        proposed_ticks += int(v)
        if hit[3.0][idx] and not v:
            dropped += 1
        idx += 1
    return {
        "rows": used,
        "levels": levels,
        "level_switches": switch,
        "request_ticks_by_h": {f"{h:.1f}s": ticks(h) for h in HORIZONS},
        "request_segments_by_h": {f"{h:.1f}s": segs(h) for h in HORIZONS},
        "proposed_request_ticks": proposed_ticks,
        "dropped_vs_static_3s": dropped,
        "note": "机会集合代理：无闭环反馈；开度用旧 scene 宽与车近似，"
                "未见旧对象按不安全剔除。",
    }


def _path(session):
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261101_113145")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rep = analyze(list(iter_rows(_path(args.session))))
    rep["session"] = args.session
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
