# -*- coding: utf-8 -*-
"""离线影子：粗层净空窗内的纵深盲区（撞车复盘实验 8）。

只读控制 trace，不 import 生产代码、不发控制指令。

口径对齐 coarse.choose_lane：cam_z_datum_m=5.0（depth_geo 常量，
decision 以它覆盖 CoarseParams 默认值）、lookahead_m=24 → 车在 z ≤ 29m 时
本应截断该道净空；粗层看得见车的唯一纵向输入是 z。

按全局拍轴重建每车状态（不跳过该车未出现的拍）：
  known      z 有效（粗层可截断）
  blind_vis  2D 轨仍存活、z 缺失（轨在世界模型里，含 grace 期）
  gone       2D 轨与 z 都缺（车仍可能在窗内——追尾案的末段）

为排除“已经并排/通过”的正常盲拍，只统计「最近一次已知 z 在 8~29m」的拍：
z 已知且已贴近的车不再计入危险区。每车报危险区内盲拍占比与最长连续
盲拍，以及最后一次已知 z 之后到消失/通过之间的盲尾长度。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

CAM_DATUM_M = 5.0     # depth_geo.CAM_Z_DATUM_M（decision 以它覆盖 CoarseParams 默认值）
LOOKAHEAD_M = 24.0
HORIZON_M = CAM_DATUM_M + LOOKAHEAD_M
DANGER_Z_M = 8.0
TAIL_S = 0.5


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
    ticks = []
    ids: set[int] = set()
    for i, r in enumerate(rows):
        zmap = {m["id"]: m.get("z") for m in (r.get("mcars") or [])}
        for d in r.get("dist") or []:
            if zmap.get(d["id"]) is None and d.get("m") is not None:
                zmap[d["id"]] = d["m"]
        why = {d["id"]: d.get("why") for d in (r.get("dist") or [])}
        vis = {c["id"] for c in (r.get("car_h") or [])}
        rel = {c["id"]: c.get("rel") for c in (r.get("car_pos") or [])}
        passes = {p["id"]: p for p in (r.get("passes") or [])}
        ids |= set(zmap) | vis | set(why)
        ticks.append({"i": i, "t": t[i], "fid": r.get("fid"), "z": zmap,
                      "why": why, "vis": vis, "rel": rel, "pass": passes})
    out = []
    for cid in ids:
        enter = next((k for k, row in enumerate(ticks)
                      if row["z"].get(cid) is not None
                      and 0.0 < row["z"][cid] <= HORIZON_M), None)
        if enter is None:
            continue
        passed = next((k for k in range(enter + 1, len(ticks))
                       if cid in ticks[k]["pass"]), None)
        pres = [k for k in range(enter, len(ticks))
                if cid in ticks[k]["vis"] or row_z(ticks[k], cid) is not None]
        if not pres:
            continue
        if passed is not None:
            end = passed
            end_why = "pass"
        else:
            last = pres[-1]
            end = last
            while (end + 1 < len(ticks)
                   and ticks[end + 1]["t"] - ticks[last]["t"] <= TAIL_S):
                end += 1
            end_why = "lost_no_pass"
        # 危险区：最近一次已知 z ≥ 8m 的拍
        cnt = {"known": 0, "blind_vis": 0, "gone": 0}
        run = {"blind_vis": 0, "gone": 0}
        best = {"blind_vis": 0, "gone": 0}
        last_z = None
        last_z_t = None
        last_pres_t = None
        for k in range(enter, end + 1):
            row = ticks[k]
            z = row["z"].get(cid)
            if z is not None:
                last_z, last_z_t = z, row["t"]
            if cid in row["vis"] or z is not None:
                last_pres_t = row["t"]
            # 危险区：车仍在净空窗内（8 ≤ 最近已知 z ≤ 25.8）
            if last_z is None or not DANGER_Z_M <= last_z <= HORIZON_M:
                run = {"blind_vis": 0, "gone": 0}
                continue
            st = ("known" if z is not None
                  else "blind_vis" if cid in row["vis"] else "gone")
            cnt[st] += 1
            if st in run:
                run[st] += 1
                best[st] = max(best[st], run[st])
            else:
                run = {"blind_vis": 0, "gone": 0}
        tail_after_z = ticks[end]["t"] - (last_z_t or ticks[end]["t"])
        gap_sight = (last_pres_t or ticks[end]["t"]) - (last_z_t or ticks[end]["t"])
        # 视野内口径（不含“消失后 0.5s”的补尾）
        end_view = pres[-1]
        cnt_view = {"known": 0, "blind_vis": 0, "gone": 0}
        last_z_v = None
        for k in range(enter, end_view + 1):
            row = ticks[k]
            z = row["z"].get(cid)
            if z is not None:
                last_z_v = z
            if last_z_v is None or not DANGER_Z_M <= last_z_v <= HORIZON_M:
                continue
            cnt_view["known" if z is not None
                     else "blind_vis" if cid in row["vis"] else "gone"] += 1
        whys = {}
        for k in range(enter, end + 1):
            w = ticks[k]["why"].get(cid)
            if w and ticks[k]["z"].get(cid) is None:
                whys[w] = whys.get(w, 0) + 1
        out.append({
            "cid": cid, "enter_fid": ticks[enter]["fid"],
            "enter_z": ticks[enter]["z"][cid],
            "end_fid": ticks[end]["fid"], "end_why": end_why,
            "last_z": last_z, "cnt": cnt, "cnt_view": cnt_view,
            "best": best,
            "tail": tail_after_z, "gap_sight": gap_sight, "whys": whys,
            "dur": ticks[end]["t"] - ticks[enter]["t"],
        })
    return out


def row_z(row, cid):
    return row["z"].get(cid)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--lost", action="store_true", help="只列未通过即消失的车")
    args = ap.parse_args()
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        out = analyze(path)
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        tot = {"known": 0, "blind_vis": 0, "gone": 0}
        totv = {"known": 0, "blind_vis": 0, "gone": 0}
        for x in out:
            for k in tot:
                tot[k] += x["cnt"][k]
                totv[k] += x["cnt_view"][k]
        allt = sum(tot.values()) or 1
        allv = sum(totv.values()) or 1
        print(f"   进过 {HORIZON_M:.1f}m 窗的车 {len(out)}；危险区"
              f"（8≤最近 z≤{HORIZON_M:.1f}）拍 {allt}："
              f"known {tot['known']} ({tot['known']*100/allt:.0f}%) "
              f"blind_vis {tot['blind_vis']} ({tot['blind_vis']*100/allt:.0f}%) "
              f"gone {tot['gone']} ({tot['gone']*100/allt:.0f}%)")
        print(f"   同上、只算视野内（不含消失后补尾）{allv} 拍："
              f"known {totv['known']} ({totv['known']*100/allv:.0f}%) "
              f"blind_vis {totv['blind_vis']} ({totv['blind_vis']*100/allv:.0f}%) "
              f"gone {totv['gone']} ({totv['gone']*100/allv:.0f}%)")
        p90b = _pct([x["best"]["blind_vis"] for x in out], .9)
        p90g = _pct([x["best"]["gone"] for x in out], .9)
        print(f"   最长连续盲拍 P50/P90：blind_vis "
              f"{_pct([x['best']['blind_vis'] for x in out], .5)}/{p90b}，"
              f"gone {_pct([x['best']['gone'] for x in out], .5)}/{p90g}")
        lost = [x for x in out if x["end_why"] == "lost_no_pass"]
        zges = [x for x in out if x["last_z"] is not None and x["last_z"] >= DANGER_Z_M]
        print(f"   通过事件收尾 {len(out)-len(lost)}，未通过即消失 {len(lost)}；"
              f"消失时最后已知 z≥8m 的车 {len(zges)}")
        tails = [x["tail"] for x in lost
                 if x["last_z"] is not None and x["last_z"] >= DANGER_Z_M]
        gaps = [x["gap_sight"] for x in lost
                if x["last_z"] is not None and x["last_z"] >= DANGER_Z_M]
        if gaps:
            print(f"   危险消失车：最后 z→最后一次 2D 看见 中位 {_pct(gaps,.5):.2f}s "
                  f"P90 {_pct(gaps,.9):.2f}s；其后无观测至少 0.5s")
        if tails:
            print(f"   危险消失车：最后 z 之后盲尾（含 0.5s 尾窗）中位 "
                  f"{_pct(tails,.5):.2f}s P90 {_pct(tails,.9):.2f}s 最大 {max(tails):.2f}s")
        for x in sorted(out, key=lambda y: (-y["best"]["gone"], -y["tail"])):
            if args.lost and x["end_why"] != "lost_no_pass":
                continue
            if not (x["cnt"]["blind_vis"] or x["cnt"]["gone"]):
                continue
            print(f"   car{x['cid']}: 进窗 fid{x['enter_fid']} z={x['enter_z']:.1f}"
                  f" → {x['end_why']} fid{x['end_fid']}"
                  f" | known/blind/gone {x['cnt']['known']}/"
                  f"{x['cnt']['blind_vis']}/{x['cnt']['gone']}"
                  f" | 连续盲 {x['best']['blind_vis']}/{x['best']['gone']}"
                  f" | 最后z {x['last_z']} 盲尾 {x['tail']:.2f}s | {x['whys']}")


if __name__ == "__main__":
    main()
