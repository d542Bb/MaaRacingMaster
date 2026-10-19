# -*- coding: utf-8 -*-
"""感知层「证据闪」审计（20261017 维护者指认：金币/车速/跳变同类错误不少）。

一局里量化五类闪，逐拍口径（trace 为影子，raw 帧为 ground truth 抽样）：
  ① 车仪器 src 翻面：ANCHOR↔UNKNOWN 的切换率与弃权原因分布（mcars src/reason）
  ② 锚点节奏：锚龄分布（P50/P90/最大）——锚点清单的 cadence
  ③ 币组可见闪：n_groups 0↔>0 翻面率
  ④ 收益报告缺席：cr_best=None 拍占比（走廊缺席/黑视）
  ⑤ 走廊缺席：cor_w=None 拍占比
只读 debug 产物，不改生产。
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
SESS = ["20261017_193629_p1", "20261017_193714_p2"]


def _p50(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def main() -> None:
    for s in SESS:
        tr = [json.loads(l) for l in
              (DBG / s / f"trace_{s}.jsonl").read_text(encoding="utf-8").splitlines()]
        n = len(tr)
        # ① 车仪器 src 翻面：逐拍看「存在过锚的 id 集」的 src 变化
        src_dist = collections.Counter()
        flips = 0
        prev_anchor_ids: set | None = None
        reasons = collections.Counter()
        for r in tr:
            mc = r.get("mcars") or []
            anchor_ids = {m["id"] for m in mc if m.get("src") == "ANCHOR"}
            for m in mc:
                src_dist[m.get("src")] += 1
                rs = m.get("reason")
                if rs:
                    reasons[rs] += 1
            if prev_anchor_ids is not None:
                if (anchor_ids - prev_anchor_ids) or (prev_anchor_ids - anchor_ids):
                    flips += 1
            prev_anchor_ids = anchor_ids
        # ② 锚点节奏
        ages = [m["age"] for r in tr for m in (r.get("mcars") or [])
                if m.get("src") == "ANCHOR" and m.get("age") is not None]
        # ③ 币组可见闪
        grp = [bool(r.get("n_groups")) for r in tr]
        grp_flips = sum(1 for a, b in zip(grp, grp[1:]) if a != b)
        grp_seen = sum(grp)
        # ④ 收益报告缺席
        absent = sum(1 for r in tr if r.get("cr_best") is None)
        # ⑤ 走廊缺席
        cor_absent = sum(1 for r in tr if r.get("cor_w") is None)
        print(f"\n=== {s} 拍={n}")
        print(f"① 车仪器 src 分布={dict(src_dist)} ANCHOR集翻面={flips}次"
              f"（{100 * flips / n:.0f}% 拍）弃权原因={dict(reasons)}")
        if ages:
            print(f"② 锚龄 P50={_p50(ages):.0f}ms P90={sorted(ages)[9 * len(ages) // 10]:.0f}ms "
                  f"max={max(ages):.0f}ms（>250ms 占 {100 * sum(1 for a in ages if a > 250) / len(ages):.0f}%）")
        print(f"③ 币组可见 {100 * grp_seen / n:.0f}% 拍，0↔有 翻面 {grp_flips} 次"
              f"（{100 * grp_flips / n:.0f}% 拍）")
        print(f"④ 收益报告缺席 {100 * absent / n:.0f}% 拍")
        print(f"⑤ 走廊缺席 {100 * cor_absent / n:.0f}% 拍")


if __name__ == "__main__":
    main()
