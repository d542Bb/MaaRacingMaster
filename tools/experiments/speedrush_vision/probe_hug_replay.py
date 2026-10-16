# -*- coding: utf-8 -*-
"""R3 贴身猎车影子回放（2026-10-16 设计稿 §4.2 ②）：A/B trace 离线重建车侧
仪器，日志 cr_scores 作 R3-off 基线，报告贴身 E 项的四项行为收益。

**口径（先读）**：
- 币组明细（ledger）在 A/B 局几乎全程缺席（corridor_reach.control 常开，
  legacy _select 不跑）——币侧不重建，用日志 cr_scores 作 R3-off 基线（那是
  引擎实时评过的真实分数，含币与旧错身项）。R3 只加车侧价值，逐候选增量
  Δ=evaluate(闸开)−evaluate(闸关)（coins=[] 使币侧抵消，计分逻辑单一真源
  不复制），argmax(score+Δ) vs argmax(score) 的差可归因 R3。
- 走廊带不落 trace：span 用空气墙口径重建（center±cor_w/2，ro_off/ cor_w
  列），逐拍与 len(cr_scores) 对账——不一致的拍计数跳过（保真面）。
- 车横位用 car_pos.x×lane_w（设计稿 §5 模型假设：相遇时横位=当前帧横位）；
  t_overtake 用 dist 的 m/v（v>0=被接近前车，t=z/v——符号约定对账见
  decision._reach_car_sources docstring）。
- 碰撞候选（cr_scores≤−100）Δ 记 0：crash 判据 R3 不动。
- 四项指标：①最佳候选改变次数（argmax 变且 |Δx|≥0.05m）②目标位进入贴邻窗
  次数（新胜出位落某前车贴邻窗内）③可达性门拒绝率（前车贴邻窗候选全被门拒）
  ④安全拒绝率（贴邻窗候选 crash 占比）。另报米制锚覆盖（R2 可观测性同源）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_hug_replay.py
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import replace
from pathlib import Path

DB = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\debug\speedrush")
GAMES = {
    "程序A": ["20261016_130735_p1", "20261016_130819_p2"],
    "程序B": ["20261016_135024_p1", "20261016_135108_p2"],
    "人工": ["20261016_160353_p1", "20261016_160437_p2"],
}
GAP_MIN = 3.0          # 长空窗界（s，§1.1 同口径）
CHANGE_EPS_M = 0.05    # 最佳候选改变的位移界（米）

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.corridor_reach import (  # noqa: E402
    CarSource, CorridorBandSpan, evaluate)
from maaracing_master.plugins.speedrush.decision import DecisionEngine  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    load_calib, t_meet_car_s)


def build_params():
    """产线参数面单一真源：引擎直构（闸开）拿 ReachParams，闸关副本派生。
    R2 副本：hug+far（w=1 定标，扫描时按 w 线性缩放——capped 封顶在聚合内，
    w 缩放保持线性）；R3-only 副本（far 关）作 R2 影子的基线。"""
    cfg = load_decision()
    cfg = replace(cfg, corridor_reach=replace(
        cfg.corridor_reach, overtake_hunting=True, hug_value=True))
    p_on = DecisionEngine(cfg)._reach_params
    p_off = replace(p_on, hug_d_lo_m=None, hug_d_hi_m=None,
                    hug_p_center_m=None, hug_p_scale_m=None,
                    hug_value_limit=0.0)
    p_r2 = replace(p_on, far_car_weight=1.0, hunt_horizon_s=6.0,
                   far_aggregation="max")
    p_r3 = replace(p_on, far_car_weight=0.0, hunt_horizon_s=None)
    return p_on, p_off, p_r2, p_r3


def load_ticks(ph):
    return [json.loads(l) for l in (DB / ph / f"trace_{ph}.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]


class Replayer:
    def __init__(self, p_on, p_off, p_r2, p_r3):
        self.p_on = p_on
        self.p_off = p_off
        self.p_r2 = p_r2
        self.p_r3 = p_r3
        self.cal = load_calib()
        self.lane_w = self.cal.lane_w_m

    def tick_cars(self, d, fid_hz):
        """car_pos ∪ dist join → 车侧仪器（车相对横位米 + 到站）。
        米制锚（m>0 且 v>0）→ t_meet=t_overtake=m/v；其余像素回退地面流。"""
        xs = {c["id"]: c.get("x") for c in (d.get("car_pos") or [])}
        rels = {c["id"]: (c.get("cy"), c.get("rel"))
                for c in (d.get("car_pos") or [])}
        dist = {e["id"]: (e.get("m"), e.get("v"))
                for e in (d.get("dist") or [])}
        out = []
        for cid in set(xs) | set(dist):
            x_lane = xs.get(cid)
            if x_lane is None:
                continue                        # 无横向读数（远轨）：不评 d̂
            m, v = dist.get(cid, (None, None))
            if m is not None and v is not None and m > 0.0 and v > 0.0:
                t = m / v                       # 米制锚前车（被接近）
                out.append((cid, x_lane * self.lane_w, t, t, True))
                continue
            cy_rel = rels.get(cid)
            t = None
            if cy_rel is not None and cy_rel[1] is not None \
                    and cy_rel[1] > 0 and fid_hz:
                t = t_meet_car_s(cy_rel[1], cy_rel[0], self.cal, fid_hz)
                if not math.isfinite(t):
                    t = None
            out.append((cid, x_lane * self.lane_w, t, None, False))
        return out

    def _reconstruct(self, d):
        """一拍重建 → (xs, sources, shift, cr_scores | None, 跳过原因)。
        span 重建：中心=ro_off 空气墙口径，宽度=日志候选数×步进（带缘证据
        不落 trace，边缘误差 ≤半候选；保真面=cr_best 界外计数）。"""
        ro, cr_scores = d.get("ro_off"), d.get("cr_scores")
        executed = d.get("executed_lane")
        if ro is None or cr_scores is None or executed is None or not cr_scores:
            return None, None, None, None, "no_input"
        shift = executed * self.lane_w
        center = (-ro) * self.lane_w + shift
        n = len(cr_scores)
        width = n * self.p_on.step_m
        xs = [center - width / 2.0 + (i + 0.5) * self.p_on.step_m
              for i in range(n)]
        sources = [CarSource(x_m=x + shift, t_meet_s=tm, t_overtake_s=tov)
                   for _cid, x, tm, tov, _met in self.tick_cars(d, d.get("_fid_hz"))]
        return xs, sources, shift, cr_scores, None

    def replay_tick(self, d, fid_hz, p_on, p_off):
        """一拍 → (评估结果 dict | None, 跳过原因 | None)。R3 贴身 E 项的
        逐候选增量 Δ（闸开−闸关，coins=[] 使币侧抵消）。"""
        d = dict(d, _fid_hz=fid_hz)
        xs, sources, shift, cr_scores, why = self._reconstruct(d)
        if why is not None:
            return None, why
        left, right = xs[0] - self.p_on.step_m / 2.0, xs[-1] + self.p_on.step_m / 2.0
        bands = [CorridorBandSpan(0.0, 3.0, left, right)]
        cars = self.tick_cars(d, fid_hz)
        rep_on = evaluate(bands, (), sources, shift, p_on)
        rep_off = evaluate(bands, (), sources, shift, p_off)
        if rep_on is None or rep_off is None:
            return None, "eval_none"
        n = len(cr_scores)
        delta = []
        for i in range(n):
            if cr_scores[i] is not None and cr_scores[i] <= -99.0:
                delta.append(0.0)               # crash 候选：R3 不动
                continue
            delta.append(rep_on.candidates[i].score
                         - rep_off.candidates[i].score)
        new_scores = [s + dl for s, dl in zip(cr_scores, delta)]
        return dict(xs=xs, cars=cars, shift=shift, delta=delta,
                    new_scores=new_scores, left=left, right=right), None

    def far_values_tick(self, d, fid_hz):
        """一拍 R2 原料：w=1 的逐候选 far_value（三聚合）+ hug 增量 + 车侧。
        R2 影子基线=R3 已应用分（cr+hug 增量），变体=+w×far 原始值。"""
        d = dict(d, _fid_hz=fid_hz)
        xs, sources, shift, cr_scores, why = self._reconstruct(d)
        if why is not None:
            return None, why
        left, right = xs[0] - self.p_on.step_m / 2.0, xs[-1] + self.p_on.step_m / 2.0
        bands = [CorridorBandSpan(0.0, 3.0, left, right)]
        cars = self.tick_cars(d, fid_hz)
        rep_r2 = evaluate(bands, (), sources, shift, self.p_r2)
        rep_r3 = evaluate(bands, (), sources, shift, self.p_r3)
        rep_off = evaluate(bands, (), sources, shift, self.p_off)
        if rep_r2 is None or rep_r3 is None or rep_off is None:
            return None, "eval_none"
        n = len(cr_scores)
        far_raw = {agg: [c.far_value for c in
                         evaluate(bands, (), sources, shift,
                                  replace(self.p_r2, far_aggregation=agg))
                         .candidates]
                   for agg in ("max", "sum", "capped")}
        hug_delta = [rep_r2.candidates[i].hug_value
                     - rep_off.candidates[i].hug_value for i in range(n)]
        baseline = []
        for i in range(n):
            s = cr_scores[i]
            dl = 0.0 if (s is not None and s <= -99.0) else hug_delta[i]
            baseline.append((s or 0.0) + dl)
        return dict(xs=xs, cars=cars, shift=shift, baseline=baseline,
                    far_raw=far_raw), None


def metric_cars(cars, horizon):
    """米制锚前车里 t_ov ≤ horizon 的（被接近 population）。"""
    return [(cid, x, tov) for cid, x, tm, tov, met in cars
            if met and tov is not None and tov <= horizon]


def need_of(p_on, shift, x):
    return p_on.need_of_dx_m(abs(x - shift))


def game_report(name, phases, rep):
    ticks = []
    for ph in phases:
        ticks += load_ticks(ph)
    fid_hz_of = {}
    for i in range(1, len(ticks)):
        dt = (ticks[i]["ts_ns"] - ticks[i - 1]["ts_ns"]) / 1e9
        df = ticks[i]["fid"] - ticks[i - 1]["fid"]
        if dt > 0 and df > 0:
            fid_hz_of[i] = df / dt
    pass_ts = sorted(d["ts_ns"] for d in ticks for _ in (d.get("passes") or []))
    gaps = [(pass_ts[i], pass_ts[i + 1]) for i in range(len(pass_ts) - 1)
            if (pass_ts[i + 1] - pass_ts[i]) / 1e9 > GAP_MIN]

    stats = dict(evaluable=0, skips={}, best_change=0, dx_list=[],
                 hug_entry=0, hug_entry_raw=0, gate_pairs=0, gate_rej=0,
                 gate_tov=[], win_cands=0, win_crash=0, met_pairs=0,
                 cr_best_out=0)
    seg_stats = [dict(gap=(g0, g1), **{k: 0 for k in
                                       ("eval", "change", "hug", "gate_p",
                                        "gate_r", "win", "crash")})
                 for g0, g1 in gaps]

    for i, d in enumerate(ticks):
        in_seg = None
        for si, (g0, g1) in enumerate(gaps):
            if g0 <= d["ts_ns"] <= g1:
                in_seg = si
                break
        r, why = rep.replay_tick(d, fid_hz_of.get(i), rep.p_on, rep.p_off)
        if r is None:
            stats["skips"][why] = stats["skips"].get(why, 0) + 1
            continue
        stats["evaluable"] += 1
        if in_seg is not None:
            seg_stats[in_seg]["eval"] += 1
        if not (r["left"] - 0.5 <= (d.get("cr_best") or r["left"])
                <= r["right"] + 0.5):
            stats["cr_best_out"] += 1
        xs, new_scores, cr = r["xs"], r["new_scores"], d["cr_scores"]
        old_i = max(range(len(cr)), key=lambda k: cr[k])
        new_i = max(range(len(new_scores)), key=lambda k: new_scores[k])
        dx = abs(xs[new_i] - xs[old_i])
        changed = new_i != old_i and dx >= CHANGE_EPS_M
        if changed:
            stats["best_change"] += 1
            stats["dx_list"].append(dx)
            if in_seg is not None:
                seg_stats[in_seg]["change"] += 1
        # ② 新胜出位落某前车贴邻窗内（原始=落窗即可，可达=门过）
        horizon = rep.p_on.horizon_s
        best_x = xs[new_i]
        for _cid, cx, tov in metric_cars(r["cars"], horizon):
            dh = abs(cx - best_x)
            if rep.p_on.hug_d_lo_m <= dh <= rep.p_on.hug_d_hi_m:
                stats["hug_entry_raw"] += 1
                need = need_of(rep.p_on, r["shift"], best_x)
                if tov >= need + rep.p_on.tau_s + rep.p_on.margin_s:
                    stats["hug_entry"] += 1
                    if in_seg is not None:
                        seg_stats[in_seg]["hug"] += 1
                break
        # ③④ 逐前车门判定 + 贴邻窗候选安全面
        for _cid, cx, tov in metric_cars(r["cars"], horizon):
            stats["met_pairs"] += 1
            stats["gate_pairs"] += 1
            if in_seg is not None:
                seg_stats[in_seg]["gate_p"] += 1
            # 该前车两侧贴邻窗中心候选的可达性（任一侧可达=门过）
            win = [x for x in xs
                   if rep.p_on.hug_d_lo_m <= abs(cx - x)
                   <= rep.p_on.hug_d_hi_m]
            ok = False
            for x in win:
                need = need_of(rep.p_on, r["shift"], x)
                if tov >= need + rep.p_on.tau_s + rep.p_on.margin_s:
                    ok = True
                    break
            stats["gate_tov"].append(tov)
            if win and not ok:
                stats["gate_rej"] += 1
                if in_seg is not None:
                    seg_stats[in_seg]["gate_r"] += 1
        for x in xs:
            if not any(rep.p_on.hug_d_lo_m <= abs(cx - x)
                       <= rep.p_on.hug_d_hi_m
                       for _c, cx, _t in metric_cars(r["cars"], horizon)):
                continue
            stats["win_cands"] += 1
            need = need_of(rep.p_on, r["shift"], x)
            crash = any(tm is not None and need <= tm < rep.p_on.crash_t_s
                        for _c2, _x2, tm, _t2, _m2 in r["cars"])
            if crash:
                stats["win_crash"] += 1
                if in_seg is not None:
                    seg_stats[in_seg]["crash"] += 1
    _print_game(name, ticks, gaps, stats, seg_stats)


def _print_game(name, ticks, gaps, s, segs):
    n = max(1, s["evaluable"])
    print(f"\n== {name}：{len(ticks)} 拍（长空窗 {len(gaps)} 个）==")
    print(f"  可评估拍 {s['evaluable']}（跳过 {s['skips']}）  "
          f"cr_best 界外拍 {s['cr_best_out']}（span 保真面）")
    print(f"  ① 最佳候选改变 {s['best_change']} 拍（{100*s['best_change']/n:.1f}%）"
          + (f"  平均|Δx|={statistics.mean(s['dx_list']):.2f}m "
             f"P50={statistics.median(s['dx_list']):.2f}m" if s["dx_list"] else ""))
    print(f"  ② 胜出位进贴邻窗 {s['hug_entry_raw']} 拍（{100*s['hug_entry_raw']/n:.1f}%）"
          f"，其中可达 {s['hug_entry']} 拍（{100*s['hug_entry']/n:.1f}%）")
    gp = max(1, s["gate_pairs"])
    print(f"  ③ 可达性门拒绝 {s['gate_rej']}/{s['gate_pairs']}"
          f"（{100*s['gate_rej']/gp:.1f}%）  米制锚前车对 {s['met_pairs']}"
          + (f"  t_ov P50={statistics.median(s['gate_tov']):.2f}s"
             if s["gate_tov"] else ""))
    wp = max(1, s["win_cands"])
    print(f"  ④ 贴邻窗候选 crash {s['win_crash']}/{s['win_cands']}"
          f"（{100*s['win_crash']/wp:.1f}%）")
    base = ticks[0]["ts_ns"]
    for i, g in enumerate(segs):
        g0, g1 = g["gap"]
        ev = max(1, g["eval"])
        print(f"    空窗{i} t={(g0-base)/1e9:6.1f}→{(g1-base)/1e9:6.1f}s "
              f"({(g1-g0)/1e9:4.1f}s)  可评估={g['eval']:3d}  改变={g['change']:3d} "
              f"进窗={g['hug']:3d}  门拒={g['gate_r']}/{g['gate_p']}  "
              f"窗crash={g['crash']}")


def r2_report(name, phases, rep, weight_grid=(0.1, 0.2, 0.3, 0.5, 0.8, 1.0)):
    """R2 影子（设计稿 §2.4）：三聚合 × 权重网格的预注册门槛四项。
    基线=R3 已应用分（cr+hug 增量），变体=基线+w×far 原始值（far_value
    即 R2 逐候选增量，审计面直接读）。门槛：①hunt 目标拍覆盖率（w 无关）
    ②候选排名改变次数 ③改变后方向可达率（新胜出位落某远车贴邻窗=预选位
    达成，离线代理）④veto/取消增量代理（新胜出位距任一车 < veto 接触界）。"""
    ticks = []
    for ph in phases:
        ticks += load_ticks(ph)
    fid_hz_of = {}
    for i in range(1, len(ticks)):
        dt = (ticks[i]["ts_ns"] - ticks[i - 1]["ts_ns"]) / 1e9
        df = ticks[i]["fid"] - ticks[i - 1]["fid"]
        if dt > 0 and df > 0:
            fid_hz_of[i] = df / dt
    stats = dict(evaluable=0, skips={}, covered=0, cr_best_out=0)
    rows = {agg: {w: dict(change=0, pre=0, veto=0) for w in weight_grid}
            for agg in ("max", "sum", "capped")}
    veto_gap_m = 0.45 * rep.lane_w
    for i, d in enumerate(ticks):
        r, why = rep.far_values_tick(d, fid_hz_of.get(i))
        if r is None:
            stats["skips"][why] = stats["skips"].get(why, 0) + 1
            continue
        stats["evaluable"] += 1
        xs, cars, far_raw = r["xs"], r["cars"], r["far_raw"]
        if any(tov is not None and 2.0 < tov <= 6.0
               for _c, _x, tm, tov, met in cars if met):
            stats["covered"] += 1
        baseline = r["baseline"]
        old_i = max(range(len(baseline)), key=lambda k: baseline[k])
        for agg in ("max", "sum", "capped"):
            for w in weight_grid:
                variant = [b + w * f for b, f in zip(baseline, far_raw[agg])]
                new_i = max(range(len(variant)), key=lambda k: variant[k])
                if new_i == old_i:
                    continue
                dx = abs(xs[new_i] - xs[old_i])
                if dx < CHANGE_EPS_M:
                    continue
                row = rows[agg][w]
                row["change"] += 1
                best_x = xs[new_i]
                # ③ 预选位达成：新胜出位落某远车（t_ov∈(2,6]）贴邻窗内
                if any(rep.p_on.hug_d_lo_m <= abs(cx - best_x)
                       <= rep.p_on.hug_d_hi_m
                       for _c, cx, tm, tov, met in cars
                       if met and tov is not None and 2.0 < tov <= 6.0):
                    row["pre"] += 1
                # ④ veto 代理：新胜出位距任一车 < 接触界
                if any(abs(cx - best_x) < veto_gap_m
                       for _c2, cx, _tm, _tov, _m in cars):
                    row["veto"] += 1
    n = max(1, stats["evaluable"])
    print(f"\n== R2 影子 {name}：可评估 {stats['evaluable']}（跳过 {stats['skips']}）==")
    print(f"  ① hunt 目标拍覆盖率（远车 t_ov∈(2,6]） {stats['covered']}/{n}"
          f"（{100*stats['covered']/n:.1f}%）")
    for agg in ("max", "sum", "capped"):
        for w in weight_grid:
            row = rows[agg][w]
            ch = row["change"]
            print(f"    {agg:6s} w={w:.1f}: ②改变 {ch:3d}（{100*ch/n:4.1f}%）  "
                  f"③预选位达成 {row['pre']:3d}（{100*row['pre']/max(1,ch):4.0f}%）  "
                  f"④veto代理 {row['veto']:3d}（{100*row['veto']/max(1,ch):4.0f}%）")


def main():
    p_on, p_off, p_r2, p_r3 = build_params()
    print(f"参数面：lo={p_on.hug_d_lo_m:.3f}m hi={p_on.hug_d_hi_m:.3f}m "
          f"P中心={p_on.hug_p_center_m:.3f}m 尺度={p_on.hug_p_scale_m:.3f}m "
          f"limit={p_on.hug_value_limit:.0f} horizon={p_on.horizon_s}s "
          f"hunt_horizon={p_r2.hunt_horizon_s}s")
    rep = Replayer(p_on, p_off, p_r2, p_r3)
    for name, phases in GAMES.items():
        game_report(name, phases, rep)
        r2_report(name, phases, rep)


if __name__ == "__main__":
    main()
