# -*- coding: utf-8 -*-
"""metric distance 对现有纵向时机信号（t_meet）的增量价值回溯探针。

预登记（2026-10-08，判据先死数据后到；设计稿 §4「A 线增量实验」段）：

  问题：决策器已有纵向时机量 t_meet = (v_ego − cy) / (rel × 20/s)——纯 2D
  像素口径（rel 是 px/fid 的接近速率 EMA，20/s 消费含 ~1.9x TTC 高估，此处
  **同口径复现**，A 臂目的就是复现决策器看到的量）。metric distance（深度
  锚点）尚未接入决策器。本探针回答：在 FSM 严格时间轴下，distance 对超车
  计划结局是否有 t_meet 之外的增量信息。

  时间轴（FSM 事件，非实验锁定段——锁定段是探针分析单位，与 FSM 计划
  不一一对应，2026-10-08 产线对齐定案）：
    select:score_win(target_kind=overtake) → 终态
      done:overtake_pass / pass_timeout        → pass
      cancel:overtake_lost（同 target_id）      → lost
      cancel:traj_blocked / cancel:lat_veto     → 阴性对照组（横向问题，
                                                   纵向时机中性）
    switch:* 结局的计划剔除；无终态（局尾截断）剔除；abort:settled 是
    cancel 后的回稳恢复拍，不是独立结局。

  三臂：
    A = t0（select 拍）重算 t_meet（决策器同口径）
    B = t0 的 metric distance（dist[] 同 id）
    增量 = t_meet 中位数二分箱后的箱内 B-AUC（控制暴露时长混杂后的层内
    信号——t0 四结局按 t_meet 单调排列（pass<blocked≈veto<lost），与
    「计划暴露时长」混杂一致，全体 AUC 会把"车近=计划短"误当时机信号）

  AUC 方向约定：正类=pass，AUC = P(读数_pass < 读数_failure)（读数小
  ↔ pass；t_meet 与 distance 都按"近=快出画"假设，方向由数据落）。

  判决（四态，预登记）：
    INCREMENTAL  A/B 两臂组内计划数均 ≥10（MIN_GROUP_N），B 全体 AUC
                 ≥0.65（AUC_HOLD）且箱内均值 ≥0.70（INCR_HOLD）且两箱
                 方向一致（箱 AUC 均 >0.5）
    REDUNDANT    B 全体 ≥0.65 但箱内不达增量线——distance 单独有信号，
                 与 t_meet 同信息
    NOT_SUPPORTED B 全体 <0.65 且箱内无增量
    INCONCLUSIVE  任一臂组内计划数 <10——样本不足时不判方向，扩数据
    阴性对照（混杂警示，不单独判决）：A 臂对 pass vs 阴性组的 AUC ≥
    主对照 AUC − 0.05（NEG_MARGIN）时，INCREMENTAL 降级 REDUNDANT
    （暴露混杂未排除）。

  已知限制（摸底 2026-10-08，8 局）：lost 组 t0 距离读数 5/12（src
  UNKNOWN 居多），当前预期 INCONCLUSIVE；预登记的意义是判据与仪器先
  定死，新局 trace 复跑零成本，攒够即出判决。

用法：.venv python probe_metric_increment.py <trace.jsonl> [...]
（dist 列与 select reason 需 25ac9e1 后 trace；v_ego 直读 resources/
calibration/gate0.json，与 load_calib 同源不 import 生产代码。）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HZ_TICK = 20.0        # 决策器 t_meet 消费口径（px/fid → px/s 的常数，含已知高估）
PRE_WIN_MAX = 12      # select 前连续 CRUISE 拍上限
MIN_GROUP_N = 10      # 每结局组该臂计划数门槛
AUC_HOLD = 0.65       # 单臂信号下限
INCR_HOLD = 0.70      # 层内增量下限
NEG_MARGIN = 0.05     # 阴性对照混杂警示余量

_CAL = Path(__file__).resolve().parents[3] / \
    "maaracing_master/plugins/speedrush/resources/calibration/gate0.json"


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        beats = [json.loads(l) for l in f if l.strip()]
    beats.sort(key=lambda b: b["fid"])
    return beats


def auc_small_is_pos(pos: list[float], neg: list[float]) -> float | None:
    """P(pos < neg) + 0.5·P(equal)；读数小 ↔ pass。任一侧空返回 None。"""
    if not pos or not neg:
        return None
    win = tie = 0
    for a in pos:
        for b in neg:
            if a < b:
                win += 1
            elif a == b:
                tie += 1
    return (win + 0.5 * tie) / (len(pos) * len(neg))


def collect(plans: list[dict], arm: str, pos_out: str, neg_out: tuple[str, ...]
            ) -> tuple[list[float], list[float], int, int]:
    """臂读数按结局分组；返回 (pos读数, neg读数, pos计划数, neg计划数)。"""
    pos = [p[arm] for p in plans if p["outcome"] == pos_out and p[arm] is not None]
    neg = [p[arm] for p in plans if p["outcome"] in neg_out and p[arm] is not None]
    n_pos = sum(1 for p in plans if p["outcome"] == pos_out and p[arm] is not None)
    n_neg = sum(1 for p in plans if p["outcome"] in neg_out and p[arm] is not None)
    return pos, neg, n_pos, n_neg


def main() -> None:
    v_ego = json.loads(_CAL.read_text(encoding="utf-8"))["v_ego"]
    plans: list[dict] = []
    for path in sys.argv[1:]:
        beats = load(path)
        for i, b in enumerate(beats):
            if b.get("reason") != "select:score_win" \
                    or b.get("target_kind") != "overtake":
                continue
            tid = b.get("target_id")
            outcome = None
            for b2 in beats[i + 1:]:
                r = b2.get("reason", "")
                if r.startswith("switch:"):
                    outcome = "switched"
                    break
                if r in ("done:overtake_pass", "done:overtake_pass_presumed",
                         "pass_timeout"):
                    outcome = "pass"
                    break
                if r.startswith("cancel:"):
                    o = r.replace("cancel:", "")
                    outcome = o if (o != "overtake_lost"
                                    or b2.get("target_id") == tid) else "lost"
                    break
            if outcome in (None, "switched"):
                continue
            c0 = next((c for c in b.get("car_pos") or [] if c["id"] == tid), None)
            d0 = next((d for d in b.get("dist") or [] if d["id"] == tid), None)
            t_meet = ((v_ego - c0["cy"]) / (c0["rel"] * HZ_TICK)
                      if c0 and c0.get("rel") else None)
            plans.append({"outcome": outcome, "t_meet": t_meet,
                          "dist": (d0 or {}).get("m"),
                          "age": (d0 or {}).get("age"),
                          "fid": b["fid"], "file": Path(path).name})

    oc: dict[str, int] = {}
    for p in plans:
        oc[p["outcome"]] = oc.get(p["outcome"], 0) + 1
    print(f"超车 select 计划 n={len(plans)}  结局: {oc}（switched/无终态已剔除）")

    POS, NEG_MAIN, NEG_CTRL = "pass", ("overtake_lost",), ("traj_blocked", "lat_veto")

    # ---- A 臂：t_meet（决策器同口径）----
    a_pos, a_neg, a_np, a_nn = collect(plans, "t_meet", POS, NEG_MAIN)
    a_auc = auc_small_is_pos(a_pos, a_neg)
    a_ctrl_pos, a_ctrl_neg, _, _ = collect(plans, "t_meet", POS, NEG_CTRL)
    a_ctrl_auc = auc_small_is_pos(a_ctrl_pos, a_ctrl_neg)
    print(f"\n[A 臂 t_meet(t0, 重算同口径)] pass n={a_np} lost n={a_nn}  "
          f"AUC(pass小)={a_auc and f'{a_auc:.2f}'}")
    print(f"  阴性对照 pass vs blocked+veto: n={len(a_ctrl_pos)}/{len(a_ctrl_neg)} "
          f"AUC={a_ctrl_auc and f'{a_ctrl_auc:.2f}'}")

    # ---- B 臂：metric distance（t0）----
    b_pos, b_neg, b_np, b_nn = collect(plans, "dist", POS, NEG_MAIN)
    b_auc = auc_small_is_pos(b_pos, b_neg)
    print(f"\n[B 臂 metric dist(t0)] pass n={b_np} lost n={b_nn}  "
          f"AUC(pass小)={b_auc and f'{b_auc:.2f}'}")

    # ---- 层内增量：t_meet 中位数二分箱 ----
    incr_auc = dir_ok = None
    if a_auc is not None and b_np + b_nn >= 4:
        pairs = [p for p in plans if p["outcome"] in (POS, "overtake_lost")
                 and p["t_meet"] is not None and p["dist"] is not None]
        med = sorted(p["t_meet"] for p in pairs)[len(pairs) // 2]
        bin_aucs = []
        for name, sel in (("低t_meet箱", lambda t: t <= med),
                          ("高t_meet箱", lambda t: t > med)):
            pp = [p["dist"] for p in pairs
                  if p["outcome"] == POS and sel(p["t_meet"])]
            nn = [p["dist"] for p in pairs
                  if p["outcome"] == "overtake_lost" and sel(p["t_meet"])]
            a = auc_small_is_pos(pp, nn)
            bin_aucs.append(a)
            print(f"  层内 {name}: pass={len(pp)} lost={len(nn)} "
                  f"AUC={a and f'{a:.2f}'}")
        vals = [a for a in bin_aucs if a is not None]
        if vals:
            incr_auc = sum(vals) / len(vals)
            dir_ok = len(vals) == 2 and all(a > 0.5 for a in vals)

    # ---- 判决（预登记四态）----
    print("\n[判决]")
    armed = a_np >= MIN_GROUP_N and a_nn >= MIN_GROUP_N
    b_armed = b_np >= MIN_GROUP_N and b_nn >= MIN_GROUP_N
    verdict = None
    if not b_armed:
        verdict = (f"INCONCLUSIVE（B 臂组内计划数不足门槛 {MIN_GROUP_N}："
                   f"pass {b_np} / lost {b_nn}——扩数据后复跑，判据已死不动）")
    elif b_auc is not None and b_auc >= AUC_HOLD and incr_auc is not None \
            and incr_auc >= INCR_HOLD and dir_ok:
        verdict = "INCREMENTAL"
    elif b_auc is not None and b_auc >= AUC_HOLD:
        verdict = "REDUNDANT（distance 单独有信号但层内无增量——与 t_meet 同信息）"
    else:
        verdict = "NOT_SUPPORTED"
    # 阴性对照混杂警示：仅在有正面结论时降级
    if verdict == "INCREMENTAL" and a_auc is not None and a_ctrl_auc is not None \
            and a_ctrl_auc >= a_auc - NEG_MARGIN:
        verdict = "REDUNDANT（阴性对照违例：t_meet 对横向失败组同样分离，"
        "暴露混杂未排除，增量不可信）"
    print(f"  => {verdict}")

    # 附：t0 读数分布（报告，不判决）
    for arm in ("t_meet", "dist"):
        for o in (POS, "overtake_lost", "traj_blocked", "lat_veto"):
            xs = sorted(p[arm] for p in plans
                        if p["outcome"] == o and p[arm] is not None)
            if xs:
                print(f"  t0 {arm:6s} {o:14s} n={len(xs):2d} "
                      f"p50={xs[len(xs)//2]:7.1f}")


if __name__ == "__main__":
    main()
