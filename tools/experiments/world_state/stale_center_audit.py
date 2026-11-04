"""8789 型事件离线排查（20261102_210341）：标线降级窗口与记忆路心陈旧。

三问：
1. 标线降级段（lane_obs n_lines/kept 低）的发生率与时长；降级时标线
   链的 state/锁定拍数
2. ro（road_offset）冻结段：同值连续拍数（记忆无新鲜度上限的证据）；
   冻结段里 ro 与标线横位的分歧
3. center_override（线为原点）的门：ro_origin=line 的拍 vs 标线可用性
   ——lines 低时 override 是否缺席
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

LOG_ROOT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/logs")


def load(session):
    p = LOG_ROOT / session / "trace" / "speedrush.jsonl"
    rows = [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()]
    segs, start = [], 0
    for i in range(1, len(rows)):
        a, b = rows[i - 1].get("ts_mono_ns"), rows[i].get("ts_mono_ns")
        if a is not None and b is not None and (b - a) / 1e9 > 5.0:
            segs.append(rows[start:i])
            start = i
    segs.append(rows[start:])
    return [s for s in segs if len(s) >= 100]


def runs_of(flags, min_len, dt):
    """连续 True 段 [(起点, 时长s, 长度)]。"""
    out, cur = [], 0
    for i, f in enumerate(flags):
        cur = cur + 1 if f else 0
        if cur == min_len:
            out.append([i - min_len + 1, min_len * dt, min_len])
        elif cur > min_len:
            out[-1][1] += dt
            out[-1][2] += 1
    return out


def seg_report(seg, name, dt):
    n = len(seg)
    lo = [(r.get("lane_obs") or {}) for r in seg]
    n_lines = [d.get("n_lines") for d in lo]
    usable = [d.get("usable") for d in lo]
    kept = [d.get("locked") for d in lo]
    roff = [r.get("ro_off") for r in seg]
    ro_src = [r.get("ro_source") for r in seg]
    ro_origin = [r.get("ro_origin") for r in seg]
    lane_x = [d.get("x") for d in lo]

    print(f"\n=== {name}  {n} 拍 {n*dt:.0f}s ===")
    # 1. 标线降级段：n_lines==1 或 usable=False
    degr = [(nl == 1) or (u is False) for nl, u in zip(n_lines, usable)]
    rs = runs_of(degr, 5, dt)
    print(f"  标线降级拍(n_lines==1或不可用): {sum(degr)}/{n}"
          f" ({sum(degr)/n:.1%})  ≥0.5s 段 {len(rs)} 个"
          f"  最长 {max((r[1] for r in rs), default=0):.1f}s")
    nl_hist = defaultdict(int)
    for v in n_lines:
        nl_hist[v] += 1
    print(f"  n_lines 分布 {dict(sorted((k or -1, v) for k, v in nl_hist.items()))}")
    # 2. ro 冻结段：同值连续（memory 供数时）
    frozen = [o is not None and o == p and s == "memory"
              for o, p, s in zip(roff, roff[1:], ro_src)] + [False]
    rs = runs_of(frozen, 10, dt)
    print(f"  ro 同值冻结拍(memory): {sum(frozen)}/{n} ({sum(frozen)/n:.1%})"
          f"  ≥1s 段 {len(rs)}  最长 {max((r[1] for r in rs), default=0):.1f}s")
    # 冻结段里 ro 与标线横位分歧
    divs = [abs((o or 0) - (x or 0)) for f_, o, x in zip(frozen, roff, lane_x)
            if f_ and o is not None and x is not None]
    if divs:
        divs.sort()
        print(f"  冻结段 |ro−线横位|: 中位 {divs[len(divs)//2]:.2f} 道"
              f"  p10 {divs[len(divs)//10]:.2f}  p90 {divs[len(divs)*9//10]:.2f}"
              f"  >1道占 {sum(d > 1 for d in divs)/len(divs):.1%}")
    # 3. center_override（线为原点）与标线可用性
    ovl = sum(1 for o in ro_origin if o == "line")
    src_hist = defaultdict(int)
    for s2 in ro_src:
        src_hist[s2] += 1
    print(f"  ro_origin=line 拍: {ovl}/{n} ({ovl/n:.1%})"
          f"  ro_source 分布 {dict(sorted(src_hist.items(), key=lambda kv: str(kv[0])))}")
    # 降级拍里 override 缺席率
    d_o = sum(1 for f_, o in zip(degr, ro_origin) if f_ and o != "line")
    d_n = sum(degr)
    if d_n:
        print(f"  降级拍中 override 缺席(ro_origin≠line): {d_o}/{d_n} ({d_o/d_n:.1%})")


def main(session):
    segs = load(session)
    for k, seg in enumerate(segs, 1):
        dt = (seg[-1]["ts_mono_ns"] - seg[0]["ts_mono_ns"]) / 1e9 / len(seg)
        seg_report(seg, f"局{k}", dt)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "20261102_210341")
