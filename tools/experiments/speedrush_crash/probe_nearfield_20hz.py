"""20Hz 取证局的近场读法（2026-10-22 起）。

读 debug/speedrush/<局>/trace_*.jsonl，回答四件事：
1. 20Hz 落盘是否到位（拍数/实际频率/调试图丢帧）；
2. 卡顿归因：dt>阈值的拍，其 pace_body 属于**上一拍**（trace 的 pace 三列
   记在上一拍头上），分段列（det/yolo/dgeo）是本拍自己的——归因要看上一行；
3. 近场米制读数的稳定性：HOLD/RANGE_PROP 分布、锚龄、相邻拍 |Δm| 跳变；
4. 框高否决米制：近场相邻拍 |Δm| 大，但框高变化不支持（框高应随 1/m 同量级
   变化），或 m<3m 但框高远小于该距离应有的像素高（focal*h_nom/m），标记
   「米制与框证矛盾」。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_nearfield_20hz.py
    ... probe_nearfield_20hz.py 20261022_132554_p2 [另一局 ...]
不带参数时读 20261022_132510_p1 / 20261022_132554_p2。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

H_NOM_M = 1.5          # 轿车框高名义值（只用于「框高否决」，阈值放得很宽）
H_FACTOR_MIN = 0.35    # 实际框高低于应有高度的 35% 即判矛盾
H_TOL = 0.35           # 跳变时框高与 1/m 预期的相对偏差超过此值即判矛盾
NEAR_M = 15.0
JUMP_M = 2.5
STALL_DT_S = 0.12


def _root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("需要 APPDATA（Windows 运行数据目录）")
    return Path(base) / "MaaRacingMaster" / "debug" / "speedrush"


def _load(name: str) -> list[dict]:
    d = _root() / name
    p = d / f"trace_{name}.jsonl"
    if not p.exists():
        raise SystemExit(f"找不到 {p}")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def _q(xs: list[float]) -> str:
    if not xs:
        return "-"
    xs = sorted(xs)
    return (f"n={len(xs)} P50={statistics.median(xs):.0f} "
            f"P90={xs[int(len(xs) * 0.9)]:.0f} max={max(xs):.0f}")


def _cy_of(row: dict) -> dict[int, float]:
    return {c["id"]: c["cy"] for c in (row.get("car_pos") or [])
            if c.get("cy") is not None}


def report(name: str) -> None:
    rows = _load(name)
    print(f"===== {name}")
    dur = (rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9
    print(f"拍数 {len(rows)} / {dur:.1f}s = {len(rows) / dur:.1f}Hz"
          f" | 调试图 saved={rows[-1].get('dbg_saved')}"
          f" dropped={rows[-1].get('dbg_dropped')}")

    dbg = [r["dbg_ms"] for r in rows if r.get("dbg_ms") is not None]
    if dbg:
        print(f"调试图单对耗时 {_q(dbg)}ms（IO 线程，不占主路）")

    # ---- 卡顿（body 是上一拍；分段列要看上一行）----
    stalls = []
    for i, r in enumerate(rows):
        if r.get("dt") is None or r["dt"] <= STALL_DT_S:
            continue
        prev = rows[i - 1] if i else None
        stalls.append((
            r["fid"], r["dt"], r.get("pace_body"),
            None if prev is None else prev.get("det_ms"),
            None if prev is None else prev.get("yolo_wait_ms"),
            None if prev is None else prev.get("dgeo_ms"),
            None if prev is None else prev.get("fid")))
    print(f"dt>{STALL_DT_S}s 的拍 {len(stalls)}：")
    for s in stalls:
        print(f"  fid {s[0]} dt={s[1]:.3f}s body={s[2]}ms"
              f" | 上一拍 fid {s[6]} det={s[3]} ywait={s[4]} dgeo={s[5]}")

    # ---- 近场米制 ----
    methods: dict[str, int] = {}
    ages: dict[str, list[float]] = {}
    prev: dict[int, dict] = {}
    jumps = []
    mism = []
    for r in rows:
        cy = _cy_of(r)
        focal = r.get("dgeo_focal")
        hmap = {c["id"]: c for c in (r.get("car_h") or [])}
        for c in (r.get("dist") or []):
            m = c.get("m")
            if m is None:
                prev.pop(c["id"], None)
                continue
            h = (hmap.get(c["id"]) or {}).get("h")
            if m < NEAR_M:
                key = str(c.get("method"))
                methods[key] = methods.get(key, 0) + 1
                if c.get("age") is not None:
                    ages.setdefault(key, []).append(c["age"])
                if m < 3.0 and focal and h is not None:
                    h_need = focal * H_NOM_M / m
                    if h < h_need * H_FACTOR_MIN:
                        mism.append((r["fid"], c["id"], round(m, 2), h,
                                     round(h_need)))
            p = prev.get(c["id"])
            if (p is not None and p.get("method") and c.get("method")
                    and p["m"] < NEAR_M and abs(m - p["m"]) > JUMP_M):
                dc = None
                if p.get("cy") is not None and cy.get(c["id"]) is not None:
                    dc = abs(cy[c["id"]] - p["cy"])
                bad = False
                if p.get("h") and h is not None and m > 0.0:
                    need = p["h"] * p["m"] / m       # 框高应随 1/m 变化
                    bad = abs(h - need) > H_TOL * need
                jumps.append((r["fid"], c["id"], round(p["m"], 1),
                              round(m, 1), round(m - p["m"], 1), dc,
                              str(p["method"]), str(c["method"]),
                              c.get("age"), p.get("h"), h, bad))
            prev[c["id"]] = {"m": m, "method": c.get("method"),
                             "cy": cy.get(c["id"]), "h": h}
    print(f"近场(<{NEAR_M:.0f}m)读数 {sum(methods.values())} 条：{methods}")
    for k, xs in ages.items():
        print(f"  锚龄 {k}: {_q(xs)}ms")
    bad_n = sum(1 for j in jumps if j[11])
    print(f"近场相邻拍 |Δm|>{JUMP_M}m 的跳变 {len(jumps)} 次；"
          f"其中框高不支持该变化（框/米制矛盾）{bad_n} 次：")
    for j in jumps[:20]:
        flag = " ← 框/米制矛盾" if j[11] else ""
        print(f"  fid {j[0]} id{j[1]} {j[2]}→{j[3]}m ({j[4]:+}m)"
              f" h={j[9]}→{j[10]} dcy={j[5]} {j[6]}→{j[7]}"
              f" age={j[8]}{flag}")
    if mism:
        print(f"近报(<3m)但框高远小于应有值 {len(mism)} 次：")
        for m in mism[:10]:
            print(f"  fid {m[0]} id{m[1]} m={m[2]} h={m[3]}px"
                  f"（该距离应约 {m[4]}px）")

    # ---- 近场框驻留（seen>0 连续 >=2 拍）----
    cur: dict[int, list] = {}
    eps = []
    for r in rows:
        zm = {c["id"]: c["z"] for c in (r.get("mcars") or [])
              if c.get("z") is not None}
        present = set()
        for c in (r.get("car_h") or []):
            z = zm.get(c["id"])
            if z is None or z >= 20.0 or c["seen"] <= 0:
                continue
            e = cur.setdefault(c["id"], [0, 0, None, None])
            e[0] += 1
            e[1] = max(e[1], c["seen"])
            e[2] = z
            e[3] = c["h"]
            present.add(c["id"])
        for tid in [t for t in cur if t not in present]:
            e = cur.pop(tid)
            if e[0] >= 2:
                eps.append((tid, *e))
    print(f"近场框驻留（z<20m 且 seen>0 连续 >=2 拍）{len(eps)} 段：")
    for e in sorted(eps, key=lambda x: -x[1])[:8]:
        print(f"  id{e[0]} 连续 {e[1]} 拍 | max seen={e[2]} |"
              f" 末 z={e[3]}m h={e[4]}px")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = argv[1:] or ["20261022_132510_p1", "20261022_132554_p2"]
    for n in names:
        report(n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
