"""占用状态机离线审计（2026-10-22，GPT 状态机评审的下一步）。

只审计“近车/近威胁”的占用（远车距离不可信不是本问题）：

- 距离：allow / reject_near / reject_far / unknown（框证与平面互斥）；
  不可信时停止发布该距离，但**不清空占用**。
- 占用：目标仍在 2D 框、且未获得离道证据时保持；释放只认
  （a）横向离道 >1.7m，（b）已掠过（t_ov<=0 或 z<0 或 pass 事件），
  （c）2D 丢失 >500ms（有界兜底，单独记为 uncertain）。
  未知占用超过 1.5s 记一次 cap（有界保守，不自动判空）。

粗层无占用的拍在释放前分类：已离道 / 已掠过 / 2D 丢失 / 横向未知 / 漏判。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_occupancy_state.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

H_NOM = 1.5
NEAR_M = 8.0
LO, HI = 0.5, 2.0
LANE_IN = 1.2
LANE_OUT = 1.7
LOST_MS = 500.0
CAP_MS = 1500.0
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


def _decision(row, c, h, cy, y_h) -> str:
    focal = row.get("dgeo_focal")
    cam_h = row.get("dgeo_cam_h")
    m = c.get("m")
    if focal is None or cam_h is None or m is None:
        return "unknown"
    y2 = cy + h / 2.0
    bz = float(focal) * H_NOM / h
    pz = (float(focal) * float(cam_h) / (y2 - y_h)
          if y2 - y_h > 1.0 else None)
    if pz is None or not (LO <= bz / pz <= HI):
        return "unknown"
    if m < LO * min(bz, pz):
        return "reject_near"
    if m > HI * max(bz, pz):
        return "reject_far"
    return "allow"


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
    lane_w = load_calib().lane_w_m
    y_h = load_calib().y_h
    st: dict[int, dict] = {}
    hist: dict[int, list] = {}
    r = {"name": name, "unknown_eps": [], "real_eps": [],
         "cap": 0, "lost_release": 0, "uncertain_release": 0,
         "uncertain": [],
         "gap": {"departed": 0, "passed": 0, "ambiguous": 0,
                 "lateral_unknown": 0, "missed": 0},
         "phantom_ticks": 0, "phantom_events": 0, "real_reject_ticks": 0}
    last_ts = 0.0
    for row in rows:
        ts = row["ts_ns"] / 1e9
        last_ts = ts
        dist = {c["id"]: c for c in (row.get("dist") or [])}
        hbb = {c["id"]: c for c in (row.get("car_h") or [])}
        pos = {c["id"]: c for c in (row.get("car_pos") or [])}
        mc = {c["id"]: c for c in (row.get("mcars") or [])}
        passed_ids = {e["id"] for e in (row.get("passes") or [])}
        ego_x = row.get("executed_lane")
        tids = set(dist) | set(hbb)
        for tid in tids:
            c = dist.get(tid)
            h = (hbb.get(tid) or {}).get("h")
            cy = (pos.get(tid) or {}).get("cy")
            x = (pos.get(tid) or {}).get("x")
            m = None if c is None else c.get("m")
            if h is None:
                continue
            hh = hist.setdefault(tid, [])
            hh.append((ts, h, cy))
            hist[tid] = [y for y in hh if ts - y[0] <= 0.5]
            y2 = cy + h / 2.0
            near2d = _near(h, y2)
            appr = _appr(hist.get(tid, []), ts)
            near_metric = m is not None and 0.0 < m < NEAR_M
            threat_near = near_metric or near2d or appr
            dec = ("unknown" if c is None or cy is None
                   else _decision(row, c, h, cy, y_h))
            lat = (abs(x - ego_x) * lane_w
                   if x is not None and ego_x is not None else None)
            zm = (mc.get(tid) or {}).get("z")
            tov = (mc.get(tid) or {}).get("t_ov")
            passed = (tid in passed_ids
                      or (zm is not None and zm < 0.0)
                      or (tov is not None and tov <= 0.0))
            departed = lat is not None and lat > LANE_OUT
            s = st.setdefault(tid, {"last": ts, "unknown_since": None,
                                    "occ": True, "real": False,
                                    "last_lat": None, "last_near": False})
            s["last"] = ts
            s["last_lat"] = lat if lat is not None else s["last_lat"]
            s["last_near"] = near2d or appr
            # 粗层无占用分类（在释放前）
            if threat_near and dec != "allow":
                cand = (row.get("coarse") or {}).get("cand") or []
                blocked = any(o[3] in ("car", "unknown_car") or o[1] < 10.0
                              for o in cand)
                if not blocked:
                    if departed:
                        r["gap"]["departed"] += 1
                    elif passed:
                        r["gap"]["passed"] += 1
                    elif lat is None:
                        r["gap"]["lateral_unknown"] += 1
                    elif lat <= LANE_IN:
                        r["gap"]["missed"] += 1
                    else:
                        r["gap"]["ambiguous"] += 1
            if passed or departed:
                if s["unknown_since"] is not None:
                    r["unknown_eps"].append(
                        (tid, row["fid"],
                         round(ts - s["unknown_since"], 3),
                         "passed" if passed else "departed"))
                    if s["real"]:
                        r["real_eps"].append((tid, row["fid"],
                                              round(ts - s["unknown_since"], 3)))
                s["occ"] = False
                s["unknown_since"] = None
                s["real"] = False
                continue
            s["occ"] = True
            if threat_near and dec != "allow":
                if s["unknown_since"] is None:
                    s["unknown_since"] = ts
                if near2d or appr:
                    s["real"] = True
                    r["real_reject_ticks"] += 1
                else:
                    r["phantom_ticks"] += 1
                    if s["unknown_since"] == ts:
                        r["phantom_events"] += 1
                if (ts - s["unknown_since"]) * 1000.0 > CAP_MS:
                    r["cap"] += 1
            elif dec == "allow" and s["unknown_since"] is not None:
                r["unknown_eps"].append(
                    (tid, row["fid"],
                     round(ts - s["unknown_since"], 3), "accepted"))
                if s["real"]:
                    r["real_eps"].append((tid, row["fid"],
                                          round(ts - s["unknown_since"], 3)))
                s["unknown_since"] = None
                s["real"] = False
        # 2D 丢失 >500ms：有界兜底释放，单独记 uncertain
        for tid, s in st.items():
            if tid in tids or not s["occ"]:
                continue
            if (last_ts - s["last"]) * 1000.0 > LOST_MS:
                if s["unknown_since"] is not None:
                    r["unknown_eps"].append(
                        (tid, row["fid"],
                         round(last_ts - s["unknown_since"], 3), "lost"))
                    r["lost_release"] += 1
                    if s["last_near"] and (s["last_lat"] is None
                                           or s["last_lat"] <= LANE_IN):
                        r["uncertain_release"] += 1
                        r["uncertain"].append(
                            (tid, row["fid"],
                             round(last_ts - s["unknown_since"], 3),
                             None if s["last_lat"] is None
                             else round(s["last_lat"], 2)))
                s["occ"] = False
                s["unknown_since"] = None
                s["real"] = False
    return r


def report(r: dict) -> None:
    ue = r["unknown_eps"]
    ds = [e[2] for e in ue]
    print(f"===== {r['name']} | 近车未知窗口 {len(ue)}"
          f" | 假近车拍 {r['phantom_ticks']}/进入 {r['phantom_events']} 次"
          f" | 真实近车未知拍 {r['real_reject_ticks']}")
    if ds:
        print(f"  未知占用时长 P50={statistics.median(ds):.2f}s "
              f"P90={sorted(ds)[int(len(ds) * 0.9)]:.2f}s max={max(ds):.2f}s"
              f" | cap>{CAP_MS / 1000:.1f}s {r['cap']} 次"
              f" | 丢失释放 {r['lost_release']}（其中最后仍近/在道 "
              f"{r['uncertain_release']}）")
        for e in r.get("uncertain", []):
            if len(e) == 5:
                print(f"    uncertain 释放 {e[0]} id{e[1]} fid {e[2]} "
                      f"{e[3]}s 最后横向 {e[4]}")
            else:
                print(f"    uncertain 释放 id{e[0]} fid {e[1]} "
                      f"{e[2]}s 最后横向 {e[3]}")
    print(f"  粗层无占用分类 {r['gap']}")


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
    agg = {"unknown_eps": [], "real_eps": [], "cap": 0,
           "lost_release": 0, "uncertain_release": 0, "uncertain": [],
           "phantom_ticks": 0, "phantom_events": 0, "real_reject_ticks": 0,
           "gap": {"departed": 0, "passed": 0, "ambiguous": 0,
                   "lateral_unknown": 0, "missed": 0}}
    for n in names:
        r = replay(n)
        report(r)
        for k in ("cap", "lost_release", "uncertain_release",
                  "phantom_ticks", "phantom_events", "real_reject_ticks"):
            agg[k] += r[k]
        agg["unknown_eps"] += r["unknown_eps"]
        agg["real_eps"] += r["real_eps"]
        agg["uncertain"] += [(n, *e) for e in r["uncertain"]]
        for k, v in r["gap"].items():
            agg["gap"][k] += v
    print("\n== 合计")
    agg["name"] = "合计"
    report(agg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
