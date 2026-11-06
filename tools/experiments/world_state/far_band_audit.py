"""cor_far 远带边界可信度离线审计（2026-11-02，单向认证闸的前置验证）。

主张（单向认证 = 深度作证、标线作图）：变道目标道必须通过深度远带占据
检查。前提是远带边界立得住——本脚本只做离线回放，不动任何生产面。

读数全部来自 trace（不重算感知）：
- cor_far 行 [z0, z1, left_m, right_m, n_ground]（自车系米，车头系 z；
  地面可见范围，含路肩/栏外缓坡，比车道边界宽——depth_geo.ground_far_extents）
- ro_off（道，右正）/ ro_w（道）：主链路框（线为主时原点=线，深度只供边界）
- heading（弯道代理）、ro_source

换算：路框系坐标 = x_m / LW + ro_off（LW=3.5m）；路缘 = ±ro_w/2。

四问之外追加三个判据变体与归因（2026-11-02 首轮回放后加）：
- 变体 A：目标道中心在包络内（不要求 span 全盖）——假否决率对照
- 连续性：负肩差（包络吃进路内）是孤立单拍噪声还是连续段
- 归因：负肩差拍 vs 当拍该侧邻道有车（YOLO 挖除假缩假设）
"""

import os
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

LW = 3.5                    # m/道（lane_obs.LANE_W 同值）
MIN_GROUND_PTS = 30         # depth_geo.FAR_MIN_GROUND_PTS
MACRO_Z0, MACRO_Z1 = 15.0, 46.0   # corridor.FAR_MACRO_Z_LO/HI
Z_MARGIN = 0.5              # 闸草案：目标道半宽外再留的认证余量（道）

LOG_ROOT = Path(os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs"))


def load_rows(session: str):
    p = LOG_ROOT / session / "trace" / "speedrush.jsonl"
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(r)
    # 文件跨局追加+首尾残行：按 >5s 断档切段，保最长段（gate_compare 同口径）
    segs, start = [], 0
    for i in range(1, len(rows)):
        a, b = rows[i - 1].get("ts_mono_ns"), rows[i].get("ts_mono_ns")
        if a is not None and b is not None and (b - a) / 1e9 > 5.0:
            segs.append(rows[start:i])
            start = i
    segs.append(rows[start:])
    return max(segs, key=len) if segs else []


def usable_map(r):
    off, w = r.get("ro_off"), r.get("ro_w")
    if off is None or w is None or not (2.5 <= w <= 7.0):
        return None
    return off, w


def band_rows(r):
    """[(z_mid, left_m, right_m, n)]——只收双侧有读数的带。"""
    out = []
    for b in (r.get("cor_far") or ()):
        if not isinstance(b, list) or len(b) < 5:
            continue
        z0, z1, xl, xr, n = b[:5]
        if xl is None or xr is None:
            out.append((0.5 * (z0 + z1), None, None, n))
        else:
            out.append((0.5 * (z0 + z1), xl, xr, n))
    return out


def quantiles(xs, qs):
    if not xs:
        return {}
    xs = sorted(xs)
    n = len(xs)
    return {q: xs[min(n - 1, max(0, int(q * (n - 1))))] for q in qs}


def audit_session(session: str):
    rows = load_rows(session)
    stats = {
        "ticks": len(rows),
        "map_ok": 0,
        "cor_far_present": 0,
        # per z_mid: 肩差左右、心差(+同拍 ro_off 供相关)、可用带数
        "edge": defaultdict(lambda: {"l": [], "r": [], "c": [], "o": [],
                                     "ok": 0, "dead": 0}),
        "roff": [], "heading": [],
        # 闸模拟：目标道 → [可用拍, 认证过拍]
        "gate": defaultdict(lambda: [0, 0]),
        "gateA": defaultdict(lambda: [0, 0]),  # 变体 A：目标中心在包络内
        "gate_bandloss": [0, 0],   # [宏带可用拍, 宏带全灭拍]
        # 逐条记录：(zmid, side, clip, car_side)——连续性与归因共用
        "recs": [],
        # 变体 C（持续深塌缩）：目标中心在所有可用宏带外包 ≥0.5 道的拍序列
        "seqC": defaultdict(list),
        # clip 拍按路框来源分账
        "clip_src": defaultdict(lambda: [0, 0]),  # src -> [clip拍, 总拍]
    }
    for r in rows:
        m = usable_map(r)
        if m is None:
            continue
        off, w = m
        stats["map_ok"] += 1
        bs = band_rows(r)
        if not any(b[1] is not None for b in bs):
            for d in (-1.0, 1.0):
                stats["seqC"][d].append(False)   # 无证据≠否决证据
            continue
        stats["cor_far_present"] += 1
        stats["roff"].append(off)
        h = r.get("heading")
        if h is not None:
            stats["heading"].append(abs(h))
        macro_usable = 0
        covers = {-1.0: [], 1.0: []}       # 严格：span+margin 全盖
        covers_a = {-1.0: [], 1.0: []}     # 变体 A：目标中心在包络内
        deep = {-1.0: [], 1.0: []}         # 变体 C：逐带中心差 ≥0.5 道在外
        src = r.get("ro_source") or "?"
        for zmid, xl, xr, n in bs:
            key = stats["edge"][round(zmid)]
            if xl is None or n < MIN_GROUND_PTS:
                key["dead"] += 1
                continue
            macro_usable += 1 if MACRO_Z0 <= zmid <= MACRO_Z1 else 0
            lo_road, hi_road = -w / 2.0, w / 2.0
            el, er = xl / LW + off, xr / LW + off      # 包络边，路框系
            key["l"].append(lo_road - el)              # 左肩差（负=吃进路内）
            key["r"].append(er - hi_road)
            key["c"].append(0.5 * (el + er))           # 心差（路心=0）
            key["o"].append(off)
            key["ok"] += 1
            # 该侧近旁有车吗（路框系，2.5 道内；z>12m 的车才可能挖到远带）
            car_l = car_r = False
            for c in (r.get("mcars") or ()):
                if c.get("x") is None or (c.get("z") or 0.0) < 12.0:
                    continue
                cl = c["x"] / LW + off
                if off - 2.5 <= cl < off:
                    car_l = True
                elif off < cl <= off + 2.5:
                    car_r = True
            for side, sh, cs in ((-1, lo_road - el, car_l),
                                 (1, er - hi_road, car_r)):
                stats["recs"].append((zmid, side, sh < 0.0, cs))
                if MACRO_Z0 <= zmid <= 70.0:
                    k2 = stats["clip_src"][src]
                    k2[1] += 1
                    if sh < 0.0:
                        k2[0] += 1
            for d in covers:
                covers[d].append(el <= d - Z_MARGIN and er >= d + Z_MARGIN)
                covers_a[d].append(el <= d <= er)
                deep[d].append((el > d + 0.5) or (er < d - 0.5))
        for d in covers:
            stats["seqC"][d].append(all(deep[d]) if deep[d] else False)
        if any(MACRO_Z0 <= b[0] <= MACRO_Z1 for b in bs):
            stats["gate_bandloss"][0] += 1
            if macro_usable == 0:
                stats["gate_bandloss"][1] += 1
        for d in covers:
            if covers[d]:
                stats["gate"][d][0] += 1
                if all(covers[d]):
                    stats["gate"][d][1] += 1
            if covers_a[d]:
                stats["gateA"][d][0] += 1
                if all(covers_a[d]):
                    stats["gateA"][d][1] += 1
    return stats


def corr(xs, ys):
    n = len(xs)
    if n < 30:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    syy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy)


def runs_and_attr(recs, zmax=70.0):
    """逐带逐侧：clip 率、clip 连续段结构、车归因提升度。

    recs 按插入序（=拍序）切 (zmid, side) 子列即时间序列。
    返回 {zmid: (clipL, clipR, longL, longR, pcar|clipL, pcar|clipR,
                 pcar|allL, pcar|allR)}，None=该带无 clip。"""
    seq = defaultdict(list)
    for zmid, side, clip, cs in recs:
        if zmid <= zmax:
            seq[(zmid, side)].append((clip, cs))
    out = {}
    for zmid in sorted({k[0] for k in seq}):
        row = []
        for side in (-1, 1):
            v = seq.get((zmid, side)) or []
            nclip = sum(c for c, _ in v)
            if not nclip:
                row += [0.0, 0.0, 0.0]
                continue
            runs, cur = [], 0
            for c, _ in v:
                if c:
                    cur += 1
                elif cur:
                    runs.append(cur)
                    cur = 0
            if cur:
                runs.append(cur)
            in_long = sum(r for r in runs if r >= 3)
            row += [nclip / len(v), in_long / nclip,
                    sum(cs for c, cs in v if c) / nclip,
                    sum(cs for _, cs in v) / len(v)]
        out[zmid] = tuple(row)
    return out


def main(sessions):
    for s in sessions:
        st = audit_session(s)
        print(f"\n=== {s}  ticks={st['ticks']} map_ok={st['map_ok']} "
              f"cor_far={st['cor_far_present']} ===")
        n_cf = st["cor_far_present"]
        if not n_cf:
            print("  (无 cor_far 数据)")
            continue
        print(f"  ro_off 分布: {quantiles(st['roff'], [0.1, 0.5, 0.9])}")
        for zmid in sorted(st["edge"]):
            k = st["edge"][zmid]
            tot = k["ok"] + k["dead"]
            if not tot:
                continue
            qs = [0.1, 0.5, 0.9]
            ql, qr, qc = quantiles(k["l"], qs), quantiles(k["r"], qs), \
                quantiles(k["c"], qs)
            rc = corr(k["o"], k["c"])
            rcs = "  n/a" if rc is None else f"{rc:+.2f}"
            print(f"  z~{zmid:5.1f}m 可用{k['ok']:5d}/{tot:5d}"
                  f" ({k['ok'] / tot:5.1%})  "
                  f"左肩差[{ql[0.1]:+.2f} {ql[0.5]:+.2f} {ql[0.9]:+.2f}]  "
                  f"右肩差[{qr[0.1]:+.2f} {qr[0.5]:+.2f} {qr[0.9]:+.2f}]  "
                  f"心差中位{qc[0.5]:+.2f} IQR[{qc[0.1]:+.2f},{qc[0.9]:+.2f}]"
                  f"  心差↔ro_off r={rcs}")
        bl0, bl1 = st["gate_bandloss"]
        if bl0:
            print(f"  宏带全灭拍: {bl1}/{bl0} ({bl1 / bl0:.1%})")
        for d in sorted(st["gate"]):
            av, ok = st["gate"][d]
            if av:
                ava, oka = st["gateA"][d]
                print(f"  闸草案 目标{d:+.0f}道: 严格认证 {ok}/{av}"
                      f" ({ok / av:.1%})  变体A {oka}/{ava} ({oka / ava:.1%})"
                      f"   <- 假否决率 严格{1 - ok / av:.1%}/A{1 - oka / ava:.1%}")
        for zmid, row in runs_and_attr(st["recs"]).items():
            cl, cr, lg_l, lg_r, pcl, pcr, pal, par = row
            print(f"  z~{zmid:4.0f}m clip率 左{cl:5.1%}/右{cr:5.1%}"
                  f"  连续≥3拍占 左{lg_l:5.1%}/右{lg_r:5.1%}"
                  f"  P(车|clip) 左{pcl:.2f}/右{pcr:.2f}"
                  f"  P(车) 左{pal:.2f}/右{par:.2f}")
        for d in sorted(st["seqC"]):
            v = st["seqC"][d]
            inrun = cur = 0
            for x in v:
                cur = cur + 1 if x else 0
                if cur == 3:
                    inrun += 3      # 进段即记整段（≥3 拍）
                elif cur > 3:
                    inrun += 1
            if v:
                print(f"  变体C 目标{d:+.0f}道: 持续深塌缩(≥3拍) {inrun}/{len(v)}"
                      f" ({inrun / len(v):.1%})")
        for s2, (c2, t2) in sorted(st["clip_src"].items(),
                                   key=lambda kv: -kv[1][1]):
            if t2:
                print(f"  clip分账 {s2}: {c2}/{t2} ({c2 / t2:.1%})")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = sorted(p.name for p in LOG_ROOT.iterdir()
                      if p.is_dir() and (p / "trace" / "speedrush.jsonl").exists()
                      and p.name.startswith("2026"))
        args = args[-5:]
    main(args)
