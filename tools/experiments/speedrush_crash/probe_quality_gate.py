"""深度锚点质量门回放（支线 B，2026-10-22）。

三个独立量：
- 深度锚值 `az`（HOLD=m；RANGE_PROP=m+v_close×age）；
- 框证 `bz = focal×1.5/h`；
- 地面平面先验 `pz = focal×cam_h/(y2−y_h)`，y2=car_pos.cy+h/2。

分级（不做“平面优先”，也不改写距离）：
- 框证与平面先验互相矛盾（比不在 0.5~2）→ ``unknown``（证据互斥，不挑值）；
- 锚值同时严重偏离两者（<0.5×min 或 >2×max）→ ``reject``（不发布该锚）；
- 其余 → ``allow``。

统计：近威胁（m<8）里的假近车/真实近车各被如何处置；reject/unknown 时
2D 框与粗层占用是否仍在（“拒绝距离 ≠ 车道为空”）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_quality_gate.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

H_NOM = 1.5
NEAR_M = 8.0
LO, HI = 0.5, 2.0
NEAR_H = 80.0
NEAR_Y2 = 500.0
APPROACH_H = 1.15
APPROACH_Y2 = 20.0
WIN_S = 0.25


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


def _near(h, y2) -> bool:
    return ((h is not None and h >= NEAR_H)
            or (y2 is not None and y2 >= NEAR_Y2))


def _appr(hist, ts) -> bool:
    old = [x for x in hist if ts - x[0] <= WIN_S]
    if len(old) < 2:
        return False
    _, h0, y0 = old[0]
    _, h1, y1 = hist[-1]
    if h0 and h0 > 0 and h1 / h0 >= APPROACH_H:
        return True
    return y0 is not None and y1 is not None and y1 - y0 >= APPROACH_Y2


def replay(name: str) -> dict:
    rows = _load(name)
    y_h = load_calib().y_h
    hist: dict[int, list] = {}
    r = {"name": name, "readings": 0, "disagree": 0,
         "phantom": {"allow": 0, "reject_near": 0, "reject_far": 0,
                     "unknown": 0},
         "real": {"allow": 0, "reject_near": 0, "reject_far": 0,
                  "unknown": 0},
         "kept_blocked": 0, "kept_sel": 0, "kept_box": 0,
         "real_rej": [], "gap": []}
    for row in rows:
        dist = {c["id"]: c for c in (row.get("dist") or [])}
        hbb = {c["id"]: c for c in (row.get("car_h") or [])}
        pos = {c["id"]: c for c in (row.get("car_pos") or [])}
        focal = row.get("dgeo_focal")
        cam_h = row.get("dgeo_cam_h")
        if not focal or not cam_h:
            continue
        ts = row["ts_ns"] / 1e9
        for tid, c in dist.items():
            m = c.get("m")
            h = (hbb.get(tid) or {}).get("h")
            cy = (pos.get(tid) or {}).get("cy")
            if m is None or m <= 0 or h is None or cy is None:
                continue
            r["readings"] += 1
            y2 = cy + h / 2.0
            age = c.get("age") or 0.0
            az = (m + (c.get("v") or 0.0) * age / 1000.0
                  if c.get("method") == "RANGE_PROP" else m)
            bz = float(focal) * H_NOM / h
            pz = (float(focal) * float(cam_h) / (y2 - y_h)
                  if y2 - y_h > 1.0 else None)
            if pz is None:
                decision = "unknown"
            else:
                ratio = bz / pz
                if not (LO <= ratio <= HI):
                    r["disagree"] += 1
                    decision = "unknown"
                elif m < LO * min(bz, pz):
                    decision = "reject_near"
                elif m > HI * max(bz, pz):
                    decision = "reject_far"
                else:
                    decision = "allow"
            hh = hist.setdefault(tid, [])
            hh.append((ts, h, y2))
            hist[tid] = [x for x in hh if ts - x[0] <= 0.5]
            near = _near(h, y2)
            appr = _appr(hist.get(tid, []), ts)
            if 0.0 < m < NEAR_M:
                key = "real" if (near or appr) else "phantom"
                r[key][decision] += 1
                if decision != "allow":
                    if key == "real":
                        r["real_rej"].append({
                            "fid": row["fid"], "id": tid,
                            "m": round(m, 2), "az": round(az, 2),
                            "bz": round(bz, 1),
                            "pz": None if pz is None else round(pz, 1),
                            "h": h, "y2": round(y2, 1),
                            "dec": decision})
                    if hbb.get(tid) is not None:
                        r["kept_box"] += 1
                    cand = (row.get("coarse") or {}).get("cand") or []
                    blocked = any(o[3] in ("car", "unknown_car")
                                  or o[1] < 10.0 for o in cand)
                    if blocked:
                        r["kept_blocked"] += 1
                    else:
                        r["gap"].append({
                            "fid": row["fid"], "id": tid,
                            "m": round(m, 2), "az": round(az, 2),
                            "bz": round(bz, 1), "pz": None if pz is None
                            else round(pz, 1), "h": h,
                            "y2": round(y2, 1), "dec": decision,
                            "key": key,
                            "lane": (row.get("coarse") or {}).get("lane")})
                    if (row.get("sel_dbg") or {}).get("car"):
                        r["kept_sel"] += 1
    return r


def report(r: dict) -> None:
    ph = r["phantom"]
    re = r["real"]
    print(f"===== {r['name']} | 读数 {r['readings']} | 框/平面互斥 "
          f"{r['disagree']}（{100.0 * r['disagree'] / r['readings']:.1f}%）")
    print(f"  假近车 {sum(ph.values())}：allow {ph['allow']} / reject_near "
          f"{ph['reject_near']} / reject_far {ph['reject_far']} / unknown "
          f"{ph['unknown']}")
    print(f"  真实近车 {sum(re.values())}：allow {re['allow']} / reject_near "
          f"{re['reject_near']} / reject_far {re['reject_far']} / unknown "
          f"{re['unknown']}")
    print(f"  拒绝/未知时：仍在 2D 框 {r['kept_box']} | 粗层仍占 "
          f"{r['kept_blocked']} | 选择层仍有车 {r['kept_sel']}")
    for e in r.get("gap", [])[:6]:
        print(f"    gap fid {e['fid']} id{e['id']} m={e['m']}"
              f" 锚={e['az']} 框证={e['bz']} 平面={e['pz']}"
              f" h={e['h']} y2={e['y2']} {e['dec']} lane={e['lane']}")
    for e in r.get("real_rej", [])[:8]:
        print(f"    real-rej fid {e['fid']} id{e['id']} m={e['m']}"
              f" 锚={e['az']} 框证={e['bz']} 平面={e['pz']}"
              f" h={e['h']} y2={e['y2']} {e['dec']}")


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
    agg = {"name": "合计", "readings": 0, "disagree": 0,
           "phantom": {"allow": 0, "reject_near": 0, "reject_far": 0,
                       "unknown": 0},
           "real": {"allow": 0, "reject_near": 0, "reject_far": 0,
                    "unknown": 0},
           "kept_blocked": 0, "kept_sel": 0, "kept_box": 0,
         "real_rej": [], "gap": []}
    for n in names:
        r = replay(n)
        report(r)
        for k in ("readings", "disagree", "kept_blocked", "kept_sel",
                  "kept_box"):
            agg[k] += r[k]
        for k in ("phantom", "real"):
            for d in ("allow", "reject_near", "reject_far", "unknown"):
                agg[k][d] += r[k][d]
    print("\n== 合计")
    report(agg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
