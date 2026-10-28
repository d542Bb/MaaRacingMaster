"""深度锚点归因（现有 trace 字段可还原的部分，2026-10-22）。

trace 没有逐条锚点的 det_idx/原始 bbox，但每条距离读数可反推锚值：

- HOLD / HELD + HOLD：m 就是锚值；
- RANGE_PROP：anchor_z = m + v_close × age（同拍同式反解）；
- 同一拍两条轨道 age 相同（±2ms）→ 锚点时间戳相同，来自同一张深度清单。

对每个近威胁读数（m<8m）把失败拆成三类：
1. ``extrapolation``：锚值本身与框证同量级（0.5~2×），塌陷由外推放大；
2. ``cross_assoc``：锚值远小于框证，但同拍有另一条**框证自洽**的轨道，
   其锚值与它相差 ≤ max(2m, 35%)——候选串轨（同清单+同量级，不是证明）；
3. ``depth_point``：锚值远小于框证，且没有可匹配的同拍轨道——深度点/
   框内近簇测错。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_anchor_attribution.py
    ... probe_anchor_attribution.py 20261022_140508_p2 ...
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

H_NOM_M = 1.5
NEAR_M = 8.0
CONSIST_LO, CONSIST_HI = 0.5, 2.0
SAME_AGE_MS = 2.0
CROSS_ABS_M = 2.0
CROSS_REL = 0.35


def _root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("需要 APPDATA（Windows 运行数据目录）")
    return Path(base) / "MaaRacingMaster" / "debug" / "speedrush"


def _load(name: str) -> list[dict]:
    p = _root() / name / f"trace_{name}.jsonl"
    if not p.exists():
        raise SystemExit(f"找不到 {p}")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def _anchor_z(c: dict) -> float | None:
    m = c.get("m")
    if m is None:
        return None
    if c.get("method") == "RANGE_PROP":
        return m + (c.get("v") or 0.0) * (c.get("age") or 0.0) / 1000.0
    return m


def replay(name: str) -> dict:
    rows = _load(name)
    last_anchor: dict[int, float] = {}
    out = {"name": name, "events": [], "classes": {}, "co_events": []}
    for row in rows:
        dist = {c["id"]: c for c in (row.get("dist") or [])}
        hbb = {c["id"]: c for c in (row.get("car_h") or [])}
        focal = row.get("dgeo_focal")
        if not focal:
            continue
        info = {}
        for tid, c in dist.items():
            m = c.get("m")
            h = (hbb.get(tid) or {}).get("h")
            if m is None or h is None or h <= 0:
                continue
            az = _anchor_z(c)
            if az is None:
                continue
            info[tid] = {"m": m, "age": c.get("age"), "v": c.get("v"),
                         "method": c.get("method"), "src": c.get("src"),
                         "h": h, "az": az,
                         "zb": focal * H_NOM_M / h}
        for tid, d in info.items():
            m, h, az, zb = d["m"], d["h"], d["az"], d["zb"]
            if not (0.0 < m < NEAR_M) or h >= 80:
                continue
            ratio = az / zb if zb > 0 else None
            co = []
            for oid, o in info.items():
                if oid == tid or o["age"] is None or d["age"] is None:
                    continue
                if abs(o["age"] - d["age"]) > SAME_AGE_MS:
                    continue
                if not (CONSIST_LO <= o["az"] / o["zb"] <= CONSIST_HI):
                    continue
                if abs(az - o["az"]) <= max(CROSS_ABS_M, CROSS_REL * o["az"]):
                    co.append((oid, round(o["az"], 2), round(o["zb"], 1),
                               round(o["age"], 1)))
            if ratio is not None and CONSIST_LO <= ratio <= CONSIST_HI:
                cls = "extrapolation"
            elif co:
                cls = "cross_assoc"
            else:
                cls = "depth_point"
            out["classes"][cls] = out["classes"].get(cls, 0) + 1
            ev = {"fid": row["fid"], "id": tid, "m": round(m, 2),
                  "h": h, "age": d["age"], "v": d["v"],
                  "method": d["method"], "src": d["src"],
                  "az": round(az, 2), "zb": round(zb, 1),
                  "ratio": None if ratio is None else round(ratio, 2),
                  "cls": cls, "co": co}
            out["events"].append(ev)
            if co:
                out["co_events"].append(ev)
            # 同一轨道的锚值跳变（相邻两次锚值）
            prev = last_anchor.get(tid)
            if prev and prev > 0 and abs(az - prev) / prev > 0.5:
                ev["anchor_jump"] = (round(prev, 2), round(az, 2))
            last_anchor[tid] = az
    return out


def report(r: dict) -> None:
    print(f"===== {r['name']} | 近威胁读数 {len(r['events'])} | {r['classes']}")
    for e in r["events"][:12]:
        jump = f" 锚跳 {e['anchor_jump']}" if "anchor_jump" in e else ""
        co = f" 同拍候选 {e['co']}" if e["co"] else ""
        print(f"  fid {e['fid']} id{e['id']} m={e['m']} h={e['h']}"
              f" age={e['age']} v={e['v']} {e['src']} "
              f"锚={e['az']} 框证={e['zb']} 比={e['ratio']}"
              f" → {e['cls']}{jump}{co}")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = argv[1:] or [
        "20261022_140023_p1", "20261022_140108_p2", "20261022_140223_p1",
        "20261022_140307_p2", "20261022_140424_p1", "20261022_140508_p2",
        "20261022_140625_p1", "20261022_140709_p2", "20261022_140822_p1",
        "20261022_140907_p2",
    ]
    agg: dict[str, int] = {}
    jumps = 0
    total = 0
    for n in names:
        r = replay(n)
        report(r)
        for k, v in r["classes"].items():
            agg[k] = agg.get(k, 0) + v
        jumps += sum(1 for e in r["events"] if "anchor_jump" in e)
        total += len(r["events"])
    print(f"\n== 合计：近威胁读数 {total} | {agg} | 锚值跳变 {jumps}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
