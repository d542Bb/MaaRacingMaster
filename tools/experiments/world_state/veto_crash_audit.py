"""闸开首轮实机审计（20261102_210341，4 段对局）。

四问：
1. cor_veto（单向认证）触发频次——离线预期 0~0.3% 拍；触发的道、时长，
   与 route 请求/并线的时序关系
2. 正碰签名（moge z）：min|z|≤2m 且 |x|<1.9m；扫到车后=通过，反弹=嫌疑，
   贴身丢检=未知；鬼影门（横跳>2m/相邻样本）与丢检门（18 拍无 otab）分账
3. 超车直撞 join：签名极小值时刻 ±1.5s 内 sel_why/layer==2（擦边/超车目标）
4. 转向质量：真打舵率（双侧幅度 ≥0.05）、osc_a 峰值段
"""

import os
import json
import sys
from collections import defaultdict
from pathlib import Path

LOG_ROOT = Path(os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs"))


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


def crash_signature(seg):
    """逐车 z 扫描签名。返回 dict：通过/反弹/丢检/鬼影 列表。"""
    series = defaultdict(list)          # id -> [(tick_idx, z, x, fid)]
    for i, r in enumerate(seg):
        for c in (r.get("mcars") or ()):
            if c.get("z") is None or c.get("id") is None:
                continue
            series[c["id"]].append((i, c["z"], c.get("x"), r.get("fid")))
    out = {"pass": [], "bounce": [], "dropout": [], "ghost": []}
    for cid, pts in series.items():
        pts.sort()
        # 贴脸点：|z| 最小且 |x|<1.9（同车道正前）；z<0=最贴脸已在车后
        cand = [(abs(z), i, z, x, f) for i, z, x, f in pts
                if abs(x or 9) < 1.9]
        if not cand:
            continue
        dz, imin, zmin, xmin, fmin = min(cand)
        if dz > 2.0:
            continue
        # 鬼影门：贴脸前 5 拍内横向跳 >2m
        win = [p for p in pts if 0 <= imin - p[0] <= 5]
        xs = [abs(p[2]) for p in win if p[2] is not None]
        if len(xs) >= 2 and max(xs) - min(xs) > 2.0:
            out["ghost"].append((cid, fmin, zmin))
            continue
        if zmin < 0:
            out["pass"].append((cid, fmin, zmin))
            continue
        # 丢检门：贴脸后 18 拍内无该车的观测行
        later = [p for p in pts if p[0] > imin]
        if not later or later[0][0] - imin > 18:
            out["dropout"].append((cid, fmin, zmin))
            continue
        if any(p[1] < -2.0 for p in later):
            out["pass"].append((cid, fmin, zmin))
            continue
        # 反弹：贴脸后 z 回升 ≥1m 且始终没扫到车后
        if max(p[1] for p in later[:10]) - zmin >= 1.0:
            out["bounce"].append((cid, fmin, zmin, xmin))
    return out


def seg_report(seg, name):
    n = len(seg)
    dur = (seg[-1]["ts_mono_ns"] - seg[0]["ts_mono_ns"]) / 1e9
    print(f"\n=== {name}  {n} 拍  {dur:.0f}s  fid "
          f"{next((r['fid'] for r in seg if r.get('fid')), '?')}.."
          f"{next((r['fid'] for r in reversed(seg) if r.get('fid')), '?')} ===")
    # 1. cor_veto
    vt = [(i, r.get("cor_veto"), r.get("fid")) for i, r in enumerate(seg)
          if r.get("cor_veto")]
    lanes = defaultdict(int)
    for _, ls, _f in vt:
        for l in ls:
            lanes[l] += 1
    print(f"  cor_veto 触发拍: {len(vt)}/{n} ({len(vt)/n:.2%})  "
          f"道分布 {dict(sorted(lanes.items()))}")
    for i, ls, f in vt[:12]:
        r = seg[i]
        print(f"    fid={f} lanes={ls} ro_src={r.get('ro_source')} "
              f"sel={r.get('sel_why')} vd={((r.get('plan_gate') or {}).get('verdict')) if isinstance(r.get('plan_gate'), dict) else None} "
              f"why={(r.get('route_why') or (r.get('plan') or {}).get('why') if isinstance(r.get('plan'), dict) else None)}")
    if len(vt) > 12:
        print(f"    ...共 {len(vt)} 拍")
    # 2. 正碰签名
    sig = crash_signature(seg)
    print(f"  签名: 通过{len(sig['pass'])} 反弹{len(sig['bounce'])} "
          f"丢检{len(sig['dropout'])} 鬼影{len(sig['ghost'])}")
    for k in ("bounce", "dropout"):
        for item in sig[k]:
            print(f"    {k}: car{item[0]} fid={item[1]} zmin={item[2]:.2f}")
    # 3. 超车直撞 join：反弹/丢检时刻 ±1.5s 内有没有 pass 目标
    for k in ("bounce", "dropout"):
        for item in sig[k]:
            imin = next((i for i, r in enumerate(seg)
                         if r.get("fid") == item[1]), None)
            if imin is None:
                continue
            t0 = seg[imin]["ts_mono_ns"]
            hits = [(r.get("fid"), r.get("sel_why"), r.get("sel_layer"))
                    for r in seg
                    if r.get("ts_mono_ns") is not None
                    and abs(r["ts_mono_ns"] - t0) / 1e9 <= 1.5
                    and (r.get("sel_why") == "pass" or r.get("sel_layer") == 2)]
            if hits:
                print(f"    [join] car{item[0]} fid={item[1]} {k} "
                      f"±1.5s 内 pass 目标 {len(hits)} 拍: {hits[:4]}")
    # 4. 转向
    flips = [i for i in range(1, n)
             if seg[i - 1].get("steer_norm") is not None
             and seg[i].get("steer_norm") is not None
             and seg[i - 1]["steer_norm"] * seg[i]["steer_norm"] < 0]
    real = [i for i in flips if abs(seg[i - 1]["steer_norm"]) >= 0.05
            and abs(seg[i]["steer_norm"]) >= 0.05]
    print(f"  真打舵: {len(real)} ({len(real)/max(dur,1):.2f}/s)")
    osc = [(r.get("fid"), (r.get("lat_ctrl") or {}).get("osc_a"))
           for r in seg if (r.get("lat_ctrl") or {}).get("osc_a")]
    if osc:
        print(f"  osc_a 非零拍 {len(osc)}  峰值 {max(a for _, a in osc):.3f}")


def main(session):
    segs = load(session)
    print(f"{session}: {len(segs)} 段对局")
    for k, seg in enumerate(segs, 1):
        seg_report(seg, f"局{k}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "20261102_210341")
