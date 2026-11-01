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
import json
import math
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True      # 不在实验区留 __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402

GAP_S = 0.5                 # 段切分空档（秒）
# 以下常量与 2026-11-01 的策略一致（planner / selection 的真源在
# resources/policy/decision.json）；旧局没有 slack 列时用它们复算。
LANE_W = 3.5
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
    惯性前送也算——那正是「越过目标」的定义。"""
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
    whys, ends, speeds, overs, settle, durations = {}, {}, [], [], [], []
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
        beyond = [(x - ref) * direction for x in ex + post]
        if any(b >= -0.02 for b in beyond):
            overs.append(max(0.0, max(beyond)))
    return {
        "count": len(segs),
        "why": dict(sorted(whys.items(), key=lambda kv: -kv[1])),
        "opportunity": whys.get("opportunity", 0),
        "end": dict(sorted(ends.items(), key=lambda kv: -kv[1])),
        "dur_s_median": _r(_pct(durations, 50), 2),
        "v_lat_lane_s_median": _r(_pct(speeds, 50)),
        "overshoot_lane_p90": _r(_pct(overs, 90)),
        "overshoot_lane_max": _r(max(overs)) if overs else None,
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
    in_band = [abs(r.get("steer_norm") or 0.0) for r in rows
               if str(r.get("reason") or "").startswith("sel:center")
               and r.get("maneuver") is False]
    rev, last = 0, None
    for r in rows:
        s = r.get("steer_norm") or 0.0
        if abs(s) < REV_DEADBAND:
            continue
        sign = 1 if s > 0 else -1
        if last is not None and sign != last:
            rev += 1
        last = sign
    dur = sum(_dt(r) for r in rows)
    all_steer = [abs(r.get("steer_norm") or 0.0) for r in rows]
    return {"in_band_abs_mean": _r(sum(in_band) / len(in_band), 4)
            if in_band else None,
            "reverse_per_s": _r(rev / dur, 3) if dur > 0 else None,
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
