# -*- coding: utf-8 -*-
"""R3 归因局逐车归因：mcars/cr_hug 列（20261016 归因局 trace）× 候选网格重构。

回答一个问题：638 量级的 R3 域机会（有锚+在接近+t_ov≤2s）里，漏掉的约 8 成
主体在哪——**可达门**（need+tau+margin > t_ov）、**碰撞判据**（贴身候选
in-path crash −100）、还是**贴身候选在走廊外**（车贴走廊缘，2.1m 横移候选
不在 span 内）。

复算链（与引擎同式同真源，decision.py:803-807）：
  - 候选网格：中心=(−ro_off)×lane_w+executed×lane_w（路心系），宽=候选数×步进
  - 车路心系横向位 = m.x + executed×lane_w（mcars.x 为自车系米）
  - need(dx_m) = 0.3 + 0.4·|dx_m|/lane_w（lane_change_base 0.3 + k 0.4/道）
  - 可达门：t_ov ≥ need+tau+margin（tau 0.13+margin 0.15）；贴身窗 [1.859,2.366]
  - 每车取贴身窗内最近候选为本车贴身候选；窗内无候选=走廊外；
    cr_scores[该候选]≤−99=碰撞杀；不可达=可达门拒；其余=有效

只读 debug 产物，不改闸。"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = ["20261016_222406_p1", "20261016_222451_p2",
          "20261016_222615_p1", "20261016_222700_p2"]

LANE_W = 3.38
BASE, K_PER_LANE = 0.3, 0.4
TAU, MARGIN = 0.13, 0.15
HORIZON = 2.0
HUG_LO, HUG_HI = 1.859, 2.366
ATTR_HALF = 0.75 * LANE_W          # 2.535 m（in-path 归属半宽）
CRASH = -99.0


def need_s(dx_m: float) -> float:
    return BASE + K_PER_LANE * abs(dx_m) / LANE_W


tot = collections.Counter()
cross = collections.Counter()      # 有效车·拍的 cr_hug 交叉验证
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    g = collections.Counter()
    for r in rows:
        cs, ro, executed = r.get("cr_scores"), r.get("ro_off"), \
            r.get("executed_lane")
        if not cs or ro is None or executed is None:
            continue
        shift = executed * LANE_W
        n = len(cs)
        xs = [-ro * LANE_W + shift - n * 0.5 / 2 + (i + 0.5) * 0.5
              for i in range(n)]
        for m in (r.get("mcars") or []):
            g["car"] += 1
            x, t_ov = m.get("x"), m.get("t_ov")
            if x is None:
                g["无锚(x None)"] += 1
                continue
            car_road = x + shift
            if t_ov is None:
                g["估计缺位(t_ov None)"] += 1
                continue
            if t_ov > HORIZON:
                g["horizon窗外(t_ov>2s)"] += 1
                continue
            g["R3域"] += 1
            # 本车贴身候选：贴身窗内离车最近的候选（窗内无=走廊外）
            hcs = [(abs(car_road - xc), i) for i, xc in enumerate(xs)
                   if HUG_LO < abs(car_road - xc) <= HUG_HI]
            if not hcs:
                g["贴身候选走廊外"] += 1
                continue
            _, i = min(hcs)
            if cs[i] is not None and cs[i] <= CRASH:
                g["碰撞判据杀"] += 1
                continue
            dx = abs(xs[i] - shift)     # 贴身候选到 executed 的横移
            if t_ov < need_s(dx) + TAU + MARGIN:
                g["可达门拒"] += 1
                continue
            g["有效(吃到贴身分)"] += 1
            rh = r.get("cr_hug")
            cross["cr_hug>0" if (rh is not None and rh > 0)
                  else "cr_hug=0(分数被别的候选/币盖过)"] += 1
    print(f"== {d} ==")
    for k, v in g.most_common():
        print(f"  {k}: {v}")
    for k, v in g.items():
        tot[k] += v
    print()

print("=== 四条 trace 汇总 ===")
for k, v in tot.most_common():
    print(f"  {k}: {v}")
r3 = tot["R3域"]
if r3:
    print(f"\nR3 域机会 {r3} 车·拍中：")
    for k in ("有效(吃到贴身分)", "可达门拒", "碰撞判据杀", "贴身候选走廊外"):
        print(f"  {k}: {tot.get(k, 0)} ({tot.get(k, 0)/r3*100:.0f}%)")
print("有效车·拍的 cr_hug 交叉验证：", dict(cross))
