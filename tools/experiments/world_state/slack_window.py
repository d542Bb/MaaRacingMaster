# -*- coding: utf-8 -*-
"""机会窗口复算：资格缺口、可达段、EMA 平滑与请求累计（第 6/7 期门槛依据）。

只读一局 speedrush 控制流水，按对象身份复算：

- 奖励车轨道丢检段（`otab` 的 seen=False 连续段）；
- 每个奖励源的资格缺口与可达段（slack>=0 连续拍，按时间计）；
- 对 slack 做按对象 EMA 后的可达段；
- 两种"能累计到确认"的模拟：
  - 第 6 期：证据=原始 slack，对象丢检 <= `--pause-s` 只冻结不清零，
    slack<0 立即清零；确认门槛 `--confirm-s`。
  - 第 7 期：证据=平滑 slack（丢检同样冻结），**起意仍要求原始 slack>=0**；
    累计到门槛但没有原始可达拍的，记为"够格但起不了意"。
- 奖励源 0/1 切换次数与按原始可见秒归一后的速率。

口径：**原始 slack 决定能否起意；平滑只影响证据累计（第 7 期）**。模拟只
衡量机制（身份/缺口/累计），不含选道目标切换与车辆占用，因此是上限估计，
不是并线次数。

用法::

    python tools/experiments/world_state/slack_window.py \
        --session 20261101_145701 --session 20261101_151804 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402


def _path(session: str) -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def _pct(values) -> dict | None:
    if not values:
        return None
    s = sorted(values)
    q = lambda p: s[int((len(s) - 1) * p)]  # noqa: E731
    return {"n": len(s), "p50": round(q(.5), 3), "p90": round(q(.9), 3),
            "p99": round(q(.99), 3), "max": round(s[-1], 3)}


def _runs(ts, flags):
    """逐对象 True 连续段长度（秒），已按对象分开。"""
    out, start, last = [], None, None
    for t, ok in zip(ts, flags):
        if ok:
            if start is None:
                start = t
            last = t
        elif start is not None:
            out.append(last - start)
            start = None
    if start is not None:
        out.append(last - start)
    return out


def _gaps(ts, flags):
    """逐对象两段 True 之间的间隔（秒）。"""
    out, last = [], None
    for t, ok in zip(ts, flags):
        if ok:
            if last is not None and t - last > 1e-3:
                out.append(t - last)
            last = t
    return out


def _unseen_runs(track_seen):
    out, start = [], None
    for t, ok in track_seen:
        if not ok and start is None:
            start = t
        elif ok and start is not None:
            out.append(t - start)
            start = None
    return out


def _simulate(ts, dts, series, pause_s, confirm_s, intent_age_s, use_ema):
    """按对象模拟确认累计；返回 (确认拍数, 够格但原始不可达的清零次数)。

    series: {oid: [(t, raw, ema) 或 None per tick]}；None=该拍无读数。
    use_ema=False（第 6 期）：证据=raw；不可达（raw<0）立即清零。
    use_ema=True（第 7 期）：证据=ema，未达原始可达仅冻结；起意要求 raw>=0。
    """
    state = {oid: {"credit": 0.0, "age": 0.0, "absent": 0.0,
                   "ready": False} for oid in series}
    confirms, stuck = 0, 0
    for i, t in enumerate(ts):
        dt = dts[i]
        for oid, seq in series.items():
            st = state[oid]
            item = seq[i]
            if item is None:
                st["absent"] += dt
                if st["absent"] > pause_s:
                    st["credit"] = 0.0
                    st["age"] = 0.0
                    st["ready"] = False
                continue
            _, raw, ema = item
            st["absent"] = 0.0
            st["age"] += dt
            evidence = (ema >= 0.0) if use_ema else (raw >= 0.0)
            start_ok = raw >= 0.0
            if evidence:
                st["credit"] += dt
            else:
                # 证据消失=当拍不可达：清零；冻结只用于"没有读数"（丢检）
                st["credit"] = 0.0
                st["ready"] = False
            if st["credit"] >= confirm_s:
                st["ready"] = True
                if start_ok:
                    confirms += 1
                    st["credit"] = 0.0
                    st["age"] = 0.0
                    st["ready"] = False
                else:
                    st["credit"] = confirm_s   # 封顶等待可达拍
            if st["age"] > intent_age_s:
                if st["ready"]:
                    stuck += 1
                st["credit"] = 0.0
                st["age"] = 0.0
                st["ready"] = False
    return confirms, stuck


def analyze(session: str, tau: float, pause_s: float, confirm_s: float,
            intent_age_s: float) -> dict:
    rows = list(iter_rows(_path(session)))
    if not rows:
        raise SystemExit(f"没有数据行：{session}")
    ts = [r["ts_ns"] / 1e9 for r in rows]
    dts = [0.0] + [max(0.0, min(0.2, b - a)) for a, b in zip(ts, ts[1:])]

    # 1) 奖励车轨道丢检段
    tracks = defaultdict(list)
    for r, t in zip(rows, ts):
        for e in ((r.get("otab") or {}).get("t") or []):
            if len(e) >= 8 and e[0] == "bonus":
                tracks[e[1]].append((t, bool(e[6])))
    unseen = []
    for seq in tracks.values():
        unseen.extend(_unseen_runs(seq))

    # 2) 逐对象 slack 序列（缺拍=None）＋按对象 EMA
    ids = set()
    for r in rows:
        for x in (((r.get("sel_dbg") or {}).get("opp") or {}).get("rw") or []):
            if isinstance(x, (list, tuple)) and len(x) > 1 \
                    and isinstance(x[1], (int, float)):
                ids.add(x[0])
    series = {oid: [None] * len(rows) for oid in ids}
    ema = {}
    for i, r in enumerate(rows):
        for x in (((r.get("sel_dbg") or {}).get("opp") or {}).get("rw") or []):
            if not (isinstance(x, (list, tuple)) and len(x) > 1
                    and isinstance(x[1], (int, float))):
                continue
            oid, s = x[0], float(x[1])
            prev = ema.get(oid)
            a = 1.0 - math.exp(-dts[i] / max(tau, 1e-3)) \
                if prev is not None else 1.0
            sm = s if prev is None else prev + a * (s - prev)
            ema[oid] = sm
            series[oid][i] = (ts[i], s, sm)

    runs_raw, gaps_raw, runs_ema, gaps_ema = [], [], [], []
    for seq in series.values():
        fr = [it is not None and it[1] >= 0.0 for it in seq]
        fe = [it is not None and it[2] >= 0.0 for it in seq]
        runs_raw.extend(_runs(ts, fr))
        gaps_raw.extend(_gaps(ts, fr))
        runs_ema.extend(_runs(ts, fe))
        gaps_ema.extend(_gaps(ts, fe))

    # 3) 两种机制的确认模拟
    conf6, _ = _simulate(ts, dts, series, pause_s, confirm_s, intent_age_s,
                         use_ema=False)
    conf7, stuck7 = _simulate(ts, dts, series, pause_s, confirm_s,
                              intent_age_s, use_ema=True)

    # 4) 实际请求信号的分段（选择层逐拍输出；缺口同向 <= pause_s 视为连续）
    spans, cur, last_t = [], None, None
    for r, t in zip(rows, ts):
        dt = 0.0 if last_t is None else t - last_t
        last_t = t
        q = (r.get("sel_dbg") or {}).get("req")
        if q is None:
            if cur is not None:
                cur["gap"] += dt
                if cur["gap"] > pause_s:
                    spans.append(cur)
                    cur = None
            continue
        if cur is not None and q == cur["sign"] and cur["gap"] <= pause_s:
            cur["t1"] = t
            cur["gap"] = 0.0
        else:
            if cur is not None:
                spans.append(cur)
            cur = {"sign": q, "t0": t, "t1": t, "gap": 0.0}
    if cur is not None:
        spans.append(cur)
    span_len = [round(b["t1"] - b["t0"], 2) for b in spans]

    # 5) 奖励源切换率（按原始可见秒归一）
    flips, visible_s, prev = 0, 0.0, None
    for r, dt in zip(rows, dts):
        cur_src = 1 if int(r.get("cr_bonus_src") or 0) > 0 else 0
        if prev is not None and cur_src != prev:
            flips += 1
        prev = cur_src
        if int(r.get("raw_bonuses") or 0) > 0:
            visible_s += dt

    return {
        "session": session,
        "ticks": len(rows),
        "dur_s": round(ts[-1] - ts[0], 1),
        "bonus_unseen_gaps": _pct(unseen),
        "reward_gaps_raw": _pct(gaps_raw),
        "reward_runs_raw": _pct(runs_raw),
        "reward_gaps_ema": _pct(gaps_ema),
        "reward_runs_ema": _pct(runs_ema),
        "confirm6": conf6,
        "confirm7": conf7,
        "confirm7_stuck_no_raw": stuck7,
        "request_spans_s": span_len,
        "request_spans_ge_confirm": sum(
            1 for x in span_len if x >= confirm_s),
        "src_flips": flips,
        "raw_visible_s": round(visible_s, 1),
        "src_flips_per_visible_min": round(
            flips / max(visible_s, 1e-9) * 60.0, 1),
        "params": {"tau": tau, "pause_s": pause_s, "confirm_s": confirm_s,
                   "intent_age_s": intent_age_s},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", action="append", default=[],
                    help="会话号（可多次）；缺省=113145/145701/151804")
    ap.add_argument("--tau", type=float, default=0.2, help="slack EMA 时间常数（秒）")
    ap.add_argument("--pause-s", type=float, default=0.2, help="丢检冻结上限（秒）")
    ap.add_argument("--confirm-s", type=float, default=0.3, help="确认门槛（秒）")
    ap.add_argument("--intent-age-s", type=float, default=1.0,
                    help="意图年龄上限（秒）")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    sessions = args.session or ["20261101_113145", "20261101_145701",
                                "20261101_151804"]
    rep = {"sessions": [
        analyze(s, args.tau, args.pause_s, args.confirm_s, args.intent_age_s)
        for s in sessions]}
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    for x in rep["sessions"]:
        print(f"== {x['session']}  {x['ticks']} 拍 / {x['dur_s']} 秒")
        for k in ("bonus_unseen_gaps", "reward_gaps_raw", "reward_runs_raw",
                  "reward_gaps_ema", "reward_runs_ema"):
            print(f"  {k:20s} {x[k]}")
        print(f"  confirm6={x['confirm6']} confirm7={x['confirm7']} "
              f"stuck7={x['confirm7_stuck_no_raw']}")
        print(f"  request_spans_s={x['request_spans_s']} "
              f">=confirm:{x['request_spans_ge_confirm']}")
        print(f"  src_flips={x['src_flips']} raw_visible={x['raw_visible_s']}s "
              f"per_min={x['src_flips_per_visible_min']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
