# -*- coding: utf-8 -*-
"""片段定位与相似片段检索（工作台，不入库）。

给定「哪一局、第几秒」，把那一小段的车速、目标、位置估计、计划、杆值和
到路缘距离摊成一张表，并给出可复现的判据标签；再在多个会话里找行为
最像的若干段。用于「我记得这里车很傻，帮我找出错在哪、还有哪些类似段」。

时间口径：
- ``--at 19.2`` = 本局开始后 19.2 秒；``--at 4:38`` 同义（分:秒=已过）。
- ``--hud 4:38`` = 游戏计时剩 4:38（HUD 从 5:00 倒数），换算成已过 22 秒。
- 游戏「局」按 trace 的 stage 顺序编号（第一局=1），可用 ``--game`` 指定。

图片：按 trace 的 stage_begin 墙钟时间匹配 debug 目录，再用逐拍
``dbg_saved`` 计数找最近的 ``raw/000k_raw.jpg``。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_segment.py \
        --session 20261026_152046_1 --game 2 --at 19.2
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_segment.py \
        --session 20261026_152046_1 --game 2 --at 19.2 \
        --similar --sessions 20261026_130014_1,20261026_143454_1,20261026_155119_1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

import probe_lateral_state as P

GAME_S = 300.0            # HUD 倒计时基准（5:00）
WIN_S = 2.0               # 相似检索窗口
STRIDE_S = 0.5
DEBUG_ROOT = os.environ.get(
    "SPEEDRUSH_DEBUG",
    r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/speedrush")


def debug_dir_for(stage_row):
    """按 stage_begin 墙钟时间在 debug/speedrush 下找同局目录。"""
    if not stage_row or stage_row.get("ts_wall_ns") is None:
        return None
    t0 = dt.datetime.fromtimestamp(stage_row["ts_wall_ns"] / 1e9)
    best, best_d = None, 1e9
    try:
        names = os.listdir(DEBUG_ROOT)
    except OSError:
        return None
    for name in names:
        meta = os.path.join(DEBUG_ROOT, name, "hud", "hud_meta.json")
        if not os.path.isfile(meta):
            continue
        try:
            started = json.load(open(meta, encoding="utf-8"))["started_at"]
            t1 = dt.datetime.fromisoformat(started)
        except Exception:
            continue
        d = abs((t1 - t0).total_seconds())
        if d < best_d:
            best, best_d = name, d
    return None if best is None or best_d > 90 else os.path.join(DEBUG_ROOT, best)


def load_games(logs, token):
    """返回 [(stage_row, rows)]，保留 stage_begin 供匹配 debug 目录。"""
    path = os.path.join(logs, token, "trace", "speedrush.jsonl")
    out, stage, buf = [], None, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            kind = d.get("type")
            if kind == "stage_begin":
                stage, buf = d, []
            elif kind == "stage_end":
                if buf is not None:
                    out.append((stage, buf))
                stage, buf = None, None
            elif kind is None and buf is not None:
                buf.append(d)
    return out


def feat(row, t):
    lo = row.get("lane_obs") or {}
    lf = row.get("lane_fuse") or {}
    x = lo.get("x")
    line = None if x is None else float(x) + float(lf.get("bias") or 0.0)
    sc = row.get("scene") or {}
    road = sc.get("road") or {}
    ego, l, r = sc.get("ego_m"), road.get("l"), road.get("r")
    gap = None if (ego is None or l is None or r is None) \
        else min(abs(ego - l), abs(r - ego))
    pt = row.get("plan") or {}
    prev = None
    if pt.get("valid"):
        prev = (float(pt["x"]) - float(pt["fd"])) / 3.38
    ro, ex = row.get("road_offset"), row.get("executed_lane")
    xt, u = row.get("x_target"), row.get("steer_norm")
    vc = (row.get("lat_ctrl") or {}).get("v_ctrl")
    hold = bool(row.get("ro_hold"))
    fresh = bool(row.get("dgeo_new")) and not hold
    role = row.get("plan_role")
    phase = (row.get("route") or {}).get("ph")
    flags = {
        "wall": line is not None and abs(line) > 1.4,
        "gated": line is not None and abs(line) > 2.6,
        "lag": ro is not None and ex is not None and abs(ro - ex) > 0.8,
        "plan_hold": (prev is not None and ex is not None and xt is not None
                      and abs(prev - ex) < 0.2 and abs(xt - ex) > 0.8),
        "weak": u is not None and abs(u) < 0.15,
        "outward": (u is not None and ex is not None
                    and u * ex > 0.08),
    }
    return {"t": t, "fid": row.get("fid"), "line": line, "gap": gap,
            "exec": ex, "target": xt, "steer": u, "v": vc, "prev": prev,
            "ro": ro, "src": row.get("ro_source"), "hold": hold,
            "fresh": fresh, "lane": lo.get("state"), "age": lo.get("det_age_ms"),
            "role": role, "phase": phase, "flags": flags,
            "dbg": row.get("dbg_saved") or 0}


def game_ticks(games, idx):
    stage, rows = games[idx]
    t0 = rows[0]["ts_ns"]
    return stage, rows, [feat(r, (r["ts_ns"] - t0) / 1e9) for r in rows]


def parse_at(val):
    if ":" in val:
        m, s = val.split(":")
        return int(m) * 60 + float(s)
    return float(val)


def img_at(debug_dir, ticks, t):
    if not debug_dir:
        return None
    best = min(ticks, key=lambda x: abs(x["t"] - t))
    k = best.get("dbg") or 0
    if k <= 0:
        return None
    path = os.path.join(debug_dir, "raw", f"{k:04d}_raw.jpg")
    return path if os.path.isfile(path) else None


def share(vals, pred):
    vals = [v for v in vals if v is not None]
    return sum(1 for v in vals if pred(v)) / max(1, len(vals))


def window_labels(win):
    n = max(1, len(win))
    line = [w["line"] for w in win]
    ex = [w["exec"] for w in win]
    gap = [w["gap"] for w in win]
    err = [abs(w["target"] - w["exec"]) for w in win
           if w["target"] is not None and w["exec"] is not None]
    f = lambda key: sum(1 for w in win if w["flags"][key]) / n
    labs = []
    if ex and abs(statistics.median(ex)) > 1.3 and f("plan_hold") > 0.4 \
            and err and statistics.median(err) > 1.0:
        labs.append(("计划贴边等", f"plan-hold {f('plan_hold'):.0%} "
                                  f"|exec| {abs(statistics.median(ex)):.2f} "
                                  f"目标差 {statistics.median(err):.2f}"))
    if f("lag") > 0.3:
        labs.append(("位置跟丢", f"滞后拍 {f('lag'):.0%}"))
    if f("gated") > 0.05:
        labs.append(("线越界错锁", f"越界拍 {f('gated'):.0%}"))
    fresh = sum(1 for w in win if w["fresh"]) / n
    hold = sum(1 for w in win if w["hold"]) / n
    if fresh < 0.15 and hold > 0.6:
        labs.append(("深度干旱", f"新鲜 {fresh:.0%} 保持 {hold:.0%}"))
    veto = sum(1 for w in win if w["role"] == "veto") / n
    if veto > 0.4 and ex and abs(statistics.median(ex)) > 1.3:
        labs.append(("否决后回带", f"否决 {veto:.0%}"))
    if f("outward") > 0.2 and ex and abs(statistics.median(ex)) > 1.3:
        labs.append(("朝墙打杆", f"外打 {f('outward'):.0%}"))
    merging = sum(1 for w in win if w["phase"] == "merging") / n
    if merging > 0.5 and f("plan_hold") > 0.4:
        labs.append(("换道等车", f"并线 {merging:.0%}"))
    if line and len(line) > 5 and statistics.pstdev(line) > 0.5:
        labs.append(("横摆", f"线标准差 {statistics.pstdev(line):.2f}"))
    if gap and share(gap, lambda g: g < 1.0) > 0.2:
        labs.append(("贴缘", f"<1m {share(gap, lambda g: g < 1.0):.0%}"))
    return labs


def window_vec(win):
    """行为特征（左右镜像归一：贴左和贴右视为同一类）。"""
    line = [w["line"] for w in win if w["line"] is not None]
    ex = [w["exec"] for w in win if w["exec"] is not None]
    side = 1.0
    if ex and abs(statistics.median(ex)) > 0.4:
        side = 1.0 if statistics.median(ex) > 0 else -1.0
    n = max(1, len(win))
    f = lambda key: sum(1 for w in win if w["flags"][key]) / n
    mix = lambda key, vals: sum(1 for w in win if w[key] in vals) / n
    gaps = [w["gap"] for w in win if w["gap"] is not None]
    srcs = Counter(w["src"] for w in win)
    return [
        side * statistics.mean(line) if line else 0.0,
        statistics.pstdev(line) if len(line) > 1 else 0.0,
        side * statistics.median(ex) if ex else 0.0,
        statistics.median([w["target"] for w in win
                           if w["target"] is not None] or [0.0]) * side,
        statistics.median([abs(w["target"] - w["exec"]) for w in win
                           if w["target"] is not None
                           and w["exec"] is not None] or [0.0]),
        statistics.median([side * w["steer"] for w in win
                           if w["steer"] is not None] or [0.0]),
        statistics.median([side * w["v"] for w in win
                           if w["v"] is not None] or [0.0]),
        statistics.median(gaps) if gaps else 2.0,
        min(gaps) if gaps else 2.0,
        mix("lane", ("TRACK",)), mix("lane", ("COAST",)),
        mix("lane", ("RELOCK",)),
        srcs.get("wall", 0) / n, srcs.get("region", 0) / n,
        srcs.get("memory", 0) / n, srcs.get("wall_occl", 0) / n,
        sum(1 for w in win if w["hold"]) / n,
        mix("role", ("executor",)), mix("role", ("veto",)),
        mix("phase", ("merging",)),
        f("plan_hold"), f("weak"), f("outward"), f("wall"), f("lag"),
        f("gated"), sum(1 for w in win if w["fresh"]) / n,
    ]


def windows_of(ticks):
    out = []
    if not ticks:
        return out
    t0, t1 = ticks[0]["t"], ticks[-1]["t"]
    t = t0
    while t + WIN_S <= t1 + 1e-6:
        win = [w for w in ticks if t <= w["t"] < t + WIN_S]
        if win and len(win) >= 10:
            out.append((t, win))
        t += STRIDE_S
    return out


def describe(win):
    ex = [w["exec"] for w in win if w["exec"] is not None]
    gap = [w["gap"] for w in win if w["gap"] is not None]
    line = [w["line"] for w in win if w["line"] is not None]
    return (f"exec {statistics.median(ex):+.2f} " if ex else "exec - ") + \
           (f"line {statistics.median(line):+.2f} " if line else "line - ") + \
           (f"gap {statistics.median(gap):.2f}m" if gap else "gap -")


def print_segment(ticks, at, win_s):
    t_lo, t_hi = at - 1.0, at + win_s
    seg = [w for w in ticks if t_lo <= w["t"] <= t_hi]
    print(f"| t(s) | HUD | 相位 | 目标 | exec | 线+bias | 线态/龄 | 路心/源/持 | 计划 role/u/预览 | 杆 | 离缘 | 标记 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    last_key = None
    for i, w in enumerate(seg):
        key = (w["lane"], w["src"], w["role"], w["hold"])
        mark = ",".join(k for k, v in w["flags"].items() if v)
        if i % 5 and key == last_key and not mark:
            continue
        last_key = key
        hud = max(0.0, GAME_S - w["t"])
        print("| %.2f | %d:%02d | %s | %s | %s | %s | %s/%s | %s/%s/%s | %s/%s/%s | %s | %s | %s |" % (
            w["t"], int(hud // 60), int(hud % 60), w["phase"] or "-",
            "-" if w["target"] is None else f"{w['target']:+.2f}",
            "-" if w["exec"] is None else f"{w['exec']:+.2f}",
            "-" if w["line"] is None else f"{w['line']:+.2f}",
            w["lane"] or "-", "-" if w["age"] is None else f"{w['age']:.0f}",
            "-" if w["ro"] is None else f"{w['ro']:+.2f}",
            w["src"] or "-", int(w["hold"]),
            w["role"] or "-", "-" if w["steer"] is None else f"{w['steer']:+.2f}",
            "-" if w["prev"] is None else f"{w['prev']:+.2f}",
            "-" if w["steer"] is None else f"{w['steer']:+.2f}",
            "-" if w["gap"] is None else f"{w['gap']:.2f}",
            mark or "-"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--game", type=int, default=1)
    ap.add_argument("--at", default="", help="已过秒数或 mm:ss")
    ap.add_argument("--hud", default="", help="游戏计时剩余 mm:ss")
    ap.add_argument("--window-s", type=float, default=6.0)
    ap.add_argument("--logs", default=P.DEFAULT_LOGS)
    ap.add_argument("--similar", action="store_true")
    ap.add_argument("--sessions", default="")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.hud:
        at = max(0.0, GAME_S - parse_at(args.hud))
    elif args.at:
        at = parse_at(args.at)
    else:
        raise SystemExit("需要 --at 或 --hud")
    games = load_games(args.logs, args.session)
    if not 1 <= args.game <= len(games):
        raise SystemExit(f"局号超范围：1~{len(games)}")
    stage, rows, ticks = game_ticks(games, args.game - 1)
    ddir = debug_dir_for(stage)
    print(f"# 片段：{args.session} 第 {args.game} 局，已过 {at:.2f}s"
          f"（HUD {int(max(0, GAME_S-at)//60)}:{int(max(0, GAME_S-at)%60):02d}）\n")
    if ddir:
        img = img_at(ddir, ticks, at)
        print(f"- 调试目录：`{os.path.basename(ddir)}`"
              + (f"；起始帧图：`{img}`" if img else ""))
    else:
        print("- 未找到同局调试目录（旧局可能已轮转）")
    print()
    print_segment(ticks, at, args.window_s)
    win0 = [w for w in ticks if at <= w["t"] < at + WIN_S] or \
        [w for w in ticks if at - WIN_S <= w["t"] < at]
    print("\n判据标签：")
    labs = window_labels(win0)
    if labs:
        for name, ev in labs:
            print(f"- **{name}**：{ev}")
    else:
        print("- 无命中（这段行为落在常规范围）")
    if not args.similar:
        return 0
    toks = [t.strip() for t in args.sessions.split(",") if t.strip()]
    if not toks:
        raise SystemExit("--similar 需要 --sessions")
    allw, qvec = [], None
    seen = []
    for tok in toks + [args.session]:
        if tok in seen:
            continue
        seen.append(tok)
        for gi, (st, rws) in enumerate(load_games(args.logs, tok), 1):
            _, _, tk = game_ticks([(st, rws)], 0)
            for t, win in windows_of(tk):
                vec = window_vec(win)
                allw.append((tok, gi, t, win, vec))
                if tok == args.session and gi == args.game \
                        and t <= at < t + WIN_S:
                    qvec = vec
    if qvec is None:
        raise SystemExit("查询窗口没找到（时间点可能落在局外）")
    dim = range(len(qvec))
    scale = []
    for j in dim:
        vals = [w[4][j] for w in allw]
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        scale.append(sd if sd > 1e-6 else 1.0)
    def dist(v):
        return math.sqrt(sum(((a - b) / s) ** 2
                             for a, b, s in zip(qvec, v, scale)))
    cand = [(dist(w[4]), w) for w in allw
            if not (w[0] == args.session and w[1] == args.game
                    and abs(w[2] - at) < 3.0)]
    cand.sort(key=lambda x: x[0])
    print(f"\n## 相似片段（2s 窗，左右镜像归一，前 {args.k} 段）\n")
    print("| # | 会话 | 局 | 已过 | HUD | 行为 | 标签 |")
    print("|---|---|---|---|---|---|---|")
    for rank, (d, (tok, gi, t, win, _v)) in enumerate(cand[:args.k], 1):
        hud = max(0.0, GAME_S - t)
        labs = "；".join(n for n, _ in window_labels(win)) or "-"
        print(f"| {rank} | `{tok[9:]}` | {gi} | {t:.1f}s | "
              f"{int(hud//60)}:{int(hud%60):02d} | {describe(win)} | {labs} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
