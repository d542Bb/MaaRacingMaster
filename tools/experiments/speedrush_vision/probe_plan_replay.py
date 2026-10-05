# -*- coding: utf-8 -*-
"""计划层 V0 验收回放：构造场景流 × 产线 DecisionEngine（闸开，离线）。

设计稿 §七 三检验的 V0 落地面（资格判例/帧抖动/字典序的**系统级**串演）：
脚本化 30s 候选流（金币兜底 → bonus 出现→持续性升级→接触兑现 → 目标消失→
解约重选 → 超车候选闪现抖动→持续性门 → 连续在场→换计划 → pass 兑现），
逐拍喂真引擎（plan_layer.enabled=true，轨迹采样关=决策语义隔离），产线实现
零复刻。输出：事件表 csv + 时间线 png（计划类阶梯 + 事件标线）——离散决策
的目检面（黑箱数字必须出可视图）。

用法：
  .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_plan_replay.py
"""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import replace
from pathlib import Path

import cv2  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.decision import DecisionEngine  # noqa: E402
from maaracing_master.plugins.speedrush.traffic import (  # noqa: E402
    OUTCOME_PASS, CarView, PassEvent)
from maaracing_master.plugins.speedrush.tracking import (  # noqa: E402
    CoinGroup, PerceptionHealth, TrackedTarget, WorldObservation)

OUT = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
       / "speedrush" / "plan_review")


def _engine() -> DecisionEngine:
    cfg = load_decision()
    cfg = replace(cfg,
                  plan_layer=replace(cfg.plan_layer, enabled=True),
                  mode=replace(cfg.mode, trajectory_sampling=False),
                  grid_veto=replace(cfg.grid_veto, enabled=False))
    return DecisionEngine(cfg)


def _group(gid, fid, x, cy=300, rel=5.0):
    ms = tuple(TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=0.85, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 400)
        for i in range(3))
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=3, estimated_count=3, x_center=x,
                  x_span=0.3, cy_min=cy, cy_max=cy + 6, conf_min=0.85,
                  partial_observation=False, first_seen_fid=fid,
                  last_seen_fid=fid, validity_until_fid=fid + 400)
    return g, ms


def _bonus(bid, fid, x, cy, rel=8.0):
    return TrackedTarget(
        id=bid, kind="bonus", x_lane=x, x_sigma=0.1, cy=cy, w=40, h=40,
        conf=0.9, rel_approach=rel, first_seen_fid=fid - 10,
        last_seen_fid=fid, validity_until_fid=fid + 400)


def _car(cid, x, cy, rel=30.0):
    return CarView(id=cid, x_lane=x, cy=cy, rel_approach=rel, d_min=1.0,
                   age_ticks=10, v_lat=0.0)


def _obs(fid, groups=(), targets=()):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 100_000_000,
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True,
                                bool(groups or targets), False),
        boundary=None, targets=targets, far_targets=(), coin_groups=groups)


DT = 0.1           # 拍长（s）：慢拍重评每 5 拍一次
V_EGO_PX = None    # 运行时取 calib
CONTACT_CY = None


def main() -> None:
    global V_EGO_PX
    eng = _engine()
    V_EGO_PX = eng.cal.v_ego
    n_ticks = 300                                # 30s
    rows, events = [], []
    executed = 0.0
    prev_reason = prev_state = None
    car_flicker = True                           # 超车候选闪现注入开关
    for k in range(n_ticks):
        fid = k + 1
        t = k * DT
        # ---- 候选流脚本（段注释=设计稿 §四.3 生命周期的串演）----
        groups, targets, views, evs = (), (), (), ()
        if t < 24.0:                             # 金币组 A 盖住前段（兜底/重选对象；
            g_a, ms_a = _group(3, fid, x=0.5)    #  留无候选空窗会触发 target_empty
            groups, targets = (g_a,), ms_a       #  → CONSERVE，那是另一条语义）
        if 2.0 <= t < 9.0:                       # bonus B 出现→逼近→接触
            cy_b = 200 + int((t - 2.0) * 80)     # 80px/s 逼近（rel=8px/tick@20Hz）
            targets = targets + (_bonus(555, fid, x=1.0, cy=min(cy_b, int(V_EGO_PX) + 20)),)
        if 14.0 <= t < 21.0:                     # 超车候选 C：先闪现抖动后连续在场
            v_c = _car(9, x=1.0, cy=200, rel=15.0)   # t_meet≈1.7s>变道耗时≈1.1s
            flick_on = ((t - 14.0) % 0.8) < 0.4 if t < 18.0 else True
            if flick_on:
                views = (v_c,)
        if 21.0 <= t < 22.0:
            evs = (PassEvent(track_id=9, outcome=OUTCOME_PASS, settled_fid=fid,
                             d_min=0.7, age_ticks=70),)
        # ---- 执行回执脚本：朝当拍输出点 2 道/s 追（够可行性门用）----
        out = eng.update(_obs(fid, groups, targets), DT,
                         executed_lane=executed, traffic=(views, evs))
        xt = out.x_target
        if xt is not None:
            executed += max(-2 * DT, min(2 * DT, xt - executed))
        # ---- 事件抽取（reason/state 变化沿）----
        if out.reason != prev_reason or out.state != prev_state:
            events.append((t, out.state.value, eng.target_kind,
                           out.target_id, out.reason))
            prev_reason, prev_state = out.reason, out.state
        rows.append((t, out.state.value, eng.target_kind, out.target_id,
                     out.reason, round(executed, 3),
                     None if xt is None else round(xt, 3)))

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "plan_replay_events.csv").open("w", encoding="utf-8",
                                               newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "state", "kind", "target_id", "reason"])
        w.writerows(events)

    # ---- 时间线 png（计划类阶梯 + 事件标线，cv2 同其它探针）----
    def _put(img, text, pos, scale=0.42, color=(230, 230, 230)):
        cv2.putText(img, text, (pos[0] + 1, pos[1] + 1),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1)
        cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1)

    W, H = 1400, 360
    x0, x1, y0 = 70, W - 20, 30
    band_h = 70
    T_END = rows[-1][0]

    def tx(t):
        return int(x0 + (x1 - x0) * t / T_END)

    cls_band = {"hold": 3, "coin": 2, "overtake": 1, "bonus": 0}
    cls_col = {"hold": (120, 120, 120), "coin": (0, 200, 255),
               "overtake": (0, 160, 0), "bonus": (255, 0, 255)}
    img = np.full((H, W, 3), 24, np.uint8)
    for name, by in cls_band.items():
        yb = y0 + by * band_h
        _put(img, name, (6, yb + band_h // 2 + 4))
        cv2.line(img, (x0, yb + band_h), (x1, yb + band_h), (60, 60, 60), 1)
    for i in range(len(rows) - 1):
        t0, st0, k0 = rows[i][0], rows[i][1], rows[i][2]
        t1 = rows[i + 1][0]
        band = cls_band.get(k0, 3) if st0 in ("CRUISE", "CHANGE") else -1
        yb = y0 + band * band_h
        cv2.rectangle(img, (tx(t0), yb + 12), (tx(t1), yb + band_h - 4),
                      cls_col.get(k0, (120, 120, 120))
                      if st0 in ("CRUISE", "CHANGE") else (60, 60, 160), -1)
    for t_ev, state, kind, tid, reason in events:
        if reason.split(":")[0] in ("select", "switch", "done", "cancel"):
            cv2.line(img, (tx(t_ev), y0), (tx(t_ev), H - 30), (0, 0, 220), 1)
            _put(img, reason, (tx(t_ev) - 60, H - 16), scale=0.36,
                 color=(80, 160, 255))
    for s in range(0, int(T_END) + 1, 2):
        _put(img, f"{s}s", (tx(s) - 8, H - 2), scale=0.34,
             color=(150, 150, 150))
    cv2.imwrite(str(OUT / "plan_replay_timeline.png"), img)
    # ---- 摘要 ----
    n_switch = sum(1 for e in events if e[4].startswith("switch"))
    n_done = sum(1 for e in events if e[4].startswith("done"))
    n_cancel = sum(1 for e in events if e[4].startswith("cancel"))
    print(f"ticks={n_ticks} events={len(events)} "
          f"switch={n_switch} done={n_done} cancel={n_cancel}")
    for e in events:
        print(f"  t={e[0]:5.1f} {e[1]:<13} kind={str(e[2]):<9} "
              f"id={str(e[3]):<5} {e[4]}")
    print(f"out -> {OUT}")


if __name__ == "__main__":
    main()
