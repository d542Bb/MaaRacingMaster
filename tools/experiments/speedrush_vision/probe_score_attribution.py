# -*- coding: utf-8 -*-
"""B 线：把主线拉回第四层——测量缺陷到底解不解释"开得差"。

**要回答的问题**（不是"换源台阶有多大"，那已经量过了）：

1. 换源台阶之后，异常行为是不是更容易发生？
2. **没有**换源台阶时，异常行为发生的概率是多少？
3. 已经发生的异常里，有多少次**前面根本没有换源台阶**？（必要性检验——
   这一项高，说明参考系交接不是主因，不该继续当首要嫌疑）
4. 每类异常期间的**实际得分速率**比基线差多少？异常合计占多少驾驶时间？

得分速率优先用 HUD 累计分离窗差分（`hud_score` 的增量/时间），显示用的
`hud_rate` 只作旁证——RULES §5 的优化对象是积分率，不是"看起来稳不稳"。

**异常类别全部从 trace 可见量定义**（不依赖图像，也不依赖横位真值）：

- ``hold_err``  控制器明知偏 0.5 道以上却回不来：|executed − x_target| ≥ 0.5
  连续 ≥ 10 拍
- ``osc``       1 秒内杆向翻转 ≥ 3 次
- ``target_flip`` 1 秒内目标符号翻转 ≥ 2 次
- ``veto``      计划本拍拒绝执行（veto_reason 非空）

**统计口径**：按会话分别报，合并只给区间不给假置信度；相邻拍高度相关，
所以概率一律按"拍"计、事件数按"连续段"计，两者都报。
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

HUG_LANE = 0.5           # 明知偏差的门（道）
HUG_TICKS = 10           # 连续拍数
OSC_REV = 3              # 1 秒内杆向翻转次数
TGT_REV = 2              # 1 秒内目标符号翻转次数
STEP_LANE = 0.25         # 换源台阶的幅度门（道）
LOOKBACK_S = 1.0         # 异常起点前看多久


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
              and r.get("dt")]
        if len(tk) < 400:
            continue                      # 残局不入库
        out.append((ls.name, tk))
    return out


def t_series(tk):
    t = 0.0
    out = []
    for r in tk:
        t += float(r.get("dt") or 0.05)
        out.append(t)
    return out


DEF_GROUPS = {"wall": "wall", "wall_occl": "wall", "region": "region",
              "memory": "memory", "pair": "wall", "pair_slot": "wall",
              "grid_region": "region", None: None}


def steps(tk, ts):
    """**跨定义**交接台阶拍：只在两个不同物理定义之间换供数者时才算
    （wall↔wall_occl 是同一把尺的内部交替，算进去会把事件率抬到 0.5/s，
    首版就踩过这个坑）。产线换档标注 ro_hop=1 一并计入。"""
    # 只在**相邻两条新鲜读数**之间判跨定义：逐拍 ro_source 有 60% 在变
    # （墙↔遮挡墙↔记忆的内部交替，同一把尺），拿它当事件会把率抬到 11 次/秒。
    fr = [(i, tk[i].get("ro_source"), tk[i]["road_offset"])
          for i in range(len(tk))
          if tk[i].get("dgeo_new") and not tk[i].get("ro_hold")
          and isinstance(tk[i].get("road_offset"), (int, float))]
    ev = []
    for (i0, s0, d0), (i1, s1, d1) in zip(fr, fr[1:]):
        g0, g1 = DEF_GROUPS.get(s0), DEF_GROUPS.get(s1)
        if tk[i1].get("ro_hop") == 1 or (g0 and g1 and g0 != g1
                                         and abs(d1 - d0) >= STEP_LANE):
            ev.append(ts[i1])
    return ev


def near(ev, t, win=LOOKBACK_S):
    return any(t - win <= e <= t for e in ev)


def classify(tk, ts):
    """逐拍打异常标签 + 找每类异常段的起点。"""
    n = len(tk)
    lab = defaultdict(lambda: [False] * n)
    # hold_err：连续段
    run = 0
    starts = []
    for i in range(n):
        xt = tk[i].get("x_target")
        e = tk[i].get("executed_lane")
        err = abs(e - xt) if isinstance(xt, (int, float)) else 0.0
        if err >= HUG_LANE:
            run += 1
            if run == HUG_TICKS:
                starts.append(i)
                for j in range(i - HUG_TICKS + 1, min(n, i + HUG_TICKS * 3)):
                    if abs(tk[j].get("x_target") - tk[j]["executed_lane"]) >= HUG_LANE:
                        lab["hold_err"][j] = True
        else:
            run = 0
    # 翻转类：滑窗 1s（onset 只取连续段的**第一拍**，否则事件率被逐拍命中抬高）
    def flips(key, thr, name):
        s = []
        hit = [False] * n
        for i in range(n):
            lo = ts[i] - 1.0
            j = i
            while j > 0 and ts[j - 1] >= lo:
                j -= 1
            vals = [tk[k].get(key) for k in range(j, i + 1)]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if len(vals) < 4:
                continue
            rev = sum(1 for a, b in zip(vals, vals[1:]) if a * b < -1e-9)
            if rev >= thr:
                lab[name][i] = hit[i] = True
        for i in range(n):
            if hit[i] and (i == 0 or not hit[i - 1]):
                s.append(i)
        return s
    osc_starts = flips("steer_norm", OSC_REV, "osc")
    tgt_starts = flips("x_target", TGT_REV, "target_flip")
    for i, r in enumerate(tk):
        if r.get("veto_reason"):
            lab["veto"][i] = True
    onsets = {"hold_err": starts, "osc": osc_starts, "target_flip": tgt_starts,
              "veto": [i for i in range(n) if lab["veto"][i]
                       and (i == 0 or not lab["veto"][i - 1])]}
    return lab, onsets


def score_segments(tk, ts):
    """HUD 累计分按时间切成 (t, score)，返回区间内的分/秒。"""
    pts = [(ts[i], float(r["hud_score"])) for i, r in enumerate(tk)
           if isinstance(r.get("hud_score"), (int, float))]
    def rate(a, b):
        seg = [(t, s) for t, s in pts if a <= t <= b]
        if len(seg) < 2 or seg[-1][0] - seg[0][0] < 1.0:
            return None
        return (seg[-1][1] - seg[0][1]) / (seg[-1][0] - seg[0][0])
    return rate


def main() -> None:
    root = Path.home() / "AppData/Roaming/MaaRacingMaster"
    BASE = [0.0]
    agg = defaultdict(lambda: {"base": 0, "after_step": 0, "n_onset": 0,
                               "onset_after_step": 0, "ticks": 0})
    per_sess = []
    for name, tk in load(root):
        ts = t_series(tk)
        ev = steps(tk, ts)
        lab, onsets = classify(tk, ts)
        rate = score_segments(tk, ts)
        total = ts[-1]
        expo = sum(1 for t in ts if near(ev, t)) / len(ts)
        row = {"session": name, "dur_s": round(total, 1),
               "step_events": len(ev), "step_per_s": round(len(ev) / total, 3),
               "exposure": round(expo, 3)}
        for k, flags in lab.items():
            ticks = sum(flags)
            agg[k]["ticks"] += ticks
            agg["ALL"]["ticks"] += ticks
        for k, idxs in onsets.items():
            hit = sum(1 for i in idxs if near(ev, ts[i]))
            agg[k]["n_onset"] += len(idxs)
            agg[k]["onset_after_step"] += hit
        # 每拍基线概率（无台阶时 vs 有台阶前）
        for i in range(len(tk)):
            aft = near(ev, ts[i])
            for k, flags in lab.items():
                if flags[i]:
                    agg[k]["after_step" if aft else "base"] += 1
        # 得分速率：异常段 vs 其余
        seg = []
        for k, flags in lab.items():
            runs = [(ts[a], ts[b]) for a, b in _runs(flags)]
            r_in = [v for v in (rate(a, b) for a, b in runs)
                    if v is not None and 0.0 <= v <= 200.0]
            if r_in:
                seg.append((k, round(statistics.mean(r_in), 1), len(r_in),
                            round(sum(b - a for a, b in runs), 1)))
        row["score_by_class"] = seg
        row["score_all"] = None if rate(0, total) is None else round(rate(0, total), 1)
        BASE.append(expo)
        per_sess.append(row)
    print("会话：")
    for r in per_sess:
        print(f"  {r['session']} {r['dur_s']}s 跨定义台阶 {r['step_events']}"
              f"（{r['step_per_s']:.3f}/s） 基线暴露率(任一拍前1s内有台阶) "
              f"{100*r['exposure']:.0f}%  全程分/秒 {r['score_all']}")
        for k, v, n, dur in r["score_by_class"]:
            print(f"      {k:12s} 段内分/秒 {v:6.1f}  段数 {n:3d}  合计 {dur:5.1f}s")
    print("\n三、必要性检验（异常起点前 1s 内是否有换源台阶）")
    pooled = statistics.mean(BASE) if BASE else 0.0
    tot_on = tot_hit = 0
    for k in ("hold_err", "osc", "target_flip", "veto"):
        a = agg[k]
        if not a["n_onset"]:
            continue
        tot_on += a["n_onset"]
        tot_hit += a["onset_after_step"]
        print(f"  {k:12s} 起点 {a['n_onset']:4d}  台阶后 {a['onset_after_step']:4d}"
              f" = {100*a['onset_after_step']/a['n_onset']:4.0f}%"
              f"  提升比 {a['onset_after_step']/a['n_onset']/max(1e-9, pooled):.2f}×")
    print(f"  合计起点 {tot_on}，台阶后 {tot_hit} = {100*tot_hit/max(1,tot_on):.0f}%")
    print("\n四、时间占比（拍）")
    all_t = sum(agg[k]["ticks"] for k in ("hold_err", "osc", "target_flip", "veto"))
    base_ticks = sum(agg[k]["base"] + agg[k]["after_step"]
                     for k in ("hold_err", "osc", "target_flip", "veto"))
    for k in ("hold_err", "osc", "target_flip", "veto"):
        a = agg[k]
        tk_tot = a["base"] + a["after_step"]
        no_step = max(0, 1 - a["base"] / max(1, tk_tot)) if tk_tot else 0
        print(f"  {k:12s} 占拍数比 {100*a['ticks']/max(1,base_ticks):4.0f}%"
              f"   台阶前 1s 内的占比 {100*no_step:4.0f}%")


def _runs(flags):
    i = 0
    n = len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j + 1 < n and flags[j + 1]:
                j += 1
            yield i, j
            i = j + 1
        else:
            i += 1


if __name__ == "__main__":
    main()
