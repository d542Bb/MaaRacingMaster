# -*- coding: utf-8 -*-
"""世界状态基线：只读一局 speedrush 控制流水，输出可复算的行为指标。

不改生产行为；新列（schema 4：对象 id/seen、sel_dbg.opp、cgroups.mids）存在
就用，不存在就如实标 null 并记进 missing。段切分一律按 ts_ns 的 >0.5 s 空档，
不看 fid 差（fid 会因丢帧跳号）。

用法：
    python tools/experiments/world_state/baseline_replay.py --session 20261101_113145
    python tools/experiments/world_state/baseline_replay.py --trace <...>/speedrush.jsonl --json out.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True      # 不在实验区留 __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402

GAP_S = 0.5                 # 段切分空档（秒）
# 反向率/杆量的观测窗：并线本身会回正一次，并线前后 2s 也在为机动做准备，
# 这些反号不是「巡航里左右摇」；混在一起算会让并线多的局反向率必然更高
# （2026-11-01 审计：并线总数与反向计数相关 0.77）。
MERGE_GUARD_S = 2.0
# 大杆值反向门：0.05 死区把零附近的抖动也算一次反号；只看 ≥0.2 的换向，
# 审计中远离并线的反向率在 0.05/0.2 两档差 ~20%，两档结论不同。
REV_LARGE = 0.20
LEAD_NEAR_M = 30.0
# 以下常量与 2026-11-01 的策略一致（planner / selection 的真源在
# resources/policy/decision.json）；旧局没有 slack 列时用它们复算。
LANE_W = 3.5
LEAD_LANE_M = 0.55 * LANE_W
K_LAT = 1.2
TAU = 0.25
HEADING_MAX = 0.15
V_REF = 45.0
MOVE_DT = 0.02
SETTLE = 0.1
REACTION = 0.13
MARGIN = 0.15
REV_DEADBAND = 0.05


def _pct(xs, q):
    """线性插值分位（无外部依赖）。"""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _r(x, nd=3):
    return None if x is None or not math.isfinite(x) else round(x, nd)


def _dt(row):
    d = row.get("dt")
    return float(d) if d else 0.05


def _ts(row):
    return row["ts_ns"] / 1e9


def _lane_move_time(dx_lane):
    """与 planner.lane_move_time 同式（闭环律积分到残差进带）。"""
    e = abs(dx_lane)
    if e <= SETTLE:
        return 0.0
    a = 1.0 - math.exp(-MOVE_DT / TAU)
    psi, t = 0.0, 0.0
    for _ in range(int(10.0 / MOVE_DT)):
        psi_des = math.atan(K_LAT * e * LANE_W / V_REF)
        psi_des = max(-HEADING_MAX, min(HEADING_MAX, psi_des))
        psi += a * (psi_des - psi)
        e -= (V_REF / LANE_W) * math.tan(psi) * MOVE_DT
        t += MOVE_DT
        if e <= SETTLE:
            return t
    return math.inf


def _slack(dx_lane, t):
    if t is None or not math.isfinite(t) or t <= 0.0:
        return None
    if abs(dx_lane) <= SETTLE:
        return t - REACTION
    return t - (_lane_move_time(dx_lane) + REACTION + MARGIN)


def _segments(rows, pred):
    """按时间空档切连续段。"""
    out, cur, prev_t = [], None, None
    for row in rows:
        ok = bool(pred(row))
        t = _ts(row)
        if ok:
            if cur is None or (prev_t is not None and t - prev_t > GAP_S):
                if cur:
                    out.append(cur)
                cur = [row]
            else:
                cur.append(row)
        elif cur:
            out.append(cur)
            cur = None
        prev_t = t
    if cur:
        out.append(cur)
    return out


def _dur(rows):
    return sum(_dt(r) for r in rows[1:]) if len(rows) > 1 else 0.0


def _curve_demand(row) -> float | None:
    """路面弯曲需求（m/m）：前 15~18 m 路心相对 3~6 m 路心的横移率。

    相机固定在车头，直道的路心随距离基本不变；弯道越急，远端路心偏得
    越多。巡航杆量因此天然随赛道变化，跨局比较必须按这一项分桶。"""
    road = (row.get("scene") or {}).get("road") or {}
    near = far = None
    for b in road.get("bands") or ():
        if len(b) < 4 or b[2] is None or b[3] is None:
            continue
        d0 = float(b[0])
        c = 0.5 * (float(b[2]) + float(b[3]))
        if 3.0 <= d0 < 6.0:
            near = c if near is None else 0.5 * (near + c)
        elif 15.0 <= d0 < 18.0:
            far = c if far is None else 0.5 * (far + c)
    if near is None or far is None:
        return None
    return abs(far - near) / 12.0


def _lead_gap(row) -> float | None:
    """正前方同车道最近车的纵向距离（m）；无车或无法判定时 None。

    新局走对象表 `otab`（x_lane/x_m + z_m），旧局退到 `mcars`（x + z 米制）。
    横向门取半道：邻道车不构成「必须用速度预测」的理由。"""
    ego_m = (row.get("scene") or {}).get("ego_m")
    best = None
    for t in (row.get("otab") or {}).get("t") or ():
        if len(t) < 5 or t[0] != "car":
            continue
        if len(t) > 6 and not t[6]:     # 只信本拍可见的位置
            continue
        z = t[4]
        if z is None or z <= 0.0 or z > LEAD_NEAR_M:
            continue
        x = t[3]
        if x is None and t[2] is not None:
            x = t[2] * LANE_W
        if x is None or ego_m is None or abs(x - ego_m) > LEAD_LANE_M:
            continue
        if best is None or z < best:
            best = z
    if best is not None or ego_m is None:
        return best
    for m in row.get("mcars") or ():
        x, z = m.get("x"), m.get("z")
        if x is None or z is None or z <= 0.0 or z > LEAD_NEAR_M:
            continue
        if abs(x - ego_m) <= LEAD_LANE_M and (best is None or z < best):
            best = z
    return best


def _no_speed_ctx(row) -> str:
    """no_speed 拍按「速度是否参与决策」分级；重叠时取更该用速度的一类。"""
    if (row.get("route") or {}).get("ph") == "merging":
        return "merge"
    if _lead_gap(row) is not None:
        return "lead"
    if (row.get("sel_dbg") or {}).get("req") is not None:
        return "request"
    return "cruise"


def _merge_near(rows):
    """并线时刻表 + 判定函数（前后 MERGE_GUARD_S 内算机动窗口）。"""
    ts = sorted(_ts(r) for r in rows
                if (r.get("route") or {}).get("ph") == "merging")

    def near(t: float) -> bool:
        i = bisect.bisect_left(ts, t)
        return ((i < len(ts) and ts[i] - t <= MERGE_GUARD_S)
                or (i > 0 and t - ts[i - 1] <= MERGE_GUARD_S))

    return len(ts), near


def _plan(rows):
    present = [r for r in rows if r.get("plan")]
    reasons = {}
    for r in present:
        k = r["plan"].get("reason")
        reasons[k] = reasons.get(k, 0) + 1
    ns = _segments(rows, lambda r: (r.get("plan") or {}).get("reason")
                   == "plan:no_speed")
    nop = _segments(rows, lambda r: r.get("plan") is None)
    chosen = [r["plan"].get("chosen") for r in present]
    players = [r["plan"].get("layer") for r in present]
    # 无速度时间按场景拆开：直道巡航里估不出速度是「没横向证据」，不代表
    # 计划该在干活（审计：no_speed 拍的保持误差中位 0.35 道，其余拍 0.47；
    # 48 局里 36 局反而更好）。闸门只认并线/近车/请求这些真正用速度的窗口。
    ctx = {}
    merge_s = lead_s = 0.0
    lead_n = lead_ok = 0
    for r in rows:
        dt = _dt(r)
        if (r.get("route") or {}).get("ph") == "merging":
            merge_s += dt
        if _lead_gap(r) is not None:
            lead_s += dt
            if r.get("plan") is not None:
                lead_n += 1
                lead_ok += bool(r["plan"].get("valid"))
        if (r.get("plan") or {}).get("reason") == "plan:no_speed":
            k = _no_speed_ctx(r)
            ctx[k] = ctx.get(k, 0.0) + dt
    need = ctx.get("merge", 0.0) + ctx.get("lead", 0.0) \
        + ctx.get("request", 0.0)
    return {
        "chosen_switches": sum(1 for a, b in zip(chosen, chosen[1:])
                               if a != b),
        "layer_switches": sum(1 for a, b in zip(players, players[1:])
                              if a != b),
        "rows_with_plan": len(present),
        "valid_rate": _r(sum(1 for r in present if r["plan"].get("valid"))
                         / len(present), 3) if present else None,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "no_speed_segments": [[len(s), _r(_dur(s), 1), s[0].get("fid")]
                              for s in ns],
        "no_speed_s": _r(sum(_dur(s) for s in ns), 1),
        "no_speed_cruise_s": _r(ctx.get("cruise", 0.0), 1),
        "no_speed_need_s": _r(need, 1),
        "no_speed_ctx_s": {k: _r(v, 1) for k, v in sorted(ctx.items())},
        "merge_ns_frac": _r(ctx.get("merge", 0.0) / merge_s, 3)
        if merge_s > 0 else None,
        "lead_s": _r(lead_s, 1),
        "lead_valid_rate": _r(lead_ok / lead_n, 3) if lead_n else None,
        "no_plan_s": _r(sum(_dur(s) for s in nop), 1),
    }


def _layers(rows):
    switches, runs = 0, []
    prev, cur = None, None
    for r in rows:
        layer = r.get("sel_layer")
        if layer is None:
            continue
        if cur is None or layer != cur[0]:
            if cur:
                runs.append((cur[0], cur[1]))
            cur = (layer, 0)
            if prev is not None:
                switches += 1
        cur = (layer, cur[1] + 1)
        prev = layer
    if cur:
        runs.append((cur[0], cur[1]))
    counts = {}
    for layer, n in runs:
        counts[str(layer)] = counts.get(str(layer), 0) + n
    return {"switches": switches, "run_ticks": counts,
            "dwell_ticks_median": _r(_pct([n for _, n in runs], 50), 1)}


def _merges(rows):
    """并线段：速度、过冲、收尾误差与结束方式。

    结束时下一拍的 route.vd 决定归账：finish=位置进中心带且横速落稳；
    cancel/rebuild/epoch_reset/timeout=没到目标就结束（这些段的「收尾误差」
    不是过冲，只报计数）。过冲窗口从并线开始到结束后 1.0 s：车到位后的
    惯性前送也算——那正是「越过目标」的定义。段内与结束后的过冲分开报：
    结束后窗口会抓到执行横位的观测/模型源切换，不能全算控制过冲。"""
    segs, cur = [], None
    for i, r in enumerate(rows):
        if (r.get("route") or {}).get("ph") == "merging":
            if cur is None:
                cur = {"i0": i, "i1": i}
            cur["i1"] = i
        elif cur:
            segs.append(cur)
            cur = None
    if cur:
        segs.append(cur)
    whys, ends, speeds, settle, durations = {}, {}, [], [], []
    overs, overs_in, overs_post = [], [], []
    # 段内真的走到目标的那几段（不含「只在结束后才到」的 0），中位数只在这
    # 些段上算；段数一并报出——1~2 段时分位就是最大值。
    reach_in = []
    for s in segs:
        rs = rows[s["i0"]:s["i1"] + 1]
        why = (rs[0].get("route") or {}).get("why")
        whys[why] = whys.get(why, 0) + 1
        nxt = (rows[s["i1"] + 1].get("route")
               if s["i1"] + 1 < len(rows) else None)
        end = (nxt or {}).get("vd") or "unknown"
        ends[end] = ends.get(end, 0) + 1
        ex = [r.get("executed_lane") for r in rs
              if r.get("executed_lane") is not None]
        ref = next((r["route"].get("ref") for r in rs
                    if (r.get("route") or {}).get("ref") is not None), None)
        d = _dur(rs)
        durations.append(d)
        if len(ex) < 2 or ref is None or d <= 0:
            continue
        speeds.append(abs(ex[-1] - ex[0]) / d)
        if end == "finish":
            settle.append(abs(ex[-1] - ref))
        t_end = _ts(rs[-1])
        post = []
        for r in rows[s["i1"] + 1:]:
            if _ts(r) - t_end > 1.0:
                break
            rt = r.get("route") or {}
            # 连跳并线不许把下一段的位移记到这一段头上
            if rt.get("ph") == "merging" or rt.get("vd") == "merge_start":
                break
            if r.get("executed_lane") is not None:
                post.append(r["executed_lane"])
        direction = 1.0 if ref > ex[0] else -1.0
        if abs(ref - ex[0]) < 0.05:
            continue
        bin_ = [(x - ref) * direction for x in ex]
        bpost = [(x - ref) * direction for x in post]
        # 与旧口径一致：只有真的走到/越过目标的段才计入过冲（没走到记
        # settle_err，不把 0 混进分位）。
        if not any(v >= -0.02 for v in bin_ + bpost):
            continue
        oi = max(0.0, max(bin_)) if bin_ else 0.0
        op = max(0.0, max(bpost)) if bpost else 0.0
        if any(v >= -0.02 for v in bin_):
            reach_in.append(oi)
        overs_in.append(oi)
        overs_post.append(op)
        overs.append(max(oi, op))
    return {
        "count": len(segs),
        "why": dict(sorted(whys.items(), key=lambda kv: -kv[1])),
        "opportunity": whys.get("opportunity", 0),
        "urgent": whys.get("urgent", 0),
        "end": dict(sorted(ends.items(), key=lambda kv: -kv[1])),
        # 过冲 p90 在 1~3 段时就是最大值，且只统计「真走到目标」的段；跨局
        # 判读要同时看中位与参与段数，否则一场里的一次擦线就能定胜负。
        "finish_share": _r(ends.get("finish", 0) / len(segs), 3)
        if segs else None,
        "overshoot_in_median": _r(_pct(reach_in, 50)),
        "overshoot_in_n": len(reach_in),
        "dur_s_median": _r(_pct(durations, 50), 2),
        "v_lat_lane_s_median": _r(_pct(speeds, 50)),
        "overshoot_lane_p90": _r(_pct(overs, 90)),
        "overshoot_lane_max": _r(max(overs)) if overs else None,
        "overshoot_in_p90": _r(_pct(overs_in, 90)),
        "overshoot_in_max": _r(max(overs_in)) if overs_in else None,
        "overshoot_post_p90": _r(_pct(overs_post, 90)),
        "overshoot_post_max": _r(max(overs_post)) if overs_post else None,
        "post_drift_over_0_5": sum(1 for x in overs_post if x > 0.5),
        "settle_err_lane_p90_finished": _r(_pct(settle, 90)),
    }


def _requests(rows):
    """请求段与转化：转化只认请求窗口内 `why=opportunity` 的并线。

    urgent/overtake 并线本来就会发生，把任何 1 秒内的并线都记成请求转化会让
    指标虚高（2026-11-01 两局：17/18 次并线是 urgent）。一次并线只归给**最近
    的一段请求**（同一并线前 1 秒内常有 3~5 段闪检，按段计会把一次转化算成
    多次）；`conversion_any` 保留旧口径作参照，不进闸门。请求寿命按首末拍
    覆盖时长计（末拍加一个 dt）。
    """
    segs = _segments(rows, lambda r: (r.get("sel_dbg") or {}).get("req")
                     is not None)
    starts = [(_ts(r), (r.get("route") or {}).get("why")) for r in rows
              if (r.get("route") or {}).get("vd") == "merge_start"]
    spans = [(_ts(s[0]), _ts(s[-1])) for s in segs]
    conv_any, conv_opp = set(), set()
    for t, why in starts:
        hit = [i for i, (t0, t1) in enumerate(spans)
               if t0 <= t <= t1 + 1.0]
        if not hit:
            continue
        i = max(hit, key=lambda j: spans[j][0])
        conv_any.add(i)
        if why == "opportunity":
            conv_opp.add(i)
    life = [t1 - t0 + _dt(s[-1]) for s, (t0, t1) in zip(segs, spans)]
    n = len(segs)
    return {"segments": n, "ticks": sum(len(s) for s in segs),
            "len_ticks": [len(s) for s in segs],
            "life_s": [round(x, 2) for x in life],
            "life_s_median": _r(_pct(life, 50), 2),
            "life_s_p90": _r(_pct(life, 90), 2),
            "merge_within_1s": len(conv_any),
            "conversion_any": _r(len(conv_any) / n, 3) if n else None,
            "opportunity_within_1s": len(conv_opp),
            "conversion": _r(len(conv_opp) / n, 3) if n else None}


def _route(rows):
    """承诺道与实测道的一致性 + 6h 承诺道跟随机制的观测面。

    keep 态按 `coarse.ego`（实测）与 `route.ref`（承诺）比较——两者同一粗层
    帧；>0.5 道算错位（半道内是边界抖动）。承诺道错一条道时选择层会把邻道
    机会判成本道目标，这是机会请求发不出来的直接原因。
    `anchor_follow/lane_drift/already_there` 是 6h 开关生效的证据。"""
    keep = mismatch = drift_ticks = already = anchors = 0
    for r in rows:
        rt = r.get("route") or {}
        co = r.get("coarse") or {}
        ego, ref = co.get("ego"), rt.get("ref")
        if rt.get("ph") == "keep" and ego is not None and ref is not None:
            keep += 1
            if abs(ego - ref) > 0.5:
                mismatch += 1
        if rt.get("vd") == "anchor_follow":
            anchors += 1
        if rt.get("why") == "lane_drift":
            drift_ticks += 1
        if rt.get("why") == "already_there":
            already += 1
    return {"keep_ticks": keep, "mismatch_ticks": mismatch,
            "mismatch_frac": _r(mismatch / keep) if keep else None,
            "anchor_events": anchors, "lane_drift_ticks": drift_ticks,
            "already_there": already}


def _source_flips(rows):
    """奖励源 0/1 切换率：按原始可见秒归一。

    `cr_bonus_src` 是选择层当拍实际拿到的奖励源数，`raw_bonuses` 是原始检测
    数。断供拍不算可见时间——否则丢检越多的局分母越大，切换率反而显得更低。
    """
    flips, visible_s, prev = 0, 0.0, None
    for r in rows:
        cur = 1 if int(r.get("cr_bonus_src") or 0) > 0 else 0
        if prev is not None and cur != prev:
            flips += 1
        prev = cur
        if int(r.get("raw_bonuses") or 0) > 0:
            visible_s += _dt(r)
    per_s = flips / visible_s if visible_s > 0.0 else None
    return {"flips": flips, "visible_s": round(visible_s, 1),
            "per_s": _r(per_s)}


def _coin_life(rows):
    """币组连续寿命（按组号，时间空档切段）。"""
    runs, open_ = [], {}
    for r in rows:
        t = _ts(r)
        seen = {g.get("id") for g in (r.get("cgroups") or [])}
        for gid in list(open_):
            if gid not in seen or t - open_[gid][1] > GAP_S:
                t0, t1 = open_.pop(gid)
                if t1 > t0:
                    runs.append(t1 - t0)
        for gid in seen:
            if gid not in open_:
                open_[gid] = (t, t)
            else:
                open_[gid] = (open_[gid][0], t)
    for t0, t1 in open_.values():
        if t1 > t0:
            runs.append(t1 - t0)
    return {"groups": len(runs), "life_s_median": _r(_pct(runs, 50), 2),
            "life_s_p90": _r(_pct(runs, 90), 2),
            "life_s_max": _r(max(runs), 2) if runs else None}


def _opps(rows):
    """机会光锥：逐拍 slack 分布 + （新列）首次可见的到站时间与余量。"""
    first = {}
    has_opp = False
    coin_slacks, rw_slacks = [], []
    for r in rows:
        dbg = r.get("sel_dbg") or {}
        ego = dbg.get("ego")
        if ego is None:
            continue
        opp = dbg.get("opp")
        if opp is not None:
            has_opp = True
        for kind, key in (("coin", "coin"), ("reward", "rw")):
            entries = dbg.get(key) or []
            aux = (opp or {}).get(key) or []
            for i, e in enumerate(entries):
                if not e or len(e) < 2:
                    continue
                x, t = e[0], e[1]
                s = (aux[i][1] if i < len(aux) else None) if opp else None
                if s is None:
                    s = _slack(x - ego, t)
                if s is not None:
                    (coin_slacks if kind == "coin" else rw_slacks).append(s)
                if has_opp and kind == "coin" and i < len(aux):
                    oid = aux[i][0]
                    if oid is not None and oid >= 0 and oid not in first:
                        first[oid] = (t, s)
    lead = [t for t, _ in first.values() if t is not None]
    sl = [s for _, s in first.values() if s is not None]
    return {
        "slack_source": "recorded" if has_opp else "recomputed",
        "coin_slack_median": _r(_pct(coin_slacks, 50)),
        "coin_slack_negative_share": _r(
            sum(1 for s in coin_slacks if s < 0) / len(coin_slacks), 3)
        if coin_slacks else None,
        "reward_slack_median": _r(_pct(rw_slacks, 50)),
        "first_sight_groups": len(first),
        "first_sight_lead_s_median": _r(_pct(lead, 50), 2),
        "first_sight_slack_median": _r(_pct(sl, 50)),
        "first_sight_reachable_share": _r(
            sum(1 for s in sl if s >= 0) / len(sl), 3) if sl else None,
    }


def _car_tracks(rows):
    """车轨遮挡保留（旧局也能算）：`car_h` 带 id 与 seen=距最后真实读数的 fid 差。
    逐轨估 fid 率，把 seen 换算成龄；统计遮挡段最大龄、超 0.6s 的段与总时长。"""
    tracks = {}
    for row in rows:
        t = _ts(row)
        for e in row.get("car_h") or []:
            if not isinstance(e, dict) or e.get("id") is None                     or e.get("seen") is None:
                continue
            tracks.setdefault(e["id"], []).append((t, int(e["seen"])))
    life, occl, blind_s, over = [], [], 0.0, 0
    for pts in tracks.values():
        if len(pts) < 2:
            continue
        life.append(pts[-1][0] - pts[0][0])
        rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(pts, pts[1:])
                 if b[0] > a[0] and b[1] > a[1]]
        if not rates:
            continue
        fps = sorted(rates)[len(rates) // 2]
        cur, cur_max = False, 0.0
        for t, seen in pts:
            age = seen / fps if seen > 0 else 0.0
            if seen > 0:
                if not cur:
                    cur = True
                cur_max = max(cur_max, age)
                if age > 0.6:
                    blind_s += 0.05
            elif cur:
                occl.append(cur_max)
                over += int(cur_max > 0.6)
                cur, cur_max = False, 0.0
        if cur:
            occl.append(cur_max)
            over += int(cur_max > 0.6)
    return {"tracks": len(life),
            "life_s_median": _r(_pct(life, 50), 2),
            "life_s_p90": _r(_pct(life, 90), 2),
            "occlusion_runs": len(occl),
            "occl_max_age_s_median": _r(_pct(occl, 50), 2),
            "occl_max_age_s_p90": _r(_pct(occl, 90), 2),
            "runs_over_0_6s": over,
            "blind_s": _r(blind_s, 1)}


def _group_blind(rows):
    """币组断检保留：组在 cgroups 里但本拍 obs=0（宽限存续）的连续时长。"""
    tracks = {}
    for row in rows:
        t = _ts(row)
        fid = row.get("fid")
        for g in row.get("cgroups") or []:
            if not isinstance(g, dict) or g.get("id") is None:
                continue
            # obs 在宽限存续分支仍是成员数（不是本拍观测数），用 last<fid 判盲
            blind = g.get("last") is not None and fid is not None                 and int(g["last"]) < int(fid)
            tracks.setdefault(g["id"], []).append((t, int(blind)))
    life, blind, over = [], [], 0
    for pts in tracks.values():
        if len(pts) < 2:
            continue
        life.append(pts[-1][0] - pts[0][0])
        cur = None
        for t, obs in pts:
            if obs == 0:
                cur = [t, t] if cur is None else [cur[0], t]
            elif cur is not None:
                d = cur[1] - cur[0]
                blind.append(d)
                over += int(d > 0.6)
                cur = None
        if cur is not None:
            d = cur[1] - cur[0]
            blind.append(d)
            over += int(d > 0.6)
    return {"groups": len(life),
            "life_s_median": _r(_pct(life, 50), 2),
            "blind_runs": len(blind),
            "blind_s_median": _r(_pct(blind, 50), 2),
            "blind_s_p90": _r(_pct(blind, 90), 2),
            "blind_over_0_6s": over}


def _objects(rows):
    """schema 4：对象遮挡后的保留时长与同 id 重现。"""
    if not any((r.get("scene") or {}).get("schema", 0) >= 4 for r in rows):
        return {"available": False}
    blind, open_, first_seen, reappear = [], {}, {}, 0
    for r in rows:
        t = _ts(r)
        cur = {}
        for o in _obj_rows(r):
            if len(o) < 16 or o[14] is None:
                continue
            cur[(o[0], o[14])] = o[15]
            first_seen.setdefault((o[0], o[14]), t)
        for oid in list(open_):
            if oid not in cur or t - open_[oid][1] > GAP_S:
                t0, t1 = open_.pop(oid)
                if t1 > t0:
                    blind.append(t1 - t0)
        for oid, seen in cur.items():
            if seen is False:
                if oid in open_:
                    open_[oid] = (open_[oid][0], t)
                else:
                    open_[oid] = (t, t)
            elif oid in open_:
                t0, t1 = open_.pop(oid)
                if t1 > t0:
                    blind.append(t1 - t0)
                reappear += 1
    return {"available": True, "objects": len(first_seen),
            "blind_runs": len(blind),
            "blind_s_median": _r(_pct(blind, 50), 2),
            "blind_s_p90": _r(_pct(blind, 90), 2),
            "reappear_after_blind": reappear}


def _obj_rows(row):
    return ((row.get("scene") or {}).get("objs") or ())


def _steer(rows):
    """控制面：巡航跟踪误差 + 转向平滑度。

    旧的巡航杆量均值同时受赛道弯曲和「车离目标多远」影响（审计：同一低弯
    桶内跨局 0.06~0.21），不能当跨局比较的主指标；主指标换成**决策目标
    跟踪误差**（同一目标口径，弯曲不影响「跟没跟上」）。杆量与反向率保留
    作观测：反向率与并线总数相关 0.77，远离并线的大杆值换向在基线组与
    新局无显著差，但并线密集局样本只剩几秒，不进门禁。"""
    in_band, flat, curve = [], [], []
    track = []
    rev, last = 0, None
    _, near = _merge_near(rows)
    rev_far = rev_far_large = 0
    act_far = act_far_large = 0.0
    last_far = last_far_large = None
    dur = sum(_dt(r) for r in rows)
    for r in rows:
        s_raw = r.get("steer_norm") or 0.0
        s = abs(s_raw)
        t = _ts(r)
        merging = (r.get("route") or {}).get("ph") == "merging"
        if str(r.get("reason") or "").startswith("sel:center") \
                and r.get("maneuver") is False:
            in_band.append(s)
            c = _curve_demand(r)
            if c is not None and c < 0.05:
                flat.append(s)
            elif c is not None and c >= 0.10:
                curve.append(s)
        # 目标跟踪误差：决策层给的 x_target 与执行层实测位同单位（道）
        x, ex = r.get("x_target"), r.get("executed_lane")
        if x is not None and ex is not None and not merging:
            track.append(abs(ex - x))
        if abs(s_raw) >= REV_DEADBAND:
            sign = 1 if s_raw > 0 else -1
            if last is not None and sign != last:
                rev += 1
            last = sign
        if merging or near(t):
            continue
        sign = 1 if s_raw > 0 else -1
        if abs(s_raw) >= REV_DEADBAND:
            act_far += _dt(r)
            if last_far is not None and sign != last_far:
                rev_far += 1
            last_far = sign
        if abs(s_raw) >= REV_LARGE:
            act_far_large += _dt(r)
            if last_far_large is not None and sign != last_far_large:
                rev_far_large += 1
            last_far_large = sign
    all_steer = [abs(r.get("steer_norm") or 0.0) for r in rows]
    return {"in_band_abs_mean": _r(sum(in_band) / len(in_band), 4)
            if in_band else None,
            "in_band_flat": _r(sum(flat) / len(flat), 4) if flat else None,
            "in_band_curve": _r(sum(curve) / len(curve), 4) if curve else None,
            "track_err_p50": _r(_pct(track, 50), 3),
            "track_err_p90": _r(_pct(track, 90), 3),
            "track_err_over_0_5": _r(sum(1 for e in track if e > 0.5)
                                     / len(track), 3) if track else None,
            "reverse_per_s": _r(rev / dur, 3) if dur > 0 else None,
            "reverse_far_per_s": _r(rev_far / act_far, 3)
            if act_far > 0 else None,
            "reverse_far_large_per_s": _r(rev_far_large / act_far_large, 3)
            if act_far_large > 0 else None,
            "saturate_share": _r(sum(1 for s in all_steer if s >= 0.9)
                                 / len(all_steer), 3) if all_steer else None}


def _map(rows):
    ages, stale, srcs, near_meas, confs = [], 0, {}, 0, []
    cor_switches, prev_cor = 0, None
    for r in rows:
        cor = r.get("cor_src")
        if cor is not None and prev_cor is not None and cor != prev_cor:
            cor_switches += 1
        if cor is not None:
            prev_cor = cor
        sc = r.get("scene") or {}
        age = sc.get("corridor_age_ms")
        if age is not None:
            ages.append(age)
        if sc.get("stale"):
            stale += 1
        bands = ((sc.get("road") or {}).get("bands") or [])
        hit = False
        for b in bands:
            if len(b) < 7:
                continue
            srcs[b[4]] = srcs.get(b[4], 0) + 1
            if b[4] == "measured":
                confs.append(b[6])
                if b[0] <= 9.0:
                    hit = True
        if hit:
            near_meas += 1
    n = max(1, len(rows))
    return {"corridor_age_ms_p50": _r(_pct(ages, 50), 1),
            "corridor_age_ms_p90": _r(_pct(ages, 90), 1),
            "stale_share": _r(stale / n, 3),
            "band_src": dict(sorted(srcs.items(), key=lambda kv: -kv[1])),
            "corridor_src_switches": cor_switches,
            "near_measured_share": _r(near_meas / n, 3),
            "measured_band_conf_mean": _r(sum(confs) / len(confs), 3)
            if confs else None}


def _resolve(args):
    if args.trace:
        return Path(args.trace)
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / args.session / "trace" / "speedrush.jsonl"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261101_113145")
    ap.add_argument("--trace")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    path = _resolve(args)
    rows = list(iter_rows(path))
    if not rows:
        raise SystemExit(f"没有数据行：{path}")
    dur = sum(_dt(r) for r in rows)
    missing = []
    if "plan_h" not in rows[-1]:
        missing.append("plan_h/sel_h")
    if not any((r.get("sel_dbg") or {}).get("opp") for r in rows):
        missing.append("sel_dbg.opp (identity+cone slack)")
    if not any(_obj_rows(r) for r in rows):
        missing.append("scene.objs identity (schema 4)")
    if not any(g.get("mids") for r in rows for g in (r.get("cgroups") or [])):
        missing.append("cgroups.mids")
    rep = {
        "schema": 1,
        "session": args.session if not args.trace else path.parent.parent.name,
        "trace": str(path),
        "rows": len(rows),
        "dur_s": _r(dur, 2),
        "fid": [rows[0].get("fid"), rows[-1].get("fid")],
        "missing": missing,
        "plan": _plan(rows),
        "layers": _layers(rows),
        "route_merges": _merges(rows),
        "requests": _requests(rows),
        "coins": _coin_life(rows),
        "opportunities": _opps(rows),
        "objects": _objects(rows),
        "car_tracks": _car_tracks(rows),
        "group_blind": _group_blind(rows),
        "steer": _steer(rows),
        "map": _map(rows),
    }
    text = json.dumps(rep, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"[baseline] -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
