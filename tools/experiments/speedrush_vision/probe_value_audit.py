# -*- coding: utf-8 -*-
"""深度计算价值对账（2026-10-09，只读）：三个统计的分母、时间对齐与因果。

问（GPT 攻击复核后定案的第一步）：44.8%（侧向被拒）/ 18.6%（ro_source=none）/
72%（hold:no_candidate）三个统计的分母与性质不同，对账前不得用于主线论证。
本脚本对齐它们并回答：

  ① dgeo_sides 分布（dgeo_new=true 的拍）：sides=0/1/2 各多少——44.8% 里
    多少是"整帧无几何"（sides=0）vs"单侧可用"（sides=1）？
  ② ro_source 分布与交叉：新计算当拍供数（pair）/保鲜槽（pair_slot）/
    黑视（none）各多少；dgeo_new=true 的拍里 ro_source=none 的成因分解
    （sides=0 单侧不供数 / 垃圾对 / 污染对 / 槽过期）？
  ③ hold:no_candidate 拍里 ro_source 的分布——hold 时深度在供数吗？
  ④ 消费计数：相邻 dgeo_new 拍间隔（一次计算被几个拍消费的驻留复用率）；
    发布后从未供数的计算比例（dgeo_new=true 且其后到下一个 dgeo_new 前
    ro_source 恒为 none）。
  ⑤ ro（路心）与 grid（ro_grid 列）两个消费面分开算：sides=1 的拍里
    ro_grid 有值的比例——单侧实例的栅格消费面仍在吗？

**口径**：rdgeo_new=true 标记本拍有新深度计算；dgeo_sides/ro_* 是本拍
take() 快照。ro_source 语义（module.py _EgoRoadObserver）：pair=新证据
供数、pair_slot=保鲜槽、none=黑视/垃圾/污染、veto=互证否决（生产 master
不参与）。**供数门 sides==2**：单侧实例对 road_offset 链路无贡献，但栅格
消费面独立。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_value_audit.py
    ... --since 20261008    # 只对账某日期后的 trace
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

TRACES = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
          / "speedrush" / "control_traces")


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="20261008",
                    help="只对账文件名含此后缀的 trace（默认 20261008=账本语料）")
    a = ap.parse_args()

    files = sorted(TRACES.glob("trace_*.jsonl"))
    if a.since:
        files = [f for f in files if a.since in f.name]
    if not files:
        print(f"无匹配 trace（{a.since}）")
        return 2

    total = 0
    new_total = 0
    sides_c = Counter()
    ro_src_c = Counter()
    hold_total = 0
    hold_ro_c = Counter()
    # ④ 消费计数：一次 dgeo_new 到下一个 dgeo_new 之间的拍数 = 消费拍数
    #   （驻留复用）；其中 ro_source != none 的拍 = 有效消费
    last_new_fid = None
    consumed_counts: list[int] = []
    effective_counts: list[int] = []
    cur_consumed = 0
    cur_effective = 0
    # ④b 发布后从未供数的计算
    never_consumed = 0
    # ⑤ sides=1 的拍里 ro_grid 有值
    sides1_total = 0
    sides1_grid_ok = 0
    # ②b dgeo_new=true 且 ro_source=none 的成因（用 dgeo_sides 分）
    new_none_c = Counter()

    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            is_new = bool(r.get("dgeo_new"))
            ro = r.get("ro_source") or "none"
            state = r.get("state")
            reason = r.get("reason") or ""
            sides = r.get("dgeo_sides")

            if is_new:
                new_total += 1
                if last_new_fid is not None:
                    consumed_counts.append(cur_consumed)
                    effective_counts.append(cur_effective)
                    if cur_effective == 0:
                        never_consumed += 1
                last_new_fid = r.get("fid")
                cur_consumed = 0
                cur_effective = 0
                if sides is not None:
                    sides_c[int(sides)] += 1
                ro_src_c[ro] += 1
                if ro == "none":
                    new_none_c[int(sides) if sides is not None else -1] += 1
            # 本拍是否消费（当前计算发布后）：dgeo_new 拍自身算第一拍消费
            cur_consumed += 1
            if ro != "none":
                cur_effective += 1

            if sides == 1:
                sides1_total += 1
                if r.get("ro_grid") is not None:
                    sides1_grid_ok += 1
            if reason.startswith("hold") or state == "CRUISE":
                if "no_candidate" in reason or "cooling" in reason:
                    hold_total += 1
                    hold_ro_c[ro] += 1
    # 收尾最后一个计算
    if last_new_fid is not None:
        consumed_counts.append(cur_consumed)
        effective_counts.append(cur_effective)
        if cur_effective == 0:
            never_consumed += 1

    print(f"trace {len(files)} 个 / 拍 {total} / 新深度计算 {new_total}")
    print(f"\n=== ① sides 分布（dgeo_new={new_total}）===")
    for k in (0, 1, 2):
        print(f"  sides={k}: {sides_c[k]:5d}  {pct(sides_c[k], new_total)}")
    s0, s1, s2 = sides_c[0], sides_c[1], sides_c[2]
    print(f"  → 44.8%（带侧向拒绝记录）= sides=1 的 {s1} + sides=0 的 {s0}"
          f"（整帧无几何）；sides=1 对 road_offset 无贡献但栅格面独立")

    print(f"\n=== ② ro_source 分布（新计算拍）===")
    for k, v in ro_src_c.most_common():
        print(f"  {k:10s} {v:5d}  {pct(v, new_total)}")
    print(f"  dgeo_new=true 且 ro=none 的成因（按 sides）：")
    for k, v in sorted(new_none_c.items()):
        label = {0: "sides=0（整帧无几何）", 1: "sides=1（单侧不供 ro）",
                 2: "sides=2（垃圾/污染对被拒）"}.get(k, f"sides={k}")
        print(f"    {label}: {v}")

    print(f"\n=== ③ hold 拍（no_candidate/cooling，n={hold_total}）ro_source ===")
    for k, v in hold_ro_c.most_common():
        print(f"  {k:10s} {v:5d}  {pct(v, hold_total)}")
    sup = sum(v for k, v in hold_ro_c.items() if k != "none")
    print(f"  → hold 拍里深度在供数（pair/pair_slot）：{pct(sup, hold_total)}")

    if consumed_counts:
        n = len(consumed_counts)
        srt_c = sorted(consumed_counts)
        srt_e = sorted(effective_counts)

        def _p50(xs):
            return xs[n // 2]
        print(f"\n=== ④ 消费计数（每次计算的驻留复用，n={n}）===")
        print(f"  发布→下次新计算间隔拍数 p50={_p50(srt_c)}  "
              f"p90={srt_c[int(n * 0.9)]}")
        print(f"  其中 ro 供数拍数     p50={_p50(srt_e)}  "
              f"p90={srt_e[int(n * 0.9)]}")
        print(f"  发布后从未供数（零有效消费）的计算：{never_consumed}  "
              f"{pct(never_consumed, n)} ← '真浪费'的唯一精确口径")

    print(f"\n=== ⑤ sides=1 的拍（n={sides1_total}）栅格消费面 ===")
    print(f"  ro_grid 有值：{sides1_grid_ok}  {pct(sides1_grid_ok, sides1_total)}"
          f"  ← 单侧实例的路心不供数但栅格仍可产出")

    print("\n判读：④ 的 never_consumed 才是'可避免计算'的候选上限；"
          "② 的 sides=0 是几何质量（点云空洞）问题，调度解决不了；"
          "③ 若 hold 拍供数占比高，则'hold=无价值'叙事不成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
