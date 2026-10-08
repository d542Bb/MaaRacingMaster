# -*- coding: utf-8 -*-
"""lost 终态形态取证探针（stdout-only，2026-10-08 挂账①）。

背景（设计稿 §4A）：26 个 lost 疑含大量"空间上已越过"的冤案（侧缘出画/
自车投影区遮挡），pass 判据只认下沿出画。本探针从 trace 重建每个 lost
计划的目标轨迹，把终态形态定量化，并检查"消失后邻位新 id 重现"（换 id/
重检出签名）——为 lost 标签语义处置（三案型分流）提供证据。

口径（与产线一致/同源直读，不 import 生产代码）：
  bottom_exit ⟺ last_cy ≥ v_ego − exit_margin_px（gate0.json v_ego=716、
  decision.json exit_margin_px=80 → 636）；FSM 时间轴同
  probe_metric_increment（select:score_win/overtake → 终态）。

trace 重建的两个关键事实：
  car_pos.x 是车道坐标（道，0=路中心），像素 cx 经 gate0 标定反推：
    cx_px = vpx + (x/a_x + base)·(cy − y_h)，base = (ego_cx−vpx)/(v_ego−y_h)
  跟踪 coasting 签名：检测死后航位推算只推 cy——(x, rel, h) 三元组冻结
  而 cy 匀速爬（实机 tid75 案：11472 起 x=1.55/rel=6.45/h=364 冻结，cy
  524→621，反推 cx 1168→1424 滑出右缘）。冻结前的最后拍 = 真实检测末点。

形态三分（取证口径）：
    bottom      终拍 cy ≥ 636（真·下沿出画，含 coast 推算段）
    side_right  终拍反推 cx ≥ 1280 或真检末点 cx ≥ 1260（右缘出画）
    side_left   终拍反推 cx ≤ 0 或真检末点 cx ≤ 20（左缘出画）
    inframe     其余（画内失联——遮挡取证的主对象）
    dead_report coast 期 cy 也冻结（航位推算都停了，异常案）

邻位重现：真实检测末点后 0.6s（12 决策拍）内，异 id 出现在其 150px 邻域
→ 换 id/重检出候选（非真消失）。

用法：.venv python probe_lost_vanish.py <trace.jsonl> [...]
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys
from pathlib import Path

HZ_BEATS_PER_06S = 12   # 决策拍 20Hz → 0.6s ≈ 12 拍
EDGE_PX = 20.0          # 画缘余量
REAP_R_PX = 150.0       # 邻位重现半径
BOTTOM_MARGIN = 80.0    # 产线 exit_margin_px（decision.json）
CY_NEAR = 430.0         # 判据预演：纵向贴身阈值（真检 cy 数据缺口 404~437 之间）
ADJ_LANE = 2.5          # 判据预演：邻道横向界（道）

_CAL = Path(__file__).resolve().parents[3] / \
    "maaracing_master/plugins/speedrush/resources/calibration/gate0.json"


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        beats = [json.loads(l) for l in f if l.strip()]
    beats.sort(key=lambda b: b["fid"])
    return beats


def car_map(b: dict) -> dict[int, dict]:
    out = {}
    for c in b.get("car_pos") or []:
        out[c["id"]] = c
    return out


def h_of(b: dict, tid: int) -> float | None:
    ch = b.get("car_h")
    if isinstance(ch, list):
        e = next((e for e in ch if e.get("id") == tid), None)
        return e.get("h") if e else None
    if isinstance(ch, dict):
        return ch.get(str(tid), ch.get(tid))
    return None


def invert_px(x: float | None, cy: float, cal: dict) -> float | None:
    """车道位 → 像素 cx（gate0 反推）；x 缺失返回 None。"""
    if x is None:
        return None
    base = (cal["ego_cx"] - cal["vpx"]) / (cal["v_ego"] - cal["y_h"])
    return cal["vpx"] + (x / cal["a_x"] + base) * (cy - cal["y_h"])


def main() -> None:
    cal = json.loads(_CAL.read_text(encoding="utf-8"))
    bottom_thr = cal["v_ego"] - BOTTOM_MARGIN
    plans: list[dict] = []
    oc_all: dict[str, int] = {}
    for path in sys.argv[1:]:
        beats = load(path)
        for i, b in enumerate(beats):
            if b.get("reason") != "select:score_win" \
                    or b.get("target_kind") != "overtake":
                continue
            tid = b.get("target_id")
            outcome, o_fid = None, None
            for b2 in beats[i + 1:]:
                r = b2.get("reason", "")
                if r.startswith("switch:"):
                    outcome = "switched"
                    break
                if r in ("done:overtake_pass", "done:overtake_pass_presumed",
                         "pass_timeout"):
                    outcome, o_fid = "pass", b2["fid"]
                    break
                if r.startswith("cancel:"):
                    o = r.replace("cancel:", "")
                    if o == "overtake_lost" and b2.get("target_id") == tid:
                        outcome, o_fid = "lost", b2["fid"]
                    else:
                        outcome, o_fid = o, b2["fid"]
                    break
            if outcome in (None, "switched"):
                continue
            oc_all[outcome] = oc_all.get(outcome, 0) + 1
            # 目标轨迹：select → 终态（sightings: fid, x, cy, rel, h）
            sightings: list[list] = []
            for b2 in beats[i:]:
                if o_fid is not None and b2["fid"] > o_fid:
                    break
                c = car_map(b2).get(tid)
                if c is not None and c.get("cy") is not None:
                    sightings.append([b2["fid"], c.get("x"), float(c["cy"]),
                                      c.get("rel"), h_of(b2, tid)])
            if not sightings:
                continue
            # coast 识别：末尾连续拍的 (x, rel, h) 与终拍完全相同且 cy 在动
            coast = 0
            if len(sightings) >= 2:
                fx, fr, fh = sightings[-1][1], sightings[-1][3], sightings[-1][4]
                for s in reversed(sightings[:-1]):
                    if s[1] == fx and s[3] == fr and s[4] == fh:
                        coast += 1
                    else:
                        break
            true_i = len(sightings) - 1 - coast
            true = sightings[true_i]
            fin = sightings[-1]
            f_cy = fin[2]
            f_cx = invert_px(fin[1], f_cy, cal)
            t_cx = invert_px(true[1], true[2], cal)
            dead_report = coast > 0 and fin[2] == sightings[true_i][2]
            if dead_report:
                form = "dead_report"
            elif f_cy >= bottom_thr:
                form = "bottom"
            elif (f_cx is not None and f_cx >= 1280) or \
                    (t_cx is not None and t_cx >= 1280 - EDGE_PX):
                form = "side_right"
            elif (f_cx is not None and f_cx <= 0) or \
                    (t_cx is not None and t_cx <= EDGE_PX):
                form = "side_left"
            else:
                form = "inframe"
            plans.append({
                "file": Path(path).stem.replace("trace_", ""), "tid": tid,
                "outcome": outcome, "sel_fid": b["fid"], "o_fid": o_fid,
                "n_seen": len(sightings), "last_fid": fin[0],
                "true_fid": true[0], "true_cx": t_cx, "true_cy": true[2],
                "true_x": true[1], "true_h": true[4], "true_rel": true[3],
                "fin_cx": f_cx, "fin_cy": f_cy, "coast": coast,
                "form": form, "beats": beats,
            })

    print(f"超车 select 计划 n={len(plans)}  结局: {oc_all}")
    lost = [p for p in plans if p["outcome"] == "lost"]
    forms: dict[str, int] = {}
    for p in lost:
        forms[p["form"]] = forms.get(p["form"], 0) + 1
    print(f"\n[lost 终态形态] n={len(lost)}  bottom 阈 cy≥{bottom_thr:.0f}"
          f"  构成: {forms}")

    print(f"\n{'会话':<17} {'tid':>4} {'sel_fid':>7} {'真检fid':>7} "
          f"{'真cx':>6} {'真cy':>5} {'h':>4} {'rel':>6} {'coast':>5} "
          f"{'终cx':>6} {'终cy':>5} {'形态':>11}")
    for p in sorted(lost, key=lambda q: (q["form"], q["true_cy"] or 0)):
        t_cx = f"{p['true_cx']:.0f}" if p["true_cx"] is not None else "None"
        f_cx = f"{p['fin_cx']:.0f}" if p["fin_cx"] is not None else "None"
        trel = f"{p['true_rel']:.2f}" if p["true_rel"] is not None else "None"
        print(f"{p['file']:<17} {p['tid']:>4} {p['sel_fid']:>7} {p['true_fid']:>7} "
              f"{t_cx:>6} {p['true_cy']:>5.0f} "
              f"{p['true_h'] if p['true_h'] is not None else -1:>4.0f} {trel:>6} "
              f"{p['coast']:>5} {f_cx:>6} {p['fin_cy']:>5.0f} {p['form']:>11}")

    # 邻位重现检查（lost 全部，锚=真实检测末点）
    print(f"\n[邻位重现] 真检末点后 0.6s 内 {REAP_R_PX:.0f}px 邻域异 id：")
    any_hit = False
    for p in lost:
        hits: list[str] = []
        for b2 in p["beats"]:
            if b2["fid"] <= p["true_fid"] or b2["fid"] > p["true_fid"] + HZ_BEATS_PER_06S:
                continue
            for cid, c in car_map(b2).items():
                if cid == p["tid"] or c.get("x") is None or c.get("cy") is None \
                        or p["true_cx"] is None:
                    continue
                ccx = invert_px(c["x"], c["cy"], cal)
                if ccx is None:
                    continue
                d2 = (ccx - p["true_cx"]) ** 2 + (c["cy"] - p["true_cy"]) ** 2
                if d2 <= REAP_R_PX ** 2:
                    hits.append(f"fid{b2['fid']}:id{cid}({ccx:.0f},{c['cy']:.0f})")
        if hits:
            any_hit = True
            print(f"  {p['file']} tid{p['tid']} {p['form']}: "
                  f"{'; '.join(sorted(set(hits))[:4])}")
    if not any_hit:
        print("  （无——lost 目标消失后 0.6s 内均无异 id 邻位重现）")

    # pass 对照：终拍 cy 分布 + 真越线/幽灵越线拆分
    pw = [p for p in plans if p["outcome"] == "pass"]
    if pw:
        lows = [p for p in pw if p["fin_cy"] < bottom_thr]
        cys = sorted(p["fin_cy"] for p in pw)
        print(f"\n[pass 对照] n={len(pw)}  终拍 cy p50={cys[len(cys)//2]:.0f} "
              f"min={cys[0]:.0f}  低于 {bottom_thr:.0f} 的 {len(lows)} 个")
        real = [p for p in pw if p["true_cy"] is not None and p["true_cy"] >= bottom_thr]
        ghost = [p for p in pw if p["true_cy"] is not None and p["true_cy"] < bottom_thr]
        print(f"  真实检测越线（真检 cy≥{bottom_thr:.0f}）: {len(real)}"
              f"  幽灵 coast 越线（真检 cy<{bottom_thr:.0f}）: {len(ghost)}"
              f"  → pass 的 {len(ghost) / max(len(pw), 1) * 100:.0f}% 最后一程靠外推走完")
        coasts = sorted(p["coast"] for p in pw)
        print(f"  pass 组 coast 拍数 p50={coasts[len(coasts)//2]}  "
              f"max={coasts[-1]}（幽灵框在宽限期内跨线才判 pass）")

    # 判据预演（用户提案 2026-10-08）：近自车 + 消失方向指向屏幕外
    #   presumed_pass ⟺ 真检 cy ≥ CY_NEAR（纵向贴身）且 |x_lane| ≤ ADJ_LANE
    #   （邻道内）且 rel > 0（还在下滑——消失方向指向下缘）。三字段全是
    #   CarView 现成量，判据一行，不添传感器。离线预演 77 计划看召回/误伤。
    print(f"\n[判据预演] presumed_pass ⟺ 真检 cy≥{CY_NEAR:.0f} 且 |x_lane|≤{ADJ_LANE} "
          f"且 rel>0")
    promo: list[dict] = []
    for p in lost:
        hit = (p["true_cy"] is not None and p["true_cy"] >= CY_NEAR
               and p["true_x"] is not None and abs(p["true_x"]) <= ADJ_LANE
               and p["true_rel"] is not None and p["true_rel"] > 0)
        p["rule_presumed"] = hit
        if hit:
            promo.append(p)
    print(f"  lost → presumed_pass 晋升 {len(promo)}/26：")
    for p in sorted(promo, key=lambda q: q["true_cy"]):
        print(f"    {p['file']} tid{p['tid']} {p['form']:<11} "
              f"cy={p['true_cy']:.0f} x={p['true_x']:+.2f} rel={p['true_rel']:.2f}")
    stayed = [p for p in lost if not p["rule_presumed"]]
    stay_ids = [f"{p['file'][-6:]}tid{p['tid']}(cy{p['true_cy']:.0f})" for p in stayed]
    print(f"  保持 lost {len(stayed)}：{stay_ids}")
    ok = [p for p in plans if p["outcome"] == "pass"]
    agree = sum(1 for p in ok if p["true_cy"] is not None and p["true_cy"] >= CY_NEAR
                and (p["true_x"] is None or abs(p["true_x"]) <= ADJ_LANE)
                and (p["true_rel"] is None or p["true_rel"] > 0))
    print(f"  pass 组一致性：{agree}/{len(ok)} 也满足本判据"
          f"（真检贴身下滑的真值自洽检查）")

    # 游戏判分对照（RULES §494：超车=超相邻车道车辆每辆 30 分；rate_b 含全部
    # 来源且 30 分单位无法区分超车/金币/动作 → 只做组间差分，不做事件判定）
    print("\n[游戏判分对照] rate_b 在真检窗口的 +30 跳变命中（≥25 分差）")
    hud_map: dict[str, list[tuple[float, float, int]]] = {}   # stem → [(t, fid, rate)]
    for path in sys.argv[1:]:
        stem = Path(path).stem.replace("trace_", "")
        hpath = os.path.join(os.path.dirname(path), f"hud_{stem}", "hud.jsonl")
        if not os.path.exists(hpath):
            continue
        rows = [json.loads(l) for l in open(hpath, encoding="utf-8") if l.strip()]
        fr = [(r["frame_id"], r["ts_ns"]) for r in rows]
        rate = [((r["frame_id"], r["ts_ns"]), r["fields"]["rate_b"]["value"])
                for r in rows if (r["fields"].get("rate_b") or {}).get("trusted")]
        if len(fr) < 2 or not rate:
            continue
        def t_of(f: int) -> float | None:
            if f < fr[0][0] or f > fr[-1][0]:
                return None
            for (f0, t0), (f1, t1) in zip(fr, fr[1:]):
                if f0 <= f <= f1:
                    return (t0 + (t1 - t0) * (f - f0) / max(f1 - f0, 1)) / 1e9
            return None
        hud_map[stem] = [(t_of(f), f, v) for (f, _), v in rate if t_of(f) is not None]

    def group_of(p: dict) -> str:
        if p["outcome"] == "pass":
            return "pass"
        if p["form"] == "inframe" and (p["true_h"] or 0) < 147:
            return "lost远车"
        if p["form"] == "inframe":
            return "lost近距"
        return f"lost{p['form']}"

    for p in plans:
        if p["outcome"] not in ("pass", "lost"):
            continue
        p["grp"] = group_of(p)
        p["game_hit"] = None
        ser = hud_map.get(p["file"])
        if not ser or p["true_cy"] is None:
            continue
        t_true = next((t for t, f, _ in ser if abs(f - p["true_fid"]) < 1), None)
        if t_true is None:
            t_true = min(ser, key=lambda r: abs(r[1] - p["true_fid"]))[0]
        base = [v for t, _, v in ser if t_true - 3.5 <= t <= t_true - 1.0]
        peak = [v for t, _, v in ser if t_true - 0.5 <= t <= t_true + 2.0]
        if base and peak:
            p["game_hit"] = (max(peak) - statistics.median(base)) >= 25
    for g in ("pass", "lost近距", "lost远车", "lostside_left", "lostside_right"):
        xs = [p for p in plans if p.get("grp") == g and p["game_hit"] is not None]
        if not xs:
            continue
        hit = sum(1 for p in xs if p["game_hit"])
        # 判读警告：+30 跳变含金币/动作/无关超车，窗口命中率 ~60% 与背景事件率
        # 同量级（pass 59% ≈ lost远车 67%）——本段只能证伪"组间差异巨大"，
        # 不能证明单个计划被游戏判分；事件级真值需 hud 捕获左上"超车"计数。
        print(f"  {g:<16} n={len(xs):>3}  +30跳变命中 {hit}（{hit / len(xs) * 100:.0f}%）"
              f"（背景事件率同量级，无组间区分力）")


if __name__ == "__main__":
    main()
