# -*- coding: utf-8 -*-
"""实验 B 可辨识性审查：现有 trace 能不能分开"目标反复改"与"目标稳定但执行不足"。

**先说清哪一半不可辨识（这是本探针存在的理由）**：计划候选的展开与控制器状态
推进调用的是**同一个运动学函数** `planner.step`（`motion_plan.py:12` 导入、
`:525` 调用）。所以"计划预测横移 − executed_lane 实际横移"不是执行误差，是模型
和自己比——它只会反映观测修正项，不会反映车到底动没动。要测真实横移必须有
独立位移源，而现有两条都不够干净：

* 深度路心：同源相邻差 p50 0.088 道（≈0.2 s 窗），1 s 窗噪声约 0.2 道；
* 车道线像素斜率增量：1 s 窗噪声 0.23 道/s（`probe_lane_velocity` 实测，且
  短窗抖动是**共模**，0.1~0.3 s 窗不可用）。

⇒ 现有数据能判"**目标是否在反复改**"（纯决策输出，无测量污染），以及
"误差持续多久"；**不能**判"目标稳定时执行到底差了多少"。本探针只做前者，
后者列为缺证据项。

口径：目标改判 = |Δx_target| ≥ 0.3 道（大于本道安全带 ±0.17）或符号翻转
（|x_target| > 0.15 才计方向）。误差段 = |executed − x_target| ≥ 0.5 连续 ≥10 拍。
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

ERR_LANE = 0.5
MIN_RUN = 10
CHANGE_LANE = 0.3
DIR_MIN = 0.15


def load(root: Path):
    out = []
    for ls in sorted((root / "logs").iterdir()):
        p = ls / "trace" / "speedrush.jsonl"
        if not p.is_file():
            continue
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8")
                .splitlines() if l.strip()]
        tk = [r for r in rows if isinstance(r.get("fid"), int)
              and isinstance(r.get("executed_lane"), (int, float))
              and isinstance(r.get("x_target"), (int, float)) and r.get("dt")]
        if len(tk) > 400:
            out.append((ls.name, tk))
    return out


def target_changes(tk, ts):
    """改判时刻序列 + 相邻改判之间的稳定时长。"""
    ev = []
    for i in range(1, len(tk)):
        a, b = tk[i - 1]["x_target"], tk[i]["x_target"]
        flip = (abs(a) > DIR_MIN and abs(b) > DIR_MIN and a * b < 0)
        if flip or abs(b - a) >= CHANGE_LANE:
            ev.append(i)
    durs = [ts[j] - ts[k] for k, j in zip(ev, ev[1:])]
    return ev, durs


def err_runs(tk, ts):
    runs, i, n = [], 0, len(tk)
    while i < n:
        if abs(tk[i]["executed_lane"] - tk[i]["x_target"]) >= ERR_LANE:
            j = i
            while (j + 1 < n and abs(tk[j + 1]["executed_lane"]
                                      - tk[j + 1]["x_target"]) >= ERR_LANE):
                j += 1
            if j - i + 1 >= MIN_RUN:
                runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def main() -> None:
    root = Path.home() / "AppData/Roaming/MaaRacingMaster"
    cls = Counter()
    time_in = Counter()
    all_durs, all_runlen = [], []
    per_sess = []
    for name, tk in load(root):
        ts = [0.0]
        for r in tk:
            ts.append(ts[-1] + float(r.get("dt") or 0.05))
        ts = ts[1:]
        ev, durs = target_changes(tk, ts)
        all_durs += durs
        evset = set(ev)
        rows = 0
        for i, j in err_runs(tk, ts):
            dur = ts[j] - ts[i]
            all_runlen.append(dur)
            n_ch = sum(1 for k in range(i + 1, j + 1) if k in evset)
            lab = ("目标稳定(0 改判)" if n_ch == 0 else
                   "改判 1 次" if n_ch == 1 else "改判 ≥2 次")
            cls[lab] += 1
            time_in[lab] += dur
            rows += 1
        per_sess.append((name, round(ts[-1], 1), len(ev),
                         round(len(ev) / ts[-1], 2), rows))
    print("会话（时长 s / 目标改判次数 / 次每秒 / 误差段数）")
    for r in per_sess:
        print(f"  {r[0]:22s} {r[1]:6.1f}  {r[2]:4d}  {r[3]:4.2f}  {r[4]:3d}")
    tot = sum(cls.values())
    print(f"\n误差段合计 {tot} 段（≥{MIN_RUN} 拍、|executed−x_target|≥{ERR_LANE} 道）")
    for k in ("目标稳定(0 改判)", "改判 1 次", "改判 ≥2 次"):
        if cls[k]:
            print(f"  {k:16s} 段数 {cls[k]:4d} = {100*cls[k]/tot:3.0f}%   "
                  f"合计时长 {time_in[k]:6.1f}s = "
                  f"{100*time_in[k]/sum(time_in.values()):3.0f}%")
    q = lambda v, p: sorted(v)[min(len(v) - 1, int(p * len(v)))] if v else 0
    print(f"\n目标稳定时长（相邻两次改判之间）p50 {q(all_durs,.5):.2f}s "
          f"p90 {q(all_durs,.9):.2f}s  <0.7s 占 "
          f"{100*sum(1 for d in all_durs if d < 0.7)/max(1,len(all_durs)):.0f}%"
          f"（配置口径：跨 1 车道 ≈0.7s）")
    print(f"误差段自身时长 p50 {q(all_runlen,.5):.2f}s "
          f"p90 {q(all_runlen,.9):.2f}s 最长 {max(all_runlen or [0]):.1f}s")
    print("\n**不可辨识项（本轮不判）**：目标稳定段里执行到底差多少——"
          "计划展开与控制器状态推进同源（同一个 step），需独立位移源。")


if __name__ == "__main__":
    main()
