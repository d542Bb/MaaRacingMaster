# -*- coding: utf-8 -*-
"""离线影子：冲突车的米制距离漏斗（撞车复盘实验 4）。

只读控制 trace，不 import 生产代码、不发控制指令。回答一个问题：
**2D 已经检出，为什么迟迟没有可供决策使用的米制距离？**

按代码里的真实原因分类（tracking.DistanceReason）：
NO_ANCHOR_YET / CLUSTER_SWITCH / ASSOC_FAIL / TRACK_LOST / EXPIRED。
质量门取代码常量：锚龄 600ms、簇跳变 20m、翻跳门 2m + 45m/s×dt、映射窗 18 拍。

漏斗阶段：
  T1  首次 2D 检出（car_h）
  Ta  首次出现距离状态（dist，任意 src）
  Tm  首次得到米制距离（m 非空）
  Tv  首次得到相对速度（v 非空 → t_ov 可用）
  Td  首次决策可用（mcars 同时有 x / z / t_ov）

决策“是否被消费”无法按 id 观测（选择对象不带 id），本探针只报到 Td 为止。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

DATUM_M = 5.0
ENVELOPE_M = 2.3
REASONS = ("NO_ANCHOR_YET", "CLUSTER_SWITCH", "ASSOC_FAIL",
           "TRACK_LOST", "EXPIRED")


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def _index(rows):
    """逐 id 建立各阶段的首见记录与原因计数。"""
    t = [0.0]
    for r in rows[1:]:
        t.append(t[-1] + (r.get("dt") or 0.05))
    acc: dict[int, dict] = {}
    conflict: set[int] = set()
    asheets = []
    for i, r in enumerate(rows):
        fid = r["fid"]
        for c in r.get("car_h") or []:
            a = acc.setdefault(c["id"], {})
            a.setdefault("t1", (i, t[i], fid, c.get("h"), c.get("conf")))
        for c in r.get("car_pos") or []:
            if c.get("x") is not None and abs(c["x"]) * 3.38 <= ENVELOPE_M:
                m = [x for x in (r.get("mcars") or []) if x["id"] == c["id"]]
                if m and m[0].get("z") is not None and m[0]["z"] <= 18.0:
                    conflict.add(c["id"])
        for m in r.get("mcars") or []:
            if (m.get("z") is not None and m.get("x") is not None
                    and m["z"] <= 18.0 and abs(m["x"]) <= ENVELOPE_M):
                conflict.add(m["id"])
        for d in r.get("dist") or []:
            a = acc.setdefault(d["id"], {})
            a.setdefault("ta", (i, t[i], fid, d.get("why")))
            why = d.get("why")
            if why in REASONS:
                a.setdefault("reasons", []).append((t[i], why))
            if d.get("m") is not None and "tm" not in a:
                a["tm"] = (i, t[i], fid, d["m"], d.get("src"),
                           d.get("method"), d.get("age"))
            if d.get("v") is not None and "tv" not in a:
                a["tv"] = (i, t[i], fid, d["m"], d["v"], d.get("age"))
            if d.get("m") is not None:
                a.setdefault("z_drops", [])
                zs = [x for x in (r.get("mcars") or []) if x["id"] == d["id"]]
                if zs and "last_z" in a:
                    a["z_drops"].append(abs(zs[0]["z"] - a["last_z"]))
                if zs:
                    a["last_z"] = zs[0]["z"]
        if r.get("asheet") is not None:
            asheets.append((t[i], r["asheet"].get("entries"),
                            r["asheet"].get("mapped")))
        for m in r.get("mcars") or []:
            a = acc.setdefault(m["id"], {})
            a.setdefault("series", []).append(
                (t[i], m.get("x"), m.get("z"), m.get("v"), m.get("t_ov")))
            if (m.get("x") is not None and m.get("z") is not None
                    and m.get("t_ov") is not None and "td" not in a):
                a["td"] = (i, t[i], fid, m["z"], m["x"], m["t_ov"])
    return acc, conflict, t, asheets


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--min-delay", type=float, default=0.0,
                    help="只打印 Td−T1 超过此秒数的车")
    ap.add_argument("--focus", type=int, default=None)
    ap.add_argument("--endpoint", choices=("z", "tov"), default="z",
                    help="决策可用终点：z=首个米制纵深（粗层真正消费的量）；"
                         "tov=首个 t_ov（该字段当前无消费者，仅存档对照）")
    args = ap.parse_args()
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        rows = [json.loads(line) for line in open(path, encoding="utf-8")]
        acc, conflict, t, asheets = _index(rows)
        if args.endpoint == "z":
            for a in acc.values():
                tm = a.get("tm")
                a["td"] = (None if not tm else
                           (tm[0], tm[1], tm[2], tm[3], None, None))
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        print(f"   车 {len(acc)}  冲突车 {len(conflict)}")
        for cid in sorted(conflict):
            a = acc.get(cid, {})
            t1, tm, tv, td = a.get("t1"), a.get("tm"), a.get("tv"), a.get("td")
            if not t1:
                continue
            delay = (td[1] - t1[1]) if (td and t1) else None
            if args.min_delay and (delay is None or delay < args.min_delay):
                continue

            def _reasons(lo, hi):
                cnt = {}
                for tt, why in a.get("reasons", []):
                    if lo <= tt < hi:
                        cnt[why] = cnt.get(why, 0) + 1
                return ",".join(f"{k}:{v}" for k, v in cnt.items()) or "—"

            pre = _reasons(t1[1], tm[1]) if tm else _reasons(t1[1], float("inf"))
            mid = _reasons(tm[1], td[1]) if (tm and td) else "—"
            zw = [x for x in asheets if tm and t1[1] <= x[0] < tm[1]]
            if zw:
                e0 = sum(1 for _t, e, _m in zw if e == 0)
                m0 = sum(1 for _t, e, m in zw if e and m == 0)
                ms = sum(1 for _t, e, m in zw if e and m)
                ash = f"深度清单 {len(zw)} 新证据拍（空 {e0} / 有条目未映射 {m0} / 有映射 {ms}）"
            else:
                ash = "深度清单 —"
            drops = a.get("z_drops") or []
            jump = f"{max(drops):.1f}" if drops else "—"
            print(f"   car{cid}: T1 fid={t1[2]} h={t1[3]} conf={t1[4]}"
                  f" | Tm={None if tm is None else f'fid{tm[2]} {tm[3]:.1f}m {tm[4]} age{tm[6]}'}"
                  f" | Tv={None if tv is None else f'fid{tv[2]} v={tv[4]:.1f}'}"
                  f" | Td={None if td is None else f'fid{td[2]} {td[3]:.1f}m'}"
                  f" | 延迟={None if delay is None else f'{delay:.2f}s'}"
                  f" | 锚前[{pre}] 锚后[{mid}] | 最大跳变 {jump}m")
            print(f"      {ash}")
        # 归因汇总（每车一个主类别，规则见下）
        cats = {}
        cat_delays = {}
        cat4_missing = {}
        for cid in sorted(conflict):
            a = acc.get(cid, {})
            t1, tm, td = a.get("t1"), a.get("tm"), a.get("td")
            if not t1:
                continue
            if not tm:
                cat = "1 从未映射成功"
            elif not td:
                cat = "4 有距离但未决策可用"
            else:
                delay = td[1] - t1[1]
                pre_gap = tm[1] - t1[1]
                mid_gap = td[1] - tm[1]
                pre = [w for tt, w in a.get("reasons", []) if t1[1] <= tt < tm[1]]
                mid = [w for tt, w in a.get("reasons", []) if tm[1] <= tt < td[1]]
                zw = [x for x in asheets if t1[1] <= x[0] < tm[1]]
                empty_all = bool(zw) and all(e == 0 for _t, e, _m in zw)
                if delay < 0.5:
                    cat = "0 正常（<0.5s）"
                elif pre_gap >= mid_gap and pre_gap >= 0.3:
                    if "ASSOC_FAIL" in pre:
                        cat = "2 关联失败（条目未映射）"
                    elif empty_all:
                        cat = "1b 无深度锚条目（供数缺口）"
                    else:
                        cat = "5 证据不足"
                else:
                    if "CLUSTER_SWITCH" in mid or "EXPIRED" in mid:
                        cat = "3a 锚点被质量门丢弃"
                    elif "TRACK_LOST" in mid:
                        cat = "3b 二维跟踪丢失（锚点保留期不够）"
                    elif mid_gap >= 0.3:
                        cat = "4 有距离但未决策可用"
                    else:
                        cat = "5 证据不足"
            cats[cat] = cats.get(cat, 0) + 1
            if td:
                cat_delays.setdefault(cat, []).append(td[1] - t1[1])
            if cat.startswith("4") and tm:
                lo = tm[1]
                hi = td[1] if td else float("inf")
                miss = {"速度缺失": 0, "横向缺失": 0, "到站时间缺失": 0}
                for tt, x, z, v, ov in a.get("series", []):
                    if lo <= tt < hi:
                        if v is None:
                            miss["速度缺失"] += 1
                        if x is None:
                            miss["横向缺失"] += 1
                        if ov is None:
                            miss["到站时间缺失"] += 1
                dom = max(miss, key=miss.get)
                cat4_missing[dom] = cat4_missing.get(dom, 0) + 1
        total = sum(cats.values())
        if total:
            print("   归因（每车一个主类别）：")
            for k in sorted(cats):
                ds = cat_delays.get(k, [])
                med = f" 延迟中位 {sorted(ds)[len(ds)//2]:.2f}s" if ds else ""
                print(f"     {k}: {cats[k]}/{total} ({cats[k]*100/total:.0f}%){med}")
            if cat4_missing:
                tot4 = sum(cat4_missing.values())
                parts = "，".join(f"{k} {v}/{tot4}" for k, v in cat4_missing.items())
                print(f"     类别 4 的主导缺口：{parts}")
        # 聚合
        delays = [a["td"][1] - a["t1"][1] for cid, a in acc.items()
                  if cid in conflict and a.get("t1") and a.get("td")]
        no_tm = [cid for cid in conflict if "tm" not in acc.get(cid, {})]
        no_td = [cid for cid in conflict if "td" not in acc.get(cid, {})]
        if delays:
            delays.sort()
            print(f"   汇总：Td−T1 中位 {delays[len(delays)//2]:.2f}s "
                  f"P90 {delays[int(len(delays)*0.9)]:.2f}s  "
                  f"无米制 {len(no_tm)}  无决策可用 {len(no_td)}")
        if args.focus is not None:
            a = acc.get(args.focus)
            print(f"   焦点 car{args.focus}: {json.dumps(a, ensure_ascii=False)[:600]}")


if __name__ == "__main__":
    main()
