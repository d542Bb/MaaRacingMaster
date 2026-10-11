# -*- coding: utf-8 -*-
"""决策门账本分析（2026-10-11，只读）：no_candidate 的逐门归因 + 错身壳距原料
+ 前置三件套验收三节。

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

三件套验收三节（计划 §2，2026-10-11；账本 v=2 起可用，v=1 缺列时如实跳过）：

  A① t_meet 收缩率对账：同一辆车相邻账本拍的 t_meet 差 / 墙钟差（ts_ns）应
     ≈1.0/s（预测以秒速收缩）；旧线性行率式按 (cy,rel) 重算同表——旧式受
     px/fid→px/tick 单位偏差+匀速假设双重拖累，实测斜率显著 <1。这是
     「单位/符号/时间基准契约检查」的回放形态，也是误差收窄判据的代理面。
  A② 成员资格对账：x_memb 与单拍 x 的带门翻转计数（成员位成立后 band 误拒
     应消失）；验收判据=接近段 band 误拒率下降（改造前后 trace 各跑本脚本）。
  A③ 宽度对账：w_m 覆盖率、w_m_age 分布（TTL 内/过期）、相邻有效拍跳变
     >1m 计数（遮挡崩坏不入统计的验收面）。

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
    # 三件套原料（跨拍按 (trace 文件名, 车id) 分组——id 空间每局/每阶段重卷）
    tmeet_series: dict[tuple, list[tuple]] = collections.defaultdict(list)
    # → 行 (t_wall, fid, t_meet, cy, rel)
    memb_rows = memb_disagree = memb_band_flip_in = memb_band_flip_out = 0
    w_rows = 0
    w_age_hist: collections.Counter = collections.Counter()
    w_jump_n = w_pairs = 0
    w_series: dict[tuple, list[tuple[float, float]]] = collections.defaultdict(list)
    led_versions: collections.Counter = collections.Counter()
    fid_hz_seen: set = set()
    # fid 时基：v2 账本自带 fid_hz；v1 旧 trace 按 (fid, ts) 差分中位估计
    fid_hz_by_run: dict[str, float] = {}
    fid_rate_samples: dict[str, list[float]] = collections.defaultdict(list)
    prev_fid_ts: tuple[int, float] | None = None
    prev_run: str | None = None

    for path in args.traces:
        run = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                led = d.get("ledger")
                if not led:
                    continue
                led_versions[led.get("v")] += 1
                fid_hz_seen.add(led.get("fid_hz"))
                if led.get("fid_hz") is not None:
                    fid_hz_by_run[run] = led["fid_hz"]
                # 旧 trace 的 fid 时基估计原料（连续 tick 的 Δfid/Δt）
                if run == prev_run and prev_fid_ts is not None:
                    dfid = d["fid"] - prev_fid_ts[0]
                    dts = d.get("ts_ns", 0) / 1e9 - prev_fid_ts[1]
                    if dfid > 0 and dts > 0:
                        fid_rate_samples[run].append(dfid / dts)
                prev_fid_ts, prev_run = (d["fid"], d.get("ts_ns", 0) / 1e9), run
                cars = led.get("cars") or []
                if not cars:
                    continue
                ts = d.get("ts_ns", 0) / 1e9
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
                    key = (run, r["id"])
                    if r.get("t_meet") is not None:
                        tmeet_series[key].append(
                            (ts, d["fid"], r["t_meet"], r["cy"], r["rel"]))
                    xm = r.get("x_memb")
                    if xm is not None:
                        memb_rows += 1
                        in_raw = 0.65 <= abs(r["x"]) < 1.6
                        in_memb = 0.65 <= abs(xm) < 1.6
                        if round(r["x"]) != round(xm):
                            memb_disagree += 1
                        if in_memb and not in_raw:
                            memb_band_flip_in += 1   # 成员位救回的误拒
                        if in_raw and not in_memb:
                            memb_band_flip_out += 1
                    w = r.get("w_m")
                    if w is not None:
                        w_rows += 1
                        w_age_hist[r.get("w_m_age", -1) // 2 * 2] += 1
                        w_series[key].append((ts, w))
                        if abs(r["cy"] - v_ego) <= args.win_px:
                            per_car[r["id"]].append((d["fid"], r["x"], w))

    print(f"== 账本对账（{len(args.traces)} 个 trace）==")
    print(f"有车且有账本拍 {car_ticks}，其中 hold 拍 {hold_ticks}；"
          f"hold 拍全车过门但未选中 {all_pass_ticks}（>0 需查改判路径）")
    print(f"cars 行 {rows}，why 直方图：{dict(why_hist.most_common())}")
    print(f"w_m 覆盖：行级 {rows_with_w}/{rows}")
    print(f"账本版本分布 {dict(led_versions)}；fid_hz 读数 {sorted(fid_hz_seen)}")

    print(f"\n== 错身壳距原料（重叠半窗 ±{args.win_px:.0f}px）==")
    for tid, obs in sorted(per_car.items()):
        min_dx = min(abs(x) for _, x, _ in obs)
        w_min = min(w for _, _, w in obs)
        shell = min_dx * lane_w - (args.ego_half_m + w_min / 2.0)
        print(f"car {tid}: 窗内 {len(obs)} 行，min|x|={min_dx:.2f} 道，"
              f"min 半宽 {w_min:.2f} m → ĝ*≈{shell:+.2f} m")

    # ---- 三件套验收 A①：预测 t_meet vs 实际错身时刻（回放配对） ----
    # 站拍 = 该车账本序列里 cy 首次 ≥ v_ego 的拍（遮挡延续拍 cy 为外推值，
    # 被超的车可观测到站）；对站拍前的每个账本拍，actual=(fid_arrive−fid)/fid_hz，
    # 误差 = 预测 − actual。新口径读账本 t_meet 列；旧线行率式按 (cy,rel) 重算
    # 作对照基线（同一批配对，改造前后的对比即「误差收窄」判据）。
    if tmeet_series:
        # v1 旧 trace 补估计 fid 时基（差分中位）；v2 以账本 fid_hz 为准
        for run, samples in fid_rate_samples.items():
            if run not in fid_hz_by_run and samples:
                samples.sort()
                fid_hz_by_run[run] = samples[len(samples) // 2]
        print("\n== 三件套①验收：预测 t_meet 对实际错身时刻的配对误差（s）==")
        for label, pick in (("新口径(账本 t_meet)", lambda s: s[2]),
                            ("旧线行率式(按 cy,rel 重算)",
                             lambda s: (v_ego - s[3]) / (s[4] * 20.0)
                             if s[4] > 0 else None)):
            errs = []
            for key, series in tmeet_series.items():
                fhz = fid_hz_by_run.get(key[0])
                arrive = next((r[1] for r in series if r[3] >= v_ego), None)
                if arrive is None or not fhz:
                    continue                    # 未观测到站/无时基：不配对
                for r in series:
                    if r[1] >= arrive:
                        continue
                    pred = pick(r)
                    if pred is not None:
                        errs.append(pred - (arrive - r[1]) / fhz)
            if errs:
                errs.sort()
                n = len(errs)
                absn = sorted(abs(e) for e in errs)
                print(f"  {label}: n={n} 误差 P25/P50/P75 = "
                      f"{errs[n//4]:+.1f}/{errs[n//2]:+.1f}/{errs[3*n//4]:+.1f}，"
                      f"|误差| P50={absn[n//2]:.1f}s"
                      f"（判据：新口径显著收窄；正=高估到站时间）")
            else:
                print(f"  {label}: 无可配对样本（账本内无 cy 跨 v_ego 的车/无 fid 时基）")

    # ---- 三件套验收 A②：成员资格 ----
    if led_versions.get(2):
        print("\n== 三件套②验收：车道成员资格 ==")
        print(f"  x_memb 成立行 {memb_rows}（读数 x 与成员位整数道不一致 "
              f"{memb_disagree}）；成员位救回的带内误拒 {memb_band_flip_in} 行、"
              f"成员位否决的出带读数 {memb_band_flip_out} 行")
        print("  （验收判据=接近段 band 误拒率下降：与改造前 trace 的 band 占比对比）")

    # ---- 三件套验收 A③：宽度 ----
    if led_versions.get(2):
        print("\n== 三件套③验收：宽度有效性面 ==")
        print(f"  有效 w_m 行 {w_rows}；w_m_age 分布（2tick 桶）{dict(sorted(w_age_hist.items()))}")
        for series in w_series.values():
            for a, b in zip(series, series[1:]):
                w_pairs += 1
                if abs(b[1] - a[1]) > 1.0:
                    w_jump_n += 1
        print(f"  相邻有效拍对 {w_pairs}，|Δw_m|>1m 跳变 {w_jump_n}"
              f"（判据：显著下降；遮挡崩坏 3.6→0.3m 案不入统计）")


if __name__ == "__main__":
    main()
