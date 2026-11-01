# -*- coding: utf-8 -*-
"""两局对照：基线局 vs 新局，逐项列差值（第 5 期验收的一键入口）。

复用同目录两个分析器（baseline_replay / map_support），不重写口径。
输出只做对照，不设通过阈值——阈值与判据写在方案第 5 步，由人按局裁定。

用法::

    python tools/experiments/world_state/acceptance.py \
        --baseline 20261101_113145 --new 20261101_xxxxxx [--new ...] \
        [--json out.json]

旧局没有 `v_ego`/`otab`/`map` 时，对应项显示 `None`，不拿缺列当零。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import baseline_replay as B  # noqa: E402
import map_support as M  # noqa: E402
from speedrush_trace_reader import iter_rows  # noqa: E402


def _path(session: str) -> Path:
    import os
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def _switches(rows) -> dict:
    """从 trace 判断机制开关是否真的开了。

    `src_elig` 只有资格闭锁开时才落；`route.rh` 是请求保持开关；`route.af`
    是承诺道跟随开关。旧局两者都没有，如实报 False，不拿缺列当"开"。
    """
    latch = any(((r.get("sel_dbg") or {}).get("src_elig") is not None)
                for r in rows)
    hold = any(bool((r.get("route") or {}).get("rh")) for r in rows)
    anchor = any(bool((r.get("route") or {}).get("af")) for r in rows)
    return {"latch": latch, "hold": hold, "anchor": anchor}


def _games(rows) -> int:
    """会话里跑了几场：阶段号回落（2→1）即新的一场。"""
    games, prev = (1 if rows else 0), None
    for r in rows:
        s = r.get("stage_id")
        if prev is not None and s is not None and s < prev:
            games += 1
        if s is not None:
            prev = s
    return games


def _game_slices(rows) -> list[list]:
    """按场切开：阶段号回落处的下一拍是新的一场。"""
    cuts, prev = [], None
    for i, r in enumerate(rows):
        s = r.get("stage_id")
        if prev is not None and s is not None and s < prev:
            cuts.append(i)
        if s is not None:
            prev = s
    edges = [0, *cuts, len(rows)]
    return [rows[a:b] for a, b in zip(edges, edges[1:]) if b > a]


def _analyze_rows(rows, session: str, speed="v_ego",
                  speed_value=41.0) -> dict:
    b = {
        "plan": B._plan(rows),
        "layers": B._layers(rows),
        "merges": B._merges(rows),
        "requests": B._requests(rows),
        "sources": B._source_flips(rows),
        "coins": B._coin_life(rows),
        "car_tracks": B._car_tracks(rows),
        "group_blind": B._group_blind(rows),
        "opps": B._opps(rows),
        "steer": B._steer(rows),
        "map": M.analyze(rows, session, speed, speed_value),
        "route": B._route(rows),
        "switches": _switches(rows),
        "games": _games(rows),
        "has_new_columns": all(
            k in rows[-1] for k in
            ("otab", "map", "open", "anchor", "h_shadow", "trig", "v_ego")),
    }
    return b


def analyze(session: str, speed="v_ego", speed_value=41.0) -> dict:
    rows = list(iter_rows(_path(session)))
    if not rows:
        raise SystemExit(f"没有数据行：{session}")
    return _analyze_rows(rows, session, speed, speed_value)


def analyze_games(session: str, speed="v_ego",
                  speed_value=41.0) -> dict[str, dict]:
    """会话 → {场名: 分析}；一场一个名字，多场自动拆成 `会话#1/#2`。

    计数类指标（并线、切换、no_speed 秒数）按整段累加，同会话多场混算会
    整体翻倍；验收按场分别判。
    """
    rows = list(iter_rows(_path(session)))
    if not rows:
        raise SystemExit(f"没有数据行：{session}")
    slices = _game_slices(rows)
    if len(slices) <= 1:
        return {session: _analyze_rows(rows, session, speed, speed_value)}
    return {f"{session}#{i}": _analyze_rows(part, session, speed, speed_value)
            for i, part in enumerate(slices, 1)}


def flat(rep: dict) -> dict:
    p, ly, m, r, s = (rep["plan"], rep["layers"], rep["merges"],
                      rep["requests"], rep["steer"])
    mp = rep["map"]
    e = mp["err_by_distance"]
    return {
        "plan.valid_rate": p["valid_rate"],
        "plan.chosen_switches": p["chosen_switches"],
        "plan.layer_switches": p["layer_switches"],
        "plan.no_speed_s": p["no_speed_s"],
        "plan.no_speed_cruise_s": p["no_speed_cruise_s"],
        "plan.merge_ns_frac": p["merge_ns_frac"],
        "plan.lead_valid_rate": p["lead_valid_rate"],
        "layers.switches": ly["switches"],
        "merges.count": m["count"],
        "merges.opportunity": m["opportunity"],
        "merges.urgent": m["urgent"],
        "merges.v_lat_median": m["v_lat_lane_s_median"],
        "merges.overshoot_p90": m["overshoot_lane_p90"],
        "merges.overshoot_in_p90": m["overshoot_in_p90"],
        "merges.overshoot_post_p90": m["overshoot_post_p90"],
        "merges.post_drift_over_0.5": m["post_drift_over_0_5"],
        "merges.settle_p90_finished": m["settle_err_lane_p90_finished"],
        "merges.finish_share": m["finish_share"],
        "merges.overshoot_in_median": m["overshoot_in_median"],
        "merges.overshoot_in_n": m["overshoot_in_n"],
        "requests.segments": r["segments"],
        "requests.life_s_median": r["life_s_median"],
        "requests.life_s_p90": r["life_s_p90"],
        "requests.conversion": r["conversion"],
        "requests.conversion_any": r["conversion_any"],
        "sources.switch_per_s": rep["sources"]["per_s"],
        "sources.visible_s": rep["sources"]["visible_s"],
        "switches.latch": rep["switches"]["latch"],
        "switches.hold": rep["switches"]["hold"],
        "switches.anchor": rep["switches"]["anchor"],
        "route.mismatch_frac": rep["route"]["mismatch_frac"],
        "route.anchor_events": rep["route"]["anchor_events"],
        "route.lane_drift_ticks": rep["route"]["lane_drift_ticks"],
        "route.already_there": rep["route"]["already_there"],
        "session.games": rep["games"],
        "steer.in_band": s["in_band_abs_mean"],
        "steer.in_band_flat": s["in_band_flat"],
        "steer.in_band_curve": s["in_band_curve"],
        "steer.reverse_s": s["reverse_per_s"],
        "steer.reverse_far_s": s["reverse_far_per_s"],
        "steer.reverse_far_large_s": s["reverse_far_large_per_s"],
        "steer.track_err_p50": s["track_err_p50"],
        "steer.track_err_p90": s["track_err_p90"],
        "steer.track_err_over_0.5": s["track_err_over_0_5"],
        "coins.life_median": rep["coins"]["life_s_median"],
        "cgroups.blind_p90": rep["group_blind"]["blind_s_p90"],
        "cgroups.blind_over_0.6s": rep["group_blind"]["blind_over_0_6s"],
        "cars.occl_p90": rep["car_tracks"]["occl_max_age_s_p90"],
        "cars.occl_over_0.6s": rep["car_tracks"]["runs_over_0_6s"],
        "cars.blind_s": rep["car_tracks"]["blind_s"],
        "opps.coin_slack_median": rep["opps"]["coin_slack_median"],
        "map.support_median": mp["support_m"]["median"],
        "map.err_0-10m": e["0-10m"]["abs_median"],
        "map.err_10-20m": e["10-20m"]["abs_median"],
        "map.err_20-30m": e["20-30m"]["abs_median"],
        "map.forward_speed": mp["forward_speed"],
        "trace.new_columns": rep["has_new_columns"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="append", default=[],
                    help="基线局；可重复。首个用于逐项对照，全部用于 "
                         "--gate6h 的基线分布（同赛道多局）")
    ap.add_argument("--new", action="append", default=[])
    ap.add_argument("--json")
    ap.add_argument("--speed", choices=("v_ego", "hud41", "const"),
                    default="v_ego")
    ap.add_argument("--speed-value", type=float, default=41.0)
    ap.add_argument("--gate", action="store_true",
                    help="按方案第 5 期判据逐项判 PASS/FAIL；任一 FAIL 退出 1")
    ap.add_argument("--gate6", action="store_true",
                    help="第 6 期验收：原闸门（过冲改用并线段内）＋ 两开关"
                         "已开、请求寿命 ≥0.3 秒、机会并线 >1、源切换降 50%、"
                         "urgent 不增；任一 FAIL 退出 1")
    ap.add_argument("--gate-anchor", action="store_true",
                    help="6h 验收：原闸门（过冲改用并线段内）＋ anchor_follow "
                         "已开、承诺道错位至少比基线低 5pt、机会并线 >1、"
                         "urgent 不超基线 3 段；任一 FAIL 退出 1")
    ap.add_argument("--gate6h", action="store_true",
                    help="指标审计后的第 6 期闸门：机制项（anchor/错位/机会/"
                         "urgent）＋ 去偏控制项（跟踪误差、远离并线的大杆值反向、"
                         "并线期无计划占比、完成率、过冲中位），控制项以全部"
                         "基线局分布的 p90/p10 为界")
    args = ap.parse_args(argv)
    bases = args.baseline or ["20261101_113145"]
    base = flat(next(iter(analyze_games(
        bases[0], args.speed, args.speed_value).values())))
    pool = []
    for s in bases:
        for rep in analyze_games(s, args.speed, args.speed_value).values():
            pool.append(flat(rep))
    news = {}
    for s in args.new:
        for key, rep in analyze_games(s, args.speed, args.speed_value).items():
            news[key] = flat(rep)
    rep = {"baseline": bases,
           "baseline_metrics": base,
           "baseline_pool": pool,
           "new": {s: v for s, v in news.items()}}
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    keys = list(base)
    print(f"{'metric':30s} {'base':>10s}  " + "  ".join(
        f"{s[-6:]:>10s}" for s in news))
    for k in keys:
        if k in ("map.forward_speed", "trace.new_columns", "session.games",
                 "switches.latch", "switches.hold", "switches.anchor"):
            print(f"{k:30s} {str(base[k])[:10]:>10s}  " + "  ".join(
                str(news[s][k])[:10].rjust(10) for s in news))
            continue

        def fmt(v):
            return "None" if v is None else f"{v:.3f}"

        print(f"{k:30s} {fmt(base[k]):>10s}  " + "  ".join(
            fmt(news[s][k]).rjust(10) for s in news))
    if args.gate6h:
        return _gate6h(base, news, pool)
    if args.gate6:
        return _gate(base, news, phase6=True)
    if args.gate_anchor:
        return _gate(base, news, anchor=True)
    if args.gate:
        return _gate(base, news)
    return 0


def _gate(base: dict, news: dict, phase6: bool = False,
          anchor: bool = False) -> int:
    """方案第 5 期硬判据；phase6/anchor 时按对应期结果层加判。

    容差只吸收测量噪声，不是放宽目标。第 6 期把总过冲换成**并线段内**
    过冲：结束后 1 秒窗口会抓到执行横位的观测/模型源切换（2026-11-01 第 2
    局复算：段内 p90 0.011、结束后 p90 0.997），不能全算控制过冲；结束后
    漂移仍逐项打印，供人工复核。
    """
    rules = [
        ("plan.valid_rate", ">=", -0.03, "计划有效率不降 3pt"),
        ("plan.no_speed_s", "<=", 1.0, "no_speed 不增 1s"),
        ("steer.in_band", "<=", 0.02, "巡航目标内杆量不增 0.02"),
        ("steer.reverse_s", "<=", 0.15, "反向率不增 0.15/s"),
        ("merges.overshoot_in_p90" if (phase6 or anchor)
         else "merges.overshoot_p90", "<=", 0.10,
         "并线段内过冲 p90 不增 0.10 道" if (phase6 or anchor)
         else "并线过冲 p90 不增 0.10 道"),
        ("requests.conversion", ">=", 0.0, "请求→并线转化（仅 opportunity）不低于基线"),
    ]
    bad = 0
    for sess, new in news.items():
        fails = []
        for key, op, tol, why in rules:
            b, n = base.get(key), new.get(key)
            if b is None or n is None:
                fails.append(f"{key}: 基线或新局缺值")
                continue
            limit = b + tol
            ok = n <= limit if op == "<=" else n >= limit
            if not ok:
                fails.append(f"{key}: {n:.3f} vs 基线 {b:.3f}（{why}）")
        g = new.get("session.games")
        if g is not None and g != 1:
            fails.append(f"session.games: 一个会话里跑了 {g} 场，计数类指标"
                         "（并线/切换/no_speed）不可比，请每场单独会话跑")
        if phase6:
            if not (new.get("switches.latch") and new.get("switches.hold")):
                fails.append("switches: 本局未同时打开 eligibility_latch 与 "
                             "request_hold（trace 缺 src_elig 或 route.rh）")
            life = new.get("requests.life_s_median")
            if life is None or life + 1e-9 < 0.3:
                fails.append(f"requests.life_s_median: {life} < 0.3 秒")
            opp = new.get("merges.opportunity")
            if opp is None or opp <= 1:
                fails.append(f"merges.opportunity: {opp} 段，未超过基线 1 段/局")
            bsrc, nsrc = base.get("sources.switch_per_s"), \
                new.get("sources.switch_per_s")
            if bsrc is None or nsrc is None:
                fails.append("sources.switch_per_s: 基线或新局缺值")
            elif nsrc > 0.5 * bsrc + 1e-9:
                fails.append(f"sources.switch_per_s: {nsrc:.3f} 未降到基线 "
                             f"{bsrc:.3f} 的一半")
            bur, nur = base.get("merges.urgent"), new.get("merges.urgent")
            if bur is None or nur is None:
                fails.append("merges.urgent: 基线或新局缺值")
            elif nur > bur:
                fails.append(f"merges.urgent: {nur} 段，高于基线 {bur} 段")
        if anchor:
            if not new.get("switches.anchor"):
                fails.append("switches: 本局未打开 anchor_follow（trace 缺 "
                             "route.af）")
            bmis, nmis = base.get("route.mismatch_frac"), \
                new.get("route.mismatch_frac")
            if bmis is None or nmis is None:
                fails.append("route.mismatch_frac: 基线或新局缺值")
            elif nmis > bmis - 0.05 + 1e-9:
                fails.append(f"route.mismatch_frac: {nmis:.3f}，未比基线 "
                             f"{bmis:.3f} 低 5pt（承诺道仍在错的道上）")
            opp = new.get("merges.opportunity")
            if opp is None or opp <= 1:
                fails.append(f"merges.opportunity: {opp} 段，未超过基线 1 段/局")
            bur, nur = base.get("merges.urgent"), new.get("merges.urgent")
            if bur is None or nur is None:
                fails.append("merges.urgent: 基线或新局缺值")
            elif nur > bur + 3:
                fails.append(f"merges.urgent: {nur} 段，超基线 {bur} 段 3 段以上"
                             "（车道跟随可能把逃生并线放大了）")
        if not new.get("trace.new_columns"):
            fails.append("trace.new_columns: 会话缺 otab/map/open/anchor/"
                         "h_shadow/trig/v_ego，不是新版本产物")
        speed = str(new.get("map.forward_speed") or "")
        if not speed.startswith("v_ego"):
            fails.append("map.forward_speed: 新局缺精确 v_ego（不接受代理跑验收）")
        if fails:
            bad += 1
            print(f"[FAIL] {sess}")
            for f in fails:
                print("  - " + f)
        else:
            print(f"[PASS] {sess}")
    return 1 if bad else 0


def _pool_thr(pool, key, q):
    vals = [p[key] for p in pool if p.get(key) is not None]
    return B._pct(vals, q) if vals else None


def _gate6h(base: dict, news: dict, pool: list[dict]) -> int:
    """第 6 期（6h）验收：机制项与去偏后的控制项分开判。

    旧闸门的两个问题（2026-11-01 审计，见 metric_audit.py）：
    - `plan.no_speed_s` 被直道巡航主导（低弯桶无速度占比 27.7%，高弯桶
      7.5%；48 局里 36 局在 no_speed 拍的保持误差反而更小），不能拿总量
      当退化证据；
    - 并线过冲 p90 实际只覆盖 1~5 个「真走到目标」的段，等于取最大值，且
      把没走到的段排除在外。
    这里改为：无速度只看并线期占比；控制看目标跟踪误差、远离并线的大杆值
    反向率、并线完成率与过冲中位。控制项以**全部基线局**分布的 p90/p10 为
    界，吸收赛道与驾驶活跃度差异（同赛道多局基线，单局不再唯一）。
    """
    bad = 0
    for sess, new in news.items():
        fails = []
        notes = []

        def chk(key, op, q, why):
            t = _pool_thr(pool, key, q)
            n = new.get(key)
            if t is None or n is None:
                notes.append(f"{key}: 基线分布或新局缺值，跳过（观测）")
                return
            limit = t
            ok = n <= limit if op == "<=" else n >= limit
            if not ok:
                fails.append(f"{key}: {n:.3f} vs 基线分布 "
                             f"{'p90' if op == '<=' else 'p10'} {t:.3f}（{why}）")

        # 控制项：去偏后的结果面。反向率不进门禁：并线密集的局里「远离并线」
        # 只剩几秒，反向计数在 2~3 次/秒之间跳，无法区分行为变化（审计见
        # metric_audit.py）；保留打印作观测。
        chk("steer.track_err_p50", "<=", 90, "巡航目标跟踪误差中位")
        chk("steer.track_err_p90", "<=", 90, "巡航目标跟踪误差 p90")
        notes.append(f"观测 no_speed 总 {new.get('plan.no_speed_s')}s / "
                     f"巡航段 {new.get('plan.no_speed_cruise_s')}s；"
                     f"杆量 {new.get('steer.in_band')}；反向 "
                     f"{new.get('steer.reverse_s')} 次/秒")
        nm = new.get("merges.count") or 0
        if nm >= 5:
            chk("plan.merge_ns_frac", "<=", 90, "并线期无计划占比")
            chk("merges.finish_share", ">=", 10, "并线走到目标的占比")
        else:
            notes.append(f"merges.count={nm} <5，并线期/完成率只观测")
        # 过冲只在少数「段内真走到目标」的段上可测（审计：每局 1~5 段），
        # 分位就是最大值，不做硬判，只打印供人工复核。
        notes.append(f"观测并线段内过冲 中位 {new.get('merges.overshoot_in_median')} "
                     f"道（n={new.get('merges.overshoot_in_n')}）")

        # 机制项：6h 开关与承诺道/机会面
        if not new.get("switches.anchor"):
            fails.append("switches: 本局未打开 anchor_follow（trace 缺 route.af）")
        bmis, nmis = base.get("route.mismatch_frac"), \
            new.get("route.mismatch_frac")
        if bmis is None or nmis is None:
            fails.append("route.mismatch_frac: 基线或新局缺值")
        elif nmis > bmis - 0.05 + 1e-9:
            fails.append(f"route.mismatch_frac: {nmis:.3f}，未比基线 "
                         f"{bmis:.3f} 低 5pt（承诺道仍在错的道上）")
        opp = new.get("merges.opportunity")
        if opp is None or opp <= 1:
            fails.append(f"merges.opportunity: {opp} 段，未超过基线 1 段/局")
        bur, nur = _pool_thr(pool, "merges.urgent", 100), \
            new.get("merges.urgent")
        if bur is None or nur is None:
            fails.append("merges.urgent: 基线分布或新局缺值")
        elif nur > bur:
            fails.append(f"merges.urgent: {nur} 段，超基线分布最大 {bur} 段")

        if not new.get("trace.new_columns"):
            fails.append("trace.new_columns: 会话缺 otab/map/open/anchor/"
                         "h_shadow/trig/v_ego，不是新版本产物")
        speed = str(new.get("map.forward_speed") or "")
        if not speed.startswith("v_ego"):
            fails.append("map.forward_speed: 新局缺精确 v_ego")

        if fails:
            bad += 1
            print(f"[FAIL] {sess}")
            for f in fails:
                print("  - " + f)
        else:
            print(f"[PASS] {sess}")
        for n in notes:
            print("  · " + n)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
