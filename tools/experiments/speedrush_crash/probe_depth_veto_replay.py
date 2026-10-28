"""框高否决的离线回放（2026-10-22，对应待办 5）。

规则只把框高当**相对尺度的一致性证人**，不拿它换算真实距离。回放模拟
"否决 + 保留旧值"的完整状态：

- 轨道、框定义稳定（seen<=1、h>=20px、cy 不大跳）时，把当前读数与上一次
  可信读数比较；
- 若当前读数本身构成威胁（z<8m 或 t_ov<0.5s），且说距离变近 >15%，而框高
  没有按 1/z 同步变大（少于应有增幅的 35%），判 ``DEPTH_SCALE_CONFLICT``，
  本拍不采纳新读数，继续用旧值；远车读数照常采纳，不用旧锚点压真实接近；
- 旧值继续按**原锚点采集时刻**计龄，600ms 到期即作废，交回未知/粗层占用；
- 框高增幅达到应有值 35% 以上时视为可接受（宁可放行真实接近）；低于 35%
  才否决。

验收口径（GPT 评审 2026-10-22）：
- 假近车：原始读数给出威胁（z<8m 或 t_ov<0.5s），但框高 <80px、画面
  也不在靠近（0.25s 窗 h+15% 或 cy+20px）；
- 假近车拦截率：假近车拍里被否决的比例；
- 真实近车误否决率：否决拍里 2D 证人说近（h>=80 或 cy>=500）或框高
  增幅达到应有值一半的比例；
- 决策暴露：被否决且原读数威胁的拍里，选择层/粗层是否同时拿到近距；
- 风险暴露时间：否决期间，同一轨道多久变成 2D 近车（或直接消失）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_depth_veto_replay.py
    ... probe_depth_veto_replay.py 20261022_132510_p1 20261022_132554_p2
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

H_MIN_PX = 20.0        # 框太小时 1px 抖动就是大比例，不比较
SEEN_MAX = 1           # 20Hz 控制 vs 30~40fps 游戏
CY_STILL_PX = 10.0     # 相邻拍纵向几乎没动（关联稳定）
Z_SHRINK = 0.85        # 新读数至少变近 15% 才值得怀疑
VETO_FRAC = 0.35       # 框高增幅低于应有值的 35% → 否决
NEAR_H_PX = 80.0       # 2D 证人：近（名义 1.5m 车高约 <=12m）
NEAR_CY_PX = 500.0
APPROACH_H = 1.15      # 0.25s 窗：在靠近
APPROACH_CY_PX = 20.0
THREAT_M = 8.0
THREAT_T = 0.5
STATIC_FRAC = 0.25      # 静态下限：h < 25% × focal×1.0m/m 即物理不可能
TRUST_TTL_MS = 600.0
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


def _window_approach(hist: list, ts: float) -> bool:
    old = [x for x in hist if ts - x[0] <= APPROACH_WIN_S]
    if len(old) < 2:
        return False
    _, h0, cy0 = old[0]
    _, h1, cy1 = hist[-1]
    if h0 and h0 > 0 and h1 / h0 >= APPROACH_H:
        return True
    return cy0 is not None and cy1 is not None \
        and cy1 - cy0 >= APPROACH_CY_PX


def _threat(c: dict) -> bool:
    """本否决只管「物理上不可信的近距离值」：米制 z<8m。

    t_ov<0.5s 的紧迫拍不在这里拦——它可能只是真实接近；对 10~20m 的
    旧锚点做保留反而会低估接近。"""
    m = c.get("m")
    return m is not None and 0.0 < m < THREAT_M


def replay(name: str) -> dict:
    rows = _load(name)
    hist: dict[int, list] = {}
    trusted: dict[int, dict] = {}
    supp_start: dict[int, float] = {}
    r = {
        "name": name, "threat": 0, "phantom_threat": 0, "phantom_caught": 0,
        "phantom_open": 0, "phantom_handoff": 0,
        "veto": 0, "causes": {}, "guard_release": 0, "guard_miss": [],
        "veto_near": 0, "veto_mid": 0, "veto_far": 0,
        "phantom_time": 0, "phantom_static": 0, "phantom_union": 0,
        "real_time": 0, "real_static": 0, "real_union": 0,
        "exposed_sel": 0, "exposed_coarse": 0,
        "supp": [], "risk": [], "released": 0, "expired": 0, "gone": 0,
        "phantom_events": [],
    }
    for row in rows:
        dist = {c["id"]: c for c in (row.get("dist") or [])}
        hbb = {c["id"]: c for c in (row.get("car_h") or [])}
        pos = {c["id"]: c for c in (row.get("car_pos") or [])}
        ts = row["ts_ns"] / 1e9
        for tid, c in dist.items():
            m = c.get("m")
            if m is None or m <= 0.0:
                continue
            h = (hbb.get(tid) or {}).get("h")
            seen = (hbb.get(tid) or {}).get("seen")
            cy = (pos.get(tid) or {}).get("cy")
            x = (pos.get(tid) or {}).get("x")
            age = c.get("age")
            if h is None:
                continue
            hh = hist.setdefault(tid, [])
            hh.append((ts, h, cy))
            hist[tid] = [y for y in hh if ts - y[0] <= 0.5]
            near = _near2d(h, cy)
            appr_win = _window_approach(hist.get(tid, []), ts)
            foc = row.get("dgeo_focal")
            static = bool(foc and m < THREAT_M
                          and h < STATIC_FRAC * float(foc) * 1.0 / m)
            az = (m + (c.get("v") or 0.0) * (age or 0.0) / 1000.0
                  if c.get("method") == "RANGE_PROP" else m)
            bz = (float(foc) * 1.5 / h) if (foc and h) else None
            anchor_bad = bool(bz and not (0.5 <= az / bz <= 2.0))
            threat = _threat(c)
            if threat:
                r["threat"] += 1
            tr = trusted.get(tid)
            suppressed = False
            expired_tick = False
            stable = False
            hr = expected = None
            if tr is not None:
                t_age = (ts - tr["anchor_ts"]) * 1000.0
                if t_age > TRUST_TTL_MS:
                    trusted.pop(tid, None)
                    r["expired"] += 1
                    tr = None
                    expired_tick = True
            if tr is not None:
                # 横向位移不作为否决门：被超车/自车横移都会让同一轨道的 x 漂移，
                # 而框高比例只要求身份与纵向成像稳定；纵向由 cy 门把关。
                stable = (seen is None or seen <= SEEN_MAX) \
                    and h >= H_MIN_PX and tr["h"] >= H_MIN_PX \
                    and (cy is None or tr.get("cy") is None
                         or abs(cy - tr["cy"]) <= CY_STILL_PX)
                hr = h / tr["h"]
                expected = tr["z"] / m if m > 0 else 1.0
                if stable and expected > 1.0 / Z_SHRINK \
                        and hr < 1.0 + VETO_FRAC * (expected - 1.0) \
                        and threat and anchor_bad:
                    # 只拦“新造的近车”：原读数本身就是威胁才值得否决；
                    # 远车读数照常采纳，避免拿旧锚点压住真实接近。
                    if near or appr_win:
                        r["guard_release"] += 1
                        if threat and not near:
                            r["guard_miss"].append(
                                {"fid": row["fid"], "id": tid, "z": m,
                                 "h": h, "cy": cy,
                                 "z0": tr["z"], "h0": tr["h"]})
                    else:
                        suppressed = True
            if threat:
                real = near or appr_win
                if real:
                    r["real_time"] += int(suppressed)
                    r["real_static"] += int(static)
                    r["real_union"] += int(suppressed or static)
                else:
                    r["phantom_time"] += int(suppressed)
                    r["phantom_static"] += int(static)
                    r["phantom_union"] += int(suppressed or static)
            if suppressed:
                r["veto"] += 1
                if m < 12.0:
                    r["veto_near"] += 1
                elif m < 30.0:
                    r["veto_mid"] += 1
                else:
                    r["veto_far"] += 1
                raw_anchor_ts = ts - (age or 0.0) / 1000.0
                if tr is not None and raw_anchor_ts > tr["anchor_ts"] + 0.05:
                    cause = "anchor_refresh"
                elif c.get("method") == "RANGE_PROP":
                    cause = "propagation"
                else:
                    cause = "other"
                r["causes"][cause] = r["causes"].get(cause, 0) + 1
                supp_start.setdefault(tid, ts)
                if threat:
                    r["exposed_sel"] += _sel_near(row)
                    r["exposed_coarse"] += _coarse_near(row)
                if threat and not near and not appr_win:
                    r["phantom_threat"] += 1
                    r["phantom_caught"] += 1
                    r["phantom_events"].append(
                        {"fid": row["fid"], "id": tid, "z": m, "h": h,
                         "suppressed": True, "cause": cause,
                         "static": static})
                # 否决期间：只看风险，不改可信值
                continue
            # 未否决时记录原因（离线审计：是被护栏放行、框已一致、还是到期）
            if expired_tick:
                reason = "expired"
            elif tr is None:
                reason = "no_trust"
            elif not stable:
                reason = "unstable_box"
            elif expected is None or expected <= 1.0 / Z_SHRINK:
                reason = "not_shrink"
            elif hr is not None and hr >= 1.0 + VETO_FRAC * (expected - 1.0):
                reason = "box_consistent"
            else:
                reason = "accepted"
            # 采纳新读数（或首次建立可信值）
            trusted[tid] = {"z": m, "h": h, "cy": cy, "x": x,
                            "anchor_ts": ts - (age or 0.0) / 1000.0}
            if tid in supp_start:
                t0 = supp_start.pop(tid)
                r["released"] += 1
                r["supp"].append((tid, round(ts - t0, 2), "released"))
            if threat and not near and not appr_win:
                r["phantom_threat"] += 1
                r["phantom_open"] += 1
                if expired_tick:
                    r["phantom_handoff"] += 1
                r["phantom_events"].append(
                    {"fid": row["fid"], "id": tid, "z": m, "h": h,
                     "suppressed": False, "cause": reason,
                     "static": static})
        # 轨道消失：结束否决窗口
        for tid in list(supp_start):
            if tid in dist or tid in hbb:
                continue
            t0 = supp_start.pop(tid)
            r["gone"] += 1
            r["supp"].append((tid, round(ts - t0, 2), "gone"))
            trusted.pop(tid, None)
        # 风险：否决期间变成 2D 近车
        for tid in list(supp_start):
            h = (hbb.get(tid) or {}).get("h")
            cy = (pos.get(tid) or {}).get("cy")
            if _near2d(h, cy):
                t0 = supp_start.pop(tid)
                r["risk"].append((tid, row["fid"], round(ts - t0, 2)))
                trusted.pop(tid, None)
                r["supp"].append((tid, round(ts - t0, 2), "near"))
    return r


def _sel_near(row: dict) -> int:
    cars = (row.get("sel_dbg") or {}).get("car") or []
    return int(any(o[1] is not None and 0.0 < o[1] < THREAT_T for o in cars))


def _coarse_near(row: dict) -> int:
    cand = (row.get("coarse") or {}).get("cand") or []
    return int(any(o[3] == "car" and o[1] < 10.0 for o in cand))


def report(r: dict) -> None:
    print(f"===== {r['name']}")
    catch = (100.0 * r["phantom_caught"] / r["phantom_threat"]
             if r["phantom_threat"] else 0.0)
    print(f"威胁拍 {r['threat']} | 假近车拍 {r['phantom_threat']}"
          f" | 否决 {r['phantom_caught']} / 放行 {r['phantom_open']}"
          f"（其中旧值到期后放行 {r['phantom_handoff']}，拦截率 {catch:.0f}%）")
    for e in r["phantom_events"][:10]:
        flag = "否决" if e["suppressed"] else "放行"
        st = " 静态可判" if e.get("static") else ""
        print(f"  fid {e['fid']} id{e['id']} z={e['z']}m h={e['h']}"
              f" {flag}（{e['cause']}）{st}")
    op = [e for e in r["phantom_events"] if not e["suppressed"]]
    su = [e for e in r["phantom_events"] if e["suppressed"]]
    print(f"静态下限（h<25%×focal×1m/m）可补覆盖放行 "
          f"{sum(1 for e in op if e.get('static'))}/{len(op)}；"
          f"已否决里也命中 {sum(1 for e in su if e.get('static'))}/{len(su)}")
    print(f"否决拍 {r['veto']} | 分类 {r['causes']}"
          f" | 原读数分区 近<12m {r['veto_near']} / 12~30m {r['veto_mid']}"
          f" / >30m {r['veto_far']}")
    print(f"护栏放行（框已近/在靠近）{r['guard_release']}"
          f" | 其中原读数仍威胁且框不近（潜在漏网）{len(r['guard_miss'])}")
    for e in r["guard_miss"][:10]:
        print(f"  fid {e['fid']} id{e['id']} {e['z0']}→{e['z']}m"
              f" h {e['h0']}→{e['h']} cy={e['cy']}")
    print(f"三组对照：假近车拦截 时间 {r['phantom_time']} / 静态 "
          f"{r['phantom_static']} / 联合 {r['phantom_union']}"
          f"（共 {r['phantom_time'] + r['phantom_threat'] - r['phantom_caught']} 条假近车）"
          f" | 同拍真实近车被标 时间 {r['real_time']} / 静态 {r['real_static']}"
          f" / 联合 {r['real_union']}")
    print(f"暴露：选择层近到站 {r['exposed_sel']} | 粗层近占用 "
          f"{r['exposed_coarse']}")
    rel = [s for s in r["supp"] if len(s) == 3]
    if rel:
        ds = [s[1] for s in rel]
        print(f"否决窗口结束 {len(rel)}：释放 {r['released']} / 到期 "
              f"{r['expired']} / 消失 {r['gone']} / 变近 {len(r['risk'])}"
              f" | 窗口 P50={statistics.median(ds):.2f}s max={max(ds):.2f}s")
    if r["risk"]:
        print("  变近延迟：")
        for x in r["risk"][:10]:
            print(f"  id{x[0]} 到 fid {x[1]} 用时 {x[2]}s")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = argv[1:] or ["20261022_132510_p1", "20261022_132554_p2"]
    for n in names:
        report(replay(n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
