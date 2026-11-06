# -*- coding: utf-8 -*-
"""跳变拍钉分支（20261102_223948 实机局）：规划器逐拍归因面首用。

三问：
1. 内部分支分布：lane_follow/depth_follow/depth_reject/hold/blind 各占
   多少；与 exec_branch（决策层）合不吻合
2. 跳变事件：|Δexec| ≥ 0.4 道/拍的拍，落 branch/lgap/lveto/lpok/anch，
   并与链侧 executed_lane、elane 对账——跳变来自重基还是深度修正
3. 闸触发：cor_veto 非空拍与 veto_reason；±1 持续闸的实机触发率

用法::

    python tools/experiments/world_state/branch_pin.py 20261102_223948
"""

import os
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOG_ROOT = Path(os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs"))


def load(session):
    p = LOG_ROOT / session / "trace" / "speedrush.jsonl"
    rows = [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()]
    segs, start = [], 0
    for i in range(1, len(rows)):
        a = rows[i - 1].get("ts_mono_ns")
        b = rows[i].get("ts_mono_ns")
        if a is not None and b is not None and (b - a) / 1e9 > 5.0:
            segs.append(rows[start:i])
            start = i
    segs.append(rows[start:])
    return [s for s in segs
            if sum(1 for r in s if r.get("lat_ctrl")) >= 100]


def lc(r, k):
    v = (r.get("lat_ctrl") or {}).get(k)
    return v


def jumps(seg, key, thr):
    """|Δkey| ≥ thr 的事件 [(i, 前, 后)]。"""
    out = []
    for i in range(1, len(seg)):
        a, b = seg[i - 1].get(key), seg[i].get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and abs(b - a) >= thr:
            out.append((i, a, b))
    return out


def seg_report(seg, name, dt):
    n = len(seg)
    br = Counter(lc(r, "branch") for r in seg)
    eb = Counter(r.get("exec_branch") for r in seg)
    print(f"\n=== {name}  {n} 拍 {n*dt:.0f}s ===")
    print(f"  planner.branch {dict(br)}")
    print(f"  decision.exec_branch {dict(eb)}")
    # 闸触发
    vt = [(i, r.get("cor_veto"), r.get("veto_reason"))
          for i, r in enumerate(seg) if r.get("cor_veto")]
    print(f"  cor_veto 触发拍: {len(vt)} ({len(vt)/n:.2%})"
          f"  样例 {vt[:5]}")
    vr = Counter(r.get("veto_reason") for r in seg if r.get("veto_reason"))
    print(f"  veto_reason {dict(vr)}")
    # 跳变事件：planner exec / 链侧 executed_lane
    je = jumps(seg, lambda r: lc(r, "exec"), 0.4)
    jl = jumps(seg, lambda r: r.get("executed_lane"), 0.4)
    print(f"  跳变 |Δexec|≥0.4: {len(je)}   |Δ链executed|≥0.4: {len(jl)}")
    shown = set()
    for i, a, b in (je + jl):
        if i in shown or i - 1 in shown:
            continue
        shown.add(i)
        r0, r1 = seg[i - 1], seg[i]
        print(f"  -- 拍{i} fid {r0.get('fid')}→{r1.get('fid')}")
        print(f"     exec {a if key else lc(r0,'exec'):.3f} →"
              f" {b if key else lc(r1,'exec'):.3f}"
              f"  elane {r0.get('executed_lane')}→{r1.get('executed_lane')}")
        for k in ("branch", "lgap", "lveto", "lpok", "anch", "rebase"):
            print(f"     {k}: {lc(r0, k)} → {lc(r1, k)}")
        for k in ("exec_branch", "ro_off", "ro_source", "ro_origin",
                  "plan", "cor_veto"):
            print(f"     {k}: {r0.get(k)} → {r1.get(k)}")
    # 分数面
    hs = [r.get("hud_score") for r in seg if r.get("hud_score") is not None]
    if hs:
        print(f"  hud_score {hs[0]} → {hs[-1]}  max {max(hs)}")
    vv = [r.get("v") for r in seg if isinstance(r.get("v"), (int, float))]
    if vv:
        print(f"  v<1 拍: {sum(1 for x in vv if x < 1)}/{len(vv)}"
              f"  v min {min(vv)}")


def main(session):
    segs = load(session)
    for k, seg in enumerate(segs, 1):
        dt = (seg[-1]["ts_mono_ns"] - seg[0]["ts_mono_ns"]) / 1e9 / len(seg)
        seg_report(seg, f"局{k}", dt)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "20261102_223948")
