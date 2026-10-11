# -*- coding: utf-8 -*-
"""决策门账本分析（2026-10-11，只读）：no_candidate 的逐门归因 + 错身壳距原料。

问（docs/plan/speedrush-score-routing §2/§4）：视野有车的 hold 拍里，候选
究竟死在哪道门（band/approach/score/feas/veto/side/done_pass）？账本列
（trace `ledger`，decision.gate_ledger 产出）逐拍逐车落了判定读数，本脚本
聚合成账目：

  ① 拍级：有车 hold 拍的占比；这些拍里逐门拒绝计数（why=第一道拦下的门）；
     全门通过却仍 hold 的拍数（>0 需查改判路径/switch_margin）。
  ② 车级：所有 cars 行的 why 直方图；带 w_m 读数的行占比（V0 车宽链覆盖面）。
  ③ 错身壳距原料：按 pass track_id 收集该车落在重叠窗（|cy−v_ego|≤win）内的
     行，取最小横向中心距与最小半宽。**输出只到原料级**：ĝ*=min|x|·lane_w −
     (ego_half + w/2) 是近似式（ego 半宽是参数非标定，见计划 §4），触发带
     标定须与 HUD 极限事件配对后再下结论，本脚本不判触发。

口径：hold 拍 = state=="CRUISE" 且 "no_candidate" in reason；行结构见
decision.gate_ledger docstring。2D 帧龄拍级已有 oage 列，不重复对账。
输出走 stdout（重定向即落盘），不写文件。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_decision_ledger.py \
        "%APPDATA%\\MaaRacingMaster\\data\\speedrush\\control_traces\\trace_xxx_p1.jsonl" \
        [--win-px 60] [--ego-half-m 0.9]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--win-px", type=float, default=60.0,
                    help="重叠半窗（px，cy 相对 v_ego 的对称窗；窗口本身待标定）")
    ap.add_argument("--ego-half-m", type=float, default=0.9,
                    help="自车半宽（米，起值=latsample GRID_CAR_HALF_LANE_M；待标定）")
    args = ap.parse_args()

    sys.path.insert(0, REPO)
    from maaracing_master.plugins.speedrush.world_model import load_calib
    cal = load_calib()
    lane_w, v_ego = cal.lane_w_m, cal.v_ego

    why_hist: collections.Counter = collections.Counter()
    car_ticks = hold_ticks = all_pass_ticks = 0
    rows = rows_with_w = 0
    per_car: dict[int, list[tuple[int, float, float]]] = collections.defaultdict(list)

    for path in args.traces:
        with open(path, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                led = d.get("ledger")
                if not led:
                    continue
                cars = led.get("cars") or []
                if not cars:
                    continue
                car_ticks += 1
                rows += len(cars)
                rows_with_w += sum(1 for r in cars if "w_m" in r)
                hold = d.get("state") == "CRUISE" \
                    and "no_candidate" in (d.get("reason") or "")
                if hold:
                    hold_ticks += 1
                    whys = [r.get("why") for r in cars]
                    if all(w is None for w in whys):
                        all_pass_ticks += 1
                why_hist.update((r.get("why") if r.get("why") is not None
                                 else "none") for r in cars)
                for r in cars:
                    w = r.get("w_m")
                    if w is not None and abs(r["cy"] - v_ego) <= args.win_px:
                        per_car[r["id"]].append((d["fid"], r["x"], w))

    print(f"== 账本对账（{len(args.traces)} 个 trace）==")
    print(f"有车且有账本拍 {car_ticks}，其中 hold 拍 {hold_ticks}；"
          f"hold 拍全车过门但未选中 {all_pass_ticks}（>0 需查改判路径）")
    print(f"cars 行 {rows}，why 直方图：{dict(why_hist.most_common())}")
    print(f"w_m 覆盖：行级 {rows_with_w}/{rows}")

    print(f"\n== 错身壳距原料（重叠半窗 ±{args.win_px:.0f}px）==")
    for tid, obs in sorted(per_car.items()):
        min_dx = min(abs(x) for _, x, _ in obs)
        w_min = min(w for _, _, w in obs)
        shell = min_dx * lane_w - (args.ego_half_m + w_min / 2.0)
        print(f"car {tid}: 窗内 {len(obs)} 行，min|x|={min_dx:.2f} 道，"
              f"min 半宽 {w_min:.2f} m → ĝ*≈{shell:+.2f} m")


if __name__ == "__main__":
    main()
