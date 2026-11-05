# -*- coding: utf-8 -*-
"""机会通道路线模拟：两位口径（证据位=平滑 / 动作位=原始）下的并线起点。

从一局 speedrush 控制流水复算（第 7 期平滑的路线级验证）：

- 逐对象（身份）原始 slack 序列来自 `sel_dbg.opp`；平滑 EMA 与实现同口径
  （τ 可参，跨丢检保持，无读数拍不更新）；
- 请求发出 = 证据位可达（平滑 slack≥0）且目标在承诺道相邻 ±1 道、
  0≤到站≤视界、保持态（记录的 route ph）；
- 证据生命周期与实现同参：同侧累计、断拍在 request_gap 内冻结、换向重置、
  意图龄 1s、确认门槛 = 决策真源 opportunity_confirm_s；
- 起意 = 证据攒满**且动作位（原始 slack≥0）**——平滑不能把不可达拍变成
  起意拍；起意即并线起点。

两种模式对照：第 6 期（证据=原始）vs 第 7 期（证据=平滑）。控制流水里没有
逐道占用（coarse 列为空），并线目标是"上限估计"：邻道可能被车占住，实车
会少于此数。画布同时画记录的实际请求与实际机会并线做参照。

用法::

    python tools/experiments/world_state/route_sim.py \
        --session 20261101_220134 --png out.png
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
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from speedrush_trace_reader import iter_rows  # noqa: E402

from maaracing_master.plugins.speedrush.config import (  # noqa: E402
    _read_decision)
from maaracing_master.plugins.speedrush import DECISION_FILE  # noqa: E402

CFG = _read_decision(DECISION_FILE)
TAU = CFG.selection.score_smooth_tau_s
CONFIRM = CFG.route.opportunity_confirm_s
GAP_S = CFG.route.request_gap_s
INTENT_AGE = CFG.route.request_intent_age_s


def _path(session: str) -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / \
        "MaaRacingMaster" / "logs"
    return base / session / "trace" / "speedrush.jsonl"


def _load(session: str):
    rows = list(iter_rows(_path(session)))
    if not rows:
        raise SystemExit(f"没有数据行：{session}")
    t0 = rows[0]["ts_ns"] / 1e9
    ticks = []
    for r in rows:
        sd = r.get("sel_dbg") or {}
        rt = r.get("route") or {}
        coins = sd.get("coin") or []
        rws = sd.get("rw") or []
        opp = sd.get("opp") or {}
        oc = opp.get("coin") or []
        orw = opp.get("rw") or []
        objs = []
        for (x, t), e in zip(coins, oc):
            if isinstance(e, (list, tuple)) and len(e) >= 2 \
                    and isinstance(e[1], (int, float)):
                objs.append(("coin", float(x), t, int(e[0]), float(e[1])))
        for (x, t), e in zip(rws, orw):
            if isinstance(e, (list, tuple)) and len(e) >= 2 \
                    and isinstance(e[1], (int, float)):
                objs.append(("rw", float(x), t, int(e[0]), float(e[1])))
        ticks.append({
            "t": r["ts_ns"] / 1e9 - t0,
            "ego": sd.get("ego"),
            "ref": rt.get("ref"),
            "ph": rt.get("ph"),
            "req": sd.get("req"),
            "merge_opp": rt.get("vd") == "merge_start"
                         and rt.get("why") == "opportunity",
            "objs": objs,
        })
    return ticks


def _ema_series(ticks, tau):
    """逐身份原始 slack 的 EMA（与实现同口径：跨丢检保持，缺拍不更新）。"""
    ema = {}
    out = []
    prev_t = None
    for tk in ticks:
        dt = max(0.0, min(0.2, tk["t"] - prev_t)) if prev_t is not None else 0.0
        prev_t = tk["t"]
        cur = {}
        for kind, x, t, oid, raw in tk["objs"]:
            prev = ema.get(oid)
            if prev is None:
                sm = raw
            else:
                a = 1.0 - math.exp(-dt / max(tau, 1e-3))
                sm = prev + a * (raw - prev)
            ema[oid] = sm
            cur[oid] = sm
        out.append(cur)
    return out


def _simulate(ticks, smas, use_ema):
    """单请求证据生命周期；返回 (并线起点列表, 请求覆盖秒数)。

    发出 = 证据位可达且相邻 ±1 道、保持态；累计与实现同参；起意 = 攒满
    且动作位（原始 slack≥0）。"""
    lane = None
    credit = age = pause = 0.0
    merges, req_s = [], 0.0
    horizon = CFG.selection.horizon_s
    for i, tk in enumerate(ticks):
        if tk["ph"] != "keep" or tk["ref"] is None:
            lane, credit, age, pause = None, 0.0, 0.0, 0.0
            continue
        pick = None
        for kind, x, t, oid, raw in tk["objs"]:
            sm = smas[i].get(oid, raw)
            evidence = (sm >= 0.0) if use_ema else (raw >= 0.0)
            if not evidence or t is None or not (0.0 <= t <= horizon):
                continue
            side = round(x - tk["ref"])
            if side not in (-1, 1):
                continue
            pick = (side, raw)
            break
        if pick is None:
            req_s += _step_none(ticks, i)
            if lane is not None and credit > 0.0:
                pause += _dt(ticks, i)
                age += _dt(ticks, i)
                if pause > GAP_S + 1e-9 or age > INTENT_AGE + 1e-9:
                    lane, credit, age, pause = None, 0.0, 0.0, 0.0
            continue
        side, raw = pick
        req_s += _dt(ticks, i)
        if lane != tk["ref"] + side:
            lane, credit, age, pause = tk["ref"] + side, 0.0, 0.0, 0.0
        credit += _dt(ticks, i)
        age += _dt(ticks, i)
        pause = 0.0
        if age > INTENT_AGE + 1e-9:
            lane, credit, age, pause = None, 0.0, 0.0, 0.0
            continue
        if credit >= CONFIRM and raw >= 0.0:
            merges.append((tk["t"], tk["ego"], side))
            lane, credit, age, pause = None, 0.0, 0.0, 0.0
    return merges, req_s


def _dt(ticks, i):
    return max(0.0, min(0.2, ticks[i]["t"] - ticks[i - 1]["t"])) \
        if i else 0.0


def _step_none(ticks, i):
    return _dt(ticks, i)


def _draw(ticks, smas, out_png, session):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
        font_s = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 12)
    except OSError:
        font = font_s = ImageFont.load_default()
    W, H = 1600, 760
    L, R, TOP, BOT = 70, 30, 30, 50
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    t_end = max(tk["t"] for tk in ticks) or 1.0

    def px(t):
        return L + (W - R - L) * t / t_end

    lanes = [tk["ego"] for tk in ticks if tk["ego"] is not None]
    lo, hi = min(lanes) - 0.4, max(lanes) + 0.4

    def py(lane, top, bot):
        return bot - (bot - top) * (lane - lo) / (hi - lo)

    panels = [("第 6 期（证据=原始，对照）", _simulate(ticks, smas, False)),
              ("第 7 期（证据=平滑）", _simulate(ticks, smas, True))]
    ph = H / 2
    for row, (title, (merges, req_s)) in enumerate(panels):
        top = TOP + row * ph
        bot = top + ph - BOT / 2
        d.text((L, top - 18), f"{session}  {title}  并线起点 {len(merges)} 次"
               f"  请求覆盖 {req_s:.1f}s", fill="black", font=font)
        # 零道参考线与道线
        for k in range(int(lo) - 1, int(hi) + 2):
            y = py(k, top + 6, bot)
            d.line((L, y, W - R, y), fill=(225, 225, 225))
        # 车轨迹
        pts = [(px(tk["t"]), py(tk["ego"], top + 6, bot))
               for tk in ticks if tk["ego"] is not None]
        d.line(pts, fill=(40, 40, 200), width=2)
        # 币/奖励车可见位置（浅色点）
        for tk in ticks:
            for kind, x, t, oid, raw in tk["objs"]:
                c = (250, 200, 90) if kind == "coin" else (230, 120, 200)
                d.ellipse((px(tk["t"]) - 1, py(x, top + 6, bot) - 1,
                           px(tk["t"]) + 1, py(x, top + 6, bot) + 1), fill=c)
        # 记录的实际机会并线（黑三角）
        for tk in ticks:
            if tk["merge_opp"] and tk["ego"] is not None:
                x0 = px(tk["t"])
                y0 = py(tk["ego"], top + 6, bot)
                d.polygon((x0, y0 - 9, x0 - 5, y0 + 3, x0 + 5, y0 + 3),
                          fill="black")
        # 模拟并线起点（红竖线）
        for t, ego, side in merges:
            if ego is None:
                continue
            x0 = px(t)
            y0 = py(ego, top + 6, bot)
            d.line((x0, y0 - 14, x0, y0 + 14), fill=(220, 30, 30), width=2)
        # 图例
        d.text((W - R - 430, top + 4), "─ 车轨迹  · 币/奖励车  ▲ 实际机会并线"
               "  | 模拟并线起点", fill=(80, 80, 80), font=font_s)
    img.save(out_png)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261101_220134")
    ap.add_argument("--png", default="")
    args = ap.parse_args(argv)
    ticks = _load(args.session)
    smas = _ema_series(ticks, TAU)
    for title, (merges, req_s) in (
            ("第 6 期（证据=原始）", _simulate(ticks, smas, False)),
            ("第 7 期（证据=平滑）", _simulate(ticks, smas, True))):
        actual = sum(1 for tk in ticks if tk["merge_opp"])
        print(f"{title}: 模拟并线起点 {len(merges)} 次、请求覆盖 {req_s:.1f}s；"
              f"记录的实际机会并线 {actual} 次")
        for t, ego, side in merges[:20]:
            print(f"  t={t:7.2f}s  ego={ego:+.2f} 道  并向 {side:+d}")
    if args.png:
        _draw(ticks, smas, args.png, args.session)
        print(f"路线图已写：{args.png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
