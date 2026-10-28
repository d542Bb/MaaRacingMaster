# -*- coding: utf-8 -*-
"""远场方向级消费的离线复算验收（2026-10-21 方案步骤 4）。

读取 speedrush trace（coarse + scene.road.bands + cor_far + sel_dbg.origin），
逐拍复算远带不对称 hint，并按 coarse.choose_lane 的同一字典序与迟滞规则
重放「开关关」「开关开」两种选道：

- 开关关的重放必须与 trace 记录的实际选道一致（复算保真度）；
- 开关开的重放给出实机开跑前的预测面：预置触发率、分歧拍分类、分歧方向、
  阈值附近抖动；
- 阈值扫描给出 FAR_HINT_MIN_LANE 与「进入/退出双阈值」迟滞的取舍依据。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_far_direction_replay.py \
        <trace.jsonl> [<trace.jsonl> ...]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ENTER_LANE = 0.5
SWITCH_MARGIN_M = 4.0
SWEEP = ((0.5, None, None), (0.5, None, 0.7), (0.5, None, 0.5),
         (0.5, None, 0.3), (0.5, 0.35, 0.5), (0.6, None, 0.5),
         (0.6, 0.4, 0.5), (0.7, 0.5, 0.5))


def center_at(bands, z: float) -> float | None:
    """与 selection.RoadFrame.center_at 同式（带中点线性插值）。"""
    pts = [(0.5 * (b[0] + b[1]), 0.5 * (b[2] + b[3]))
           for b in bands if b[2] is not None and b[3] is not None]
    if not pts:
        return None
    if z <= pts[0][0]:
        return pts[0][1]
    for (z0, c0), (z1, c1) in zip(pts, pts[1:]):
        if z <= z1:
            w = 0.0 if z1 <= z0 else (z - z0) / (z1 - z0)
            return c0 + (c1 - c0) * w
    return pts[-1][1]


def far_hint(row) -> tuple[float, float | None]:
    """走廊层共享方向结果的离线复算（原始车头系）：最近一条两侧有证据的
    粗带，中点相对该纵深中心线的偏移（道）。返回 (hint, |hint|)。"""
    far = row.get("cor_far")
    sc = row.get("scene") or {}
    bands = (sc.get("road") or {}).get("bands") or []
    lane_w = sc.get("lane_w")
    dbg = row.get("sel_dbg") or {}
    origin, shift = dbg.get("origin"), dbg.get("shift") or 0.0
    if not far or not bands or not lane_w or origin is None:
        return 0.0, None
    for b in far:
        if b[2] is None or b[3] is None:
            continue
        cen = center_at(bands, 0.5 * (b[0] + b[1]))
        if cen is None:
            return 0.0, None
        # 与 3m 带同一条平移（先加 executed shift、再减 origin）：远带中点
        # 与中心线同帧，executed 不得漏进方向信号（2026-10-21 修复口径）。
        mid = 0.5 * (b[2] + b[3]) + shift - origin
        return (mid - cen) / lane_w, abs((mid - cen) / lane_w)
    return 0.0, None


def replay(cand, ego: float, hint: float, active: bool,
           current_lane: float | None):
    """coarse.choose_lane 的选道段复算（不含净空/占用计算）。

    cand 行：[lane, free, margin, blocker, unknown_car]（trace 原样）。
    返回 (chosen_lane, best_lane, reason, best_free, best_unknown)。"""
    lanes = [(c[0], c[1], c[2] if c[2] is not None else -1e9, c[3], c[4])
             for c in cand]
    target = hint if active else 0.0

    def key(c):
        return (c[1], not c[4], -abs(c[0] - target), c[2], -abs(c[0] - ego))

    best = max(lanes, key=key)
    if current_lane is None:
        current_lane = round(ego)
    cur = next((c for c in lanes if abs(c[0] - current_lane) < 1e-6), None)
    closer_to_center = cur is not None and abs(best[0]) < abs(cur[0]) - 1e-9
    better_known = cur is not None and cur[4] and not best[4]
    toward_open = active and cur is not None and (best[0] - cur[0]) * hint > 0
    cur_clear = cur is not None and cur[3] is None
    if cur is not None and cur[1] > 0.0 and cur is not best and cur_clear \
            and not closer_to_center and not better_known and not toward_open \
            and best[1] - cur[1] <= SWITCH_MARGIN_M:
        chosen, reason = cur, "keep"
    else:
        chosen, reason = best, "space"
    return chosen[0], best[0], reason, best[1], best[4]


def simulate(rows, enter: float, exit_: float | None,
             alpha: float | None = None) -> dict:
    """逐拍重放。enter/exit_ 为预置进入/退出阈值（exit_=None 单阈值）；
    alpha 非 None 时模拟**共享滤波**：只在 dgeo_new 拍更新一次 EMA，其余拍
    共享同一结果（与生产口径一致，消费端不再逐拍去抖）。"""
    prev_off = prev_on = None
    prev_lane_on = prev_lane_off = None
    prev_sign = None
    active = False
    held = None
    n = active_n = 0
    mism = div = div_tie = div_yield = div_other = div_dir_ok = 0
    flips_on = flips_off = 0
    cross = {"activate": [0, 0], "deactivate": [0, 0], "flip": [0, 0]}
    episodes, episode_len = [], 0
    samples = []

    for r in rows:
        # 共享滤波（生产口径）：只在**新远带证据**拍更新一次，其余拍共享；
        # 与选择拍无关——弃权拍的新证据也更新，下一拍直接用。
        hint_raw, ah_raw = far_hint(r)
        if alpha is not None and r.get("dgeo_new"):
            held = None if ah_raw is None else (
                hint_raw if held is None
                else (1.0 - alpha) * held + alpha * hint_raw)
        c = r.get("coarse")
        if not c or not c.get("cand"):
            prev_off = prev_on = prev_lane_on = prev_lane_off = None
            prev_sign, active = None, False
            if episode_len:
                episodes.append(episode_len)
                episode_len = 0
            continue
        n += 1
        ego, cand = c["ego"], c["cand"]
        hint = hint_raw if alpha is None else held
        ah = None if hint is None else abs(hint)
        if ah is None:
            active = False
        else:
            if active:
                active = ah >= (enter if exit_ is None else exit_)
            else:
                active = ah >= enter
        active_n += active

        off = replay(cand, ego, 0.0, False, prev_off)
        on = replay(cand, ego, hint, active, prev_on)
        if abs(off[0] - c["lane"]) > 1e-6:
            mism += 1
        if abs(on[0] - c["lane"]) > 1e-6:
            div += 1
            if abs(on[0] - off[0]) > 1e-6:
                same_first2 = abs(on[3] - off[3]) < 1e-9 and on[4] == off[4]
                if off[2] == "keep" and on[2] == "space" \
                        and abs(on[0] - off[1]) < 1e-6:
                    div_yield += 1
                elif same_first2:
                    div_tie += 1
                else:
                    div_other += 1
                if abs(hint) > 1e-9 and (on[0] - off[0]) * hint > 0:
                    div_dir_ok += 1
                if len(samples) < 6:
                    samples.append({
                        "fid": r.get("fid"), "hint": round(hint, 2),
                        "off": off[0], "on": on[0],
                        "why_off": off[2], "why_on": on[2],
                    })

        sign = ((hint > 0) - (hint < 0)) if active else 0
        if prev_sign is not None and sign != prev_sign:
            kind = ("activate" if prev_sign == 0 else
                    "deactivate" if sign == 0 else "flip")
            cross[kind][0] += 1
            if prev_lane_on is not None and abs(on[0] - prev_lane_on) > 1e-6:
                cross[kind][1] += 1
        prev_sign = sign
        episode_len += 1 if sign else 0
        if sign == 0 and episode_len:
            episodes.append(episode_len)
            episode_len = 0

        if prev_lane_on is not None:
            flips_on += abs(on[0] - prev_lane_on) > 1e-6
            flips_off += abs(off[0] - prev_lane_off) > 1e-6
        prev_on, prev_off = on[0], off[0]
        prev_lane_on, prev_lane_off = on[0], off[0]
    if episode_len:
        episodes.append(episode_len)

    return {
        "enter": enter, "exit": exit_, "alpha": alpha, "valid": n,
        "active": active_n,
        "active_rate": active_n / n if n else 0.0,
        "off_mismatch": mism,
        "div": div, "div_tie": div_tie, "div_yield": div_yield,
        "div_other": div_other, "div_dir_ok": div_dir_ok,
        "div_rate": div / n if n else 0.0,
        "flips_on": flips_on, "flips_off": flips_off,
        "cross": cross,
        "episodes": len(episodes),
        "short_episodes": sum(1 for e in episodes if e <= 2),
        "episode_median": statistics.median(episodes) if episodes else 0,
        "samples": samples,
    }


def hint_stats(rows) -> dict:
    hs = [ah for r in rows for ah in [far_hint(r)[1]] if ah is not None]
    return {
        "valid": len(hs),
        "p50": statistics.median(hs) if hs else None,
        "p90": statistics.quantiles(hs, n=10)[8] if len(hs) >= 10 else None,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        path = Path(arg)
        rows = [json.loads(line) for line in path.open(encoding="utf-8")]
        print(f"\n=== {path.name} ===")
        hs = hint_stats(rows)
        if hs["p50"] is not None:
            print(f"hint 可算 {hs['valid']} 拍  |hint| p50={hs['p50']:.2f} 道"
                  + (f"  p90={hs['p90']:.2f} 道" if hs["p90"] is not None else ""))
        base = simulate(rows, ENTER_LANE, None, 0.5)
        print(f"复算保真度（关 vs 实记）：不一致 {base['off_mismatch']} 拍")
        print(f"基线（共享滤波 α=0.5，进入 {ENTER_LANE}）：触发 {base['active']} 拍 "
              f"({base['active_rate']:.1%})  分歧 {base['div']} 拍 "
              f"({base['div_rate']:.1%})：打平 {base['div_tie']}  "
              f"迟滞让路 {base['div_yield']}  其它 {base['div_other']}  "
              f"方向一致 {base['div_dir_ok']}/{base['div']}")
        c = base["cross"]
        print(f"跨越：激活 {c['activate'][0]}（翻面 {c['activate'][1]}）  "
              f"失活 {c['deactivate'][0]}（翻面 {c['deactivate'][1]}）  "
              f"换向 {c['flip'][0]}（翻面 {c['flip'][1]}）")
        print(f"激活段 {base['episodes']} 段，中位 {base['episode_median']} 拍，"
              f"≤2 拍短段 {base['short_episodes']} 段")
        print(f"换道拍：开 {base['flips_on']}  关 {base['flips_off']}")
        for s in base["samples"]:
            print("  样本", s)
        print("扫描（进入/退出/共享α  触发率  分歧率  方向一致  "
              "激活翻面  失活翻面  中位段长/短段）")
        for enter, exit_, alpha in SWEEP:
            r = simulate(rows, enter, exit_, alpha)
            c = r["cross"]
            print(f"  {enter:.2f}/{('-' if exit_ is None else f'{exit_:.2f}'):>4}/"
                  f"{('-' if alpha is None else f'{alpha:.1f}'):>4}  "
                  f"{r['active_rate']:6.1%}  {r['div_rate']:6.1%}  "
                  f"{r['div_dir_ok']:>4}/{r['div']:<4}  "
                  f"{c['activate'][1]:>3}/{c['activate'][0]:<3}  "
                  f"{c['deactivate'][1]:>3}/{c['deactivate'][0]:<3}  "
                  f"{r['episode_median']:>4.0f} / {r['short_episodes']:<3}"
                  f"（{r['episodes']} 段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
