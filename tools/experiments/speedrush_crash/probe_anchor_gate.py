"""锚点级门控回放（2026-10-22，锚点归因的下一步）。

不在输出距离上打补丁，而是在锚点发布前判一次：

- 锚值反解：HOLD/HELD 用 m，RANGE_PROP 用 `m + v_close × age`；
- 框证：`focal × 1.5m / h`；
- 锚值远小于框证（`az < 0.5 × bz`，即锚点比框允许的近得多）→ 拒绝该锚，
  沿用上一条可信锚（按原锚点计龄，600ms 到期），没有可信锚就置未知；
- 锚值 >= 0.5×框证 → 接受，包括由该锚外推出来的近读数。反向（锚比框远）
  不拦：那是低估接近，不是凭空造近车，且近车框常被截断。

验收：被拒的原始近威胁里，2D 证人既不近也不靠近 = 拦住的假近车；2D 说
近/在靠近 = 误伤；锚值合理但仍外推出近读数 = 这条门控的漏网（需要输出
层兜底）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_anchor_gate.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

H_NOM_M = 1.5
NEAR_M = 8.0
LO = 0.5
TTL_MS = 600.0
NEAR_H_PX = 80.0
NEAR_CY_PX = 500.0
APPROACH_H = 1.15
APPROACH_CY_PX = 20.0
APPROACH_WIN_S = 0.25


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


def _near2d(h, cy) -> bool:
    return ((h is not None and h >= NEAR_H_PX)
            or (cy is not None and cy >= NEAR_CY_PX))


def _appr(hist: list, ts: float) -> bool:
    old = [x for x in hist if ts - x[0] <= APPROACH_WIN_S]
    if len(old) < 2:
        return False
    _, h0, cy0 = old[0]
    _, h1, cy1 = hist[-1]
    if h0 and h0 > 0 and h1 / h0 >= APPROACH_H:
        return True
    return cy0 is not None and cy1 is not None \
        and cy1 - cy0 >= APPROACH_CY_PX


def replay(name: str) -> dict:
    rows = _load(name)
    trusted: dict[int, dict] = {}
    hist: dict[int, list] = {}
    r = {"name": name, "readings": 0, "bad": 0, "accepted": 0,
         "bad_near": 0, "bad_phantom": 0, "bad_real": 0,
         "held_near": 0, "unknown_near": 0, "accepted_phantom": 0,
         "accepted_real": 0, "events": []}
    for row in rows:
        dist = {c["id"]: c for c in (row.get("dist") or [])}
        hbb = {c["id"]: c for c in (row.get("car_h") or [])}
        pos = {c["id"]: c for c in (row.get("car_pos") or [])}
        focal = row.get("dgeo_focal")
        ts = row["ts_ns"] / 1e9
        for tid, c in dist.items():
            m = c.get("m")
            h = (hbb.get(tid) or {}).get("h")
            if m is None or m <= 0 or h is None or not focal:
                continue
            r["readings"] += 1
            age = c.get("age") or 0.0
            az = (m + (c.get("v") or 0.0) * age / 1000.0
                  if c.get("method") == "RANGE_PROP" else m)
            bz = float(focal) * H_NOM_M / h
            bad = az < LO * bz
            cy = (pos.get(tid) or {}).get("cy")
            near = _near2d(h, cy)
            hh = hist.setdefault(tid, [])
            hh.append((ts, h, cy))
            hist[tid] = [x for x in hh if ts - x[0] <= 0.5]
            appr = _appr(hist.get(tid, []), ts)
            threat = 0.0 < m < NEAR_M
            tr = trusted.get(tid)
            if tr is not None and (ts - tr["ts"]) * 1000.0 > TTL_MS:
                trusted.pop(tid, None)
                tr = None
            if bad:
                r["bad"] += 1
                if threat:
                    r["bad_near"] += 1
                    if near or appr:
                        r["bad_real"] += 1
                    else:
                        r["bad_phantom"] += 1
                        if tr is None:
                            r["unknown_near"] += 1
                        elif tr["z"] < NEAR_M:
                            r["held_near"] += 1
                r["events"].append({"fid": row["fid"], "id": tid,
                                    "m": round(m, 2), "h": h,
                                    "az": round(az, 2), "bz": round(bz, 1),
                                    "held": None if tr is None else tr["z"],
                                    "why": ("real" if (near or appr)
                                            else "phantom")})
                continue
            r["accepted"] += 1
            trusted[tid] = {"z": az, "ts": ts - age / 1000.0}
            if threat:
                if near or appr:
                    r["accepted_real"] += 1
                else:
                    r["accepted_phantom"] += 1
    return r


def report(r: dict) -> None:
    tot_ph = r["bad_phantom"] + r["accepted_phantom"]
    print(f"===== {r['name']} | 读数 {r['readings']} | 坏锚 {r['bad']}"
          f"（{100.0 * r['bad'] / r['readings']:.1f}%）| 接受 {r['accepted']}")
    print(f"  原始近威胁 {r['bad_near'] + r['accepted_real'] + r['accepted_phantom']}"
          f" | 假近车 {tot_ph}（框已近/在靠近之外的近读数）"
          f" | 门控拦住 {r['bad_phantom']} / 漏网 {r['accepted_phantom']}")
    print(f"  误伤（坏锚但 2D 说近/在靠近）{r['bad_real']}"
          f" | 坏锚威胁里无可信锚 {r['unknown_near']}"
          f" | 沿用旧值仍近 {r['held_near']}")
    for e in r["events"][:8]:
        print(f"    fid {e['fid']} id{e['id']} m={e['m']} h={e['h']}"
              f" 锚={e['az']} 框证={e['bz']} 旧值={e['held']} {e['why']}")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = argv[1:] or [
        "20261022_132510_p1", "20261022_132554_p2",
        "20261022_140023_p1", "20261022_140108_p2", "20261022_140223_p1",
        "20261022_140307_p2", "20261022_140424_p1", "20261022_140508_p2",
        "20261022_140625_p1", "20261022_140709_p2", "20261022_140822_p1",
        "20261022_140907_p2",
    ]
    agg = {}
    for n in names:
        r = replay(n)
        report(r)
        for k in ("readings", "bad", "accepted", "bad_near", "bad_phantom",
                  "bad_real", "held_near", "unknown_near",
                  "accepted_phantom", "accepted_real"):
            agg[k] = agg.get(k, 0) + r[k]
    print(f"\n== 合计 {agg}")
    ph = agg["bad_phantom"] + agg["accepted_phantom"]
    print(f"   假近车 {ph} | 门控拦 {agg['bad_phantom']}"
          f"（{100.0 * agg['bad_phantom'] / ph if ph else 0:.0f}%）"
          f" | 漏网 {agg['accepted_phantom']} | 误伤 {agg['bad_real']}"
          f" | 坏锚率 {100.0 * agg['bad'] / agg['readings']:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
