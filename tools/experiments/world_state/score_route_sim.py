# -*- coding: utf-8 -*-
"""得分密度路径（原拟 6i）离线回放：现行政策 vs 追远币政策的收币对照。

从一局 speedrush 控制流水复算：

- 币组按身份（sel_dbg.opp 的 oid）跟踪，逐拍记录 (x, t, count)；
- 收币判据：组到站（t≤0.3s 的读数）时横位与组心差 ≤0.5 道；
- 现行政策 A = 记录轨迹直接判收（复算实况）；
- 6i 政策 B = 在 A 轨迹上叠加"追远币"模拟：keep 态、无记录并线时，对
  1<|x−ref|≤2.5 道、t≤6s、枚数≥阈值的币组，向中间道发一步请求——
  证据生命周期与实现同参（确认 0.25s、断拍冻结 0.2s、意图龄 1s），
  起意后按实测横移速度模拟并线，目标道被占 ≥0.25s 判撤销（回原道、
  冷却 2s），到达即更新参考道、可继续第二步；记录并线/安全段冻结
  跟随实况（安全优先于追币）。

局限（读数时须知）：选择层候选是漏斗下游（可见 33.5% → 候选 25.3%），
模拟只见候选；横移用中位速度常数，不含加减速；撤销只建模"目标道被占"，
不含计划层否决。

用法::

    python tools/experiments/world_state/score_route_sim.py \
        --session 20261102_142309 [--min-count 4]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# ---- 常量（与实现同参；来源 decision.json 2026-11-02） ----
CONFIRM_S = 0.25      # route.opportunity_confirm_s
GAP_S = 0.2           # route.request_gap_s
INTENT_AGE_S = 1.0    # route.request_intent_age_s
CANCEL_UNSAFE_S = 0.25
CANCEL_COOLDOWN_S = 2.0
LAT_SPEED = 0.45      # 实测并线横移中位（道/s）
HORIZON_A = 3.0       # 现行请求视界
HORIZON_B = 6.0       # 6i 追远币视界
MAX_OFF = 2.5         # 追币最大道距
CAPTURE_T = 0.3       # 到站判收窗（s）
CAPTURE_X = 0.5       # 判收横距（道）


def _load(session: str):
    p = (Path(os.environ.get("APPDATA", str(Path.home())))
         / "MaaRacingMaster" / "logs" / session / "trace" / "speedrush.jsonl")
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]
    t0 = rows[0]["ts_mono_ns"] / 1e9
    ticks = []
    prev_oid = {}
    for r in rows:
        sd = r.get("sel_dbg") or {}
        rt = r.get("route") or {}
        coins = sd.get("coin") or []
        oc = (sd.get("opp") or {}).get("coin") or []
        groups = []
        for c, e in zip(coins, oc):
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                continue
            if not isinstance(e, (list, tuple)) or len(e) < 1:
                continue
            groups.append({"oid": int(e[0]), "x": float(c[0]),
                           "t": None if c[1] is None else float(c[1])})
        ticks.append({
            "t": r["ts_mono_ns"] / 1e9 - t0,
            "ego": sd.get("ego"),
            "ref": rt.get("ref"),
            "keep": rt.get("why") != "merging"
                    and rt.get("vd") not in ("merge_start", "cancel"),
            "blk_l": r.get("blk_l"),
            "blk_r": r.get("blk_r"),
            "groups": groups,
        })
    return ticks


def _dt(ticks, i):
    if i == 0:
        return 0.0
    return max(0.0, min(0.2, ticks[i]["t"] - ticks[i - 1]["t"]))


def _capture(ticks, ego_seq):
    """按组判收：到站窗内横位差最小值 ≤0.5 道。返回 {oid: 最小差}。"""
    best = {}
    for tk, ego in zip(ticks, ego_seq):
        if ego is None:
            continue
        for g in tk["groups"]:
            if g["t"] is None or g["t"] > CAPTURE_T:
                continue
            d = abs(g["x"] - ego)
            if d < best.get(g["oid"], 9.9):
                best[g["oid"]] = d
    return {k: v for k, v in best.items() if v <= CAPTURE_X}


def _group_meta(ticks):
    """组首见道距分带（trace 无枚数列，按组计）。"""
    meta = {}
    for tk in ticks:
        for g in tk["groups"]:
            m = meta.setdefault(g["oid"], {"off0": None})
            if m["off0"] is None and g["t"] is not None and g["t"] > 1.0:
                m["off0"] = abs(g["x"])  # 相对 0 道近似；分带用
    return meta


def simulate_b(ticks):
    """6i 追远币模拟。返回 (ego_seq, 事件列表)。"""
    n = len(ticks)
    ego_seq = [tk["ego"] for tk in ticks]
    credit = {}          # side -> 累计秒
    last_seen = {}       # side -> 上次见到合格组的 tick t
    age = {}
    state = None         # {"dir", "from", "target", "progress", "unsafe"}
    cooldown = {}        # 目标道 -> 冷却到的时间
    events = []
    for i, tk in enumerate(ticks):
        dt = _dt(ticks, i)
        ego = tk["ego"]
        ref = tk["ref"] if tk["ref"] is not None else (
            round(ego) if ego is not None else None)

        # 记录并线/非保持段：冻结跟随实况，追币状态清空
        if not tk["keep"] or ego is None or ref is None:
            if state:
                events.append(("abort", tk["t"], state["target"]))
            state, credit, last_seen, age = None, {}, {}, {}
            continue

        if state is None:
            # 证据累计：找合格远币组
            qual = {}        # side -> 最近到站的合格组 t
            for g in tk["groups"]:
                if g["t"] is None or not (0.0 < g["t"] <= HORIZON_B):
                    continue
                off = g["x"] - ref
                if not (1.0 < abs(off) <= MAX_OFF):
                    continue
                # 可达性：横移时间 ≤ 到站
                if abs(off) / LAT_SPEED > g["t"] + 0.5:
                    continue
                side = 1 if off > 0 else -1
                blk = tk["blk_r"] if side > 0 else tk["blk_l"]
                if blk is not None:
                    continue          # 中间道被占
                tgt = ref + side
                if tk["t"] < cooldown.get(tgt, -1.0):
                    continue
                if side not in qual or g["t"] < qual[side]:
                    qual[side] = g["t"]
            for side in list(credit):
                if side not in qual \
                        and tk["t"] - last_seen.get(side, tk["t"]) > GAP_S:
                    credit.pop(side)
            for side in sorted(qual, key=lambda s: qual[s]):
                if tk["t"] - last_seen.get(side, -9.0) > GAP_S:
                    credit[side] = 0.0
                    age[side] = 0.0
                last_seen[side] = tk["t"]
                credit[side] = credit.get(side, 0.0) + dt
                age[side] = age.get(side, 0.0) + dt
                if age[side] > INTENT_AGE_S:
                    credit.pop(side, None)
                    continue
                if credit[side] >= CONFIRM_S:
                    state = {"dir": side, "from": ego, "target": ref + side,
                             "progress": 0.0, "unsafe": 0.0, "t0": tk["t"]}
                    credit, last_seen, age = {}, {}, {}
                    events.append(("chase", tk["t"], state["target"]))
                    break
            continue

        # 追币中：模拟横移
        tgt = state["target"]
        blk = tk["blk_r"] if state["dir"] > 0 else tk["blk_l"]
        if blk is not None:
            state["unsafe"] += dt
            if state["unsafe"] >= CANCEL_UNSAFE_S:
                events.append(("cancel", tk["t"], tgt))
                cooldown[tgt] = tk["t"] + CANCEL_COOLDOWN_S
                state = None
                continue
        else:
            state["unsafe"] = max(0.0, state["unsafe"] - dt)
        remain = tgt - ego_seq[i - 1] if i else tgt - ego
        step = LAT_SPEED * dt
        if abs(remain) <= step + 0.15:
            ego_seq[i] = float(tgt)
            events.append(("arrive", tk["t"], tgt))
            state = None
        else:
            ego_seq[i] = ego_seq[i - 1] + (step if remain > 0 else -step)
    return ego_seq, events


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="20261102_142309")
    args = ap.parse_args(argv)
    ticks = _load(args.session)
    meta = _group_meta(ticks)

    ego_a = [tk["ego"] for tk in ticks]
    cap_a = _capture(ticks, ego_a)
    ego_b, events = simulate_b(ticks)
    cap_b = _capture(ticks, ego_b)

    chases = [e for e in events if e[0] == "chase"]
    cancels = [e for e in events if e[0] == "cancel"]
    print(f"== {args.session}  {ticks[-1]['t']:.0f}s  组数 {len(meta)}")
    print(f"现行 A: 收 {len(cap_a)} 组")
    print(f"6i   B: 收 {len(cap_b)} 组"
          f"  （追币并线 {len(chases)} 次，撤销 {len(cancels)} 次）")
    gained = sorted(set(cap_b) - set(cap_a))
    lost = sorted(set(cap_a) - set(cap_b))
    print(f"B 多收 {len(gained)} 组；B 丢失 {len(lost)} 组")
    bands = {"1~1.5": 0, "1.5~2": 0, "2~2.5": 0}
    for o in gained:
        off = meta[o]["off0"] or 0
        for lo, hi, k in ((1.0, 1.5, "1~1.5"), (1.5, 2.0, "1.5~2"),
                          (2.0, 2.51, "2~2.5")):
            if lo <= off < hi:
                bands[k] += 1
    print(f"多收组的首见道距分带: {bands}")
    for kind, t, tgt in events:
        print(f"  {kind:6s} t={t:7.1f}s 目标道 {tgt:+d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
