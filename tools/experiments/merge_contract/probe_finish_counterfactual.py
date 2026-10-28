#!/usr/bin/env python
"""完成判据证据门的离线反事实重放（只读；不改生产控制）。

把路线层逐拍重放成 4 个版本，比较完成判据的两处证据门：

- A：现状基线（同时逐拍核对生产 trace，0 处不符才允许看反事实）。
- B：速度项只认**两端都新鲜**的差商（保持值商 0 不算「横速落稳」）。
- C：位置项拒绝**由 hop=1（换档）新鲜读数确定**的到达位置。
- D：B 且 C。

另跑 S 作敏感性对照：速度项要求**两端都新鲜**（完成判定器的 ``measured``
同义）。S 不属于 A/B/C/D 矩阵，只用来回答「更严的速度门会怎样」。

输入全部来自 debug trace；路线逻辑直接调用生产 ``route.tick_route``，
变体只在 ``finish`` 判定成立时改写参数重算，不复制状态机。任何变体的
新结局都按原始流如实报告；没有对应观测就记「不可确定」，不补造完成。

用法::

    python probe_finish_counterfactual.py --session 20261028_183009_1
    python probe_finish_counterfactual.py --baseline-out <仓库外路径>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR_DEFAULT = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "MaaRacingMaster", "logs")
_APPDATA = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _repo_root() -> str | None:
    d = _HERE
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd


_ROOT = _repo_root()
if _ROOT and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from maaracing_master.plugins.speedrush import route as R          # noqa: E402
from maaracing_master.plugins.speedrush.coarse import (           # noqa: E402
    CoarseLane, CoarseReport)
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402


# ---------------------------------------------------------------- 证据类

def pos_class(row: dict) -> str:
    """新鲜 = dgeo_new=true 且 ro_hold=false（同拍消费的新深度证据）。"""
    if row.get("road_offset") is None:
        return "missing"
    if row.get("dgeo_new") is True and row.get("ro_hold") is False:
        return "fresh"
    if row.get("ro_hold") is True or row.get("dgeo_new") is False:
        return "hold"
    return "unknown"


def fresh(row: dict) -> bool:
    return pos_class(row) == "fresh"


def coarse_ego(row: dict):
    c = row.get("coarse")
    return None if not isinstance(c, dict) else c.get("ego")


def route_of(row: dict) -> dict:
    r = row.get("route")
    return r if isinstance(r, dict) else {}


def report_of(row: dict):
    c = row.get("coarse")
    if not isinstance(c, dict):
        return None
    try:
        lanes = tuple(CoarseLane(float(x[0]), float(x[1]), x[2], x[3],
                                 bool(x[4])) for x in c["cand"])
        return CoarseReport(float(c["ego"]), float(c["lane"]), int(c["dir"]),
                            float(c["free"]), c["why"], lanes)
    except (KeyError, TypeError, ValueError):
        return None


def params_from(cfg) -> R.RouteParams:
    """与 DecisionEngine.__init__ 同一组真源（不在探针里另定数值）。"""
    return R.RouteParams(
        switch_gain_m=cfg.coarse.switch_margin_m,
        proxy_speed_mps=cfg.coarse.range_speed_mps,
        urgent_confirm_ticks=cfg.route.urgent_confirm_ticks,
        opportunity_confirm_ticks=cfg.route.opportunity_confirm_ticks,
        cancel_unsafe_ticks=cfg.route.cancel_unsafe_ticks,
        cancel_cooldown_s=cfg.route.cancel_cooldown_s,
        finish_tol_lane=cfg.selection.center_band_lane,
        finish_v_tol_lane_s=cfg.route.finish_v_tol_lane_s,
        merge_max_s=cfg.route.merge_max_s,
        soft_car=cfg.route.soft_car)


# ---------------------------------------------------------------- 逐拍重放

class Replay:
    """一次重放：路线状态、请求时序、读数新鲜度谱系与逐拍定案。"""

    def __init__(self, rows, params, variant: str):
        self.rows = rows
        self.p = params
        self.variant = variant
        self.ticks = []          # [(row_index, verdict)]
        self.lineage = []        # 每拍结束时的 (road_offset, hop, fid) 谱系
        self.ctx = {"allow_finish": True}

    def _gate(self, cur_row, prev_routed_row, cur_ego):
        if self.variant == "A":
            return True
        prev_ego = (None if prev_routed_row is None
                    else coarse_ego(prev_routed_row))
        dt = cur_row.get("dt")
        q = (None if cur_ego is None or prev_ego is None or not dt
             else (cur_ego - prev_ego) / dt)
        speed_ok = True
        if self.variant in ("B", "D"):
            # 只拒「保持值商 0」：读数没更新且差商恰为 0，速度项没有证据。
            speed_ok = not (not fresh(cur_row) and q is not None
                            and abs(q) < 1e-9)
        if self.variant == "S":
            # 敏感性对照（不属于 A/B/C/D 矩阵）：两端都新鲜才算实测横速。
            speed_ok = (prev_routed_row is not None
                        and fresh(prev_routed_row) and fresh(cur_row))
        pos_ok = True
        if self.variant in ("C", "D"):
            pc = pos_class(cur_row)
            if pc in ("unknown", "missing"):
                pos_ok = True          # 证据不足不归因，放行
            elif pc == "fresh":
                pos_ok = cur_row.get("ro_hop") != 1
            else:
                # 契约口径：保持值等于上一次新鲜路心读数（同一序列），
                # 且那份读数 incoming hop=1，位置项才由它确定。
                ro = cur_row.get("road_offset")
                lf = self.last_fresh
                if ro is not None and lf is not None \
                        and abs(ro - lf[0]) <= 1e-3 and lf[1] == 1:
                    pos_ok = False
        return speed_ok and pos_ok

    def run(self):
        orig = R._tick_merging
        state, req, run = None, None, None
        self.last_fresh = None       # (road_offset, hop, fid) 上一份新鲜读数
        prev_routed = None
        holder = self

        def patched(s, by, p, clock, epoch):
            st, v = orig(s, by, p, clock, epoch)
            if v.verdict == "finish" and not holder.ctx["allow_finish"]:
                # 保留状态机其余语义（撤销优先、超时照常）：
                # 把位置容差调到不可达，再走一遍同一函数。
                return orig(s, by, replace(p, finish_tol_lane=-1.0),
                            clock, epoch)
            return st, v

        R._tick_merging = patched
        try:
            for idx, row in enumerate(self.rows):
                if row.get("run_id") != run:
                    run = row.get("run_id")
                    state, req, prev_routed = None, None, None
                    self.last_fresh = None
                ro = row.get("road_offset")
                if fresh(row) and ro is not None:
                    # 谱系随读数更新（含非巡航拍），与生产标注同一序列
                    self.last_fresh = (ro, row.get("ro_hop"),
                                       row.get("fid"))
                if row.get("route") is None:
                    state, req, prev_routed = None, None, None
                    continue
                if row.get("state") != "CRUISE":
                    continue
                rep = report_of(row)
                ep = route_of(row).get("ep", 0)
                dt = row.get("dt") or 0.0
                use_req = None if rep is None else req
                cur_ego = coarse_ego(row)
                self.ctx["allow_finish"] = self._gate(row, prev_routed,
                                                      cur_ego)
                state, v = R.tick_route(state, rep, self.p, dt_s=dt,
                                        epoch=ep, request=use_req)
                self.ticks.append((idx, v))
                req = None if rep is None else (
                    row.get("sel_dbg") or {}).get("req")
                self.lineage.append((idx, self.last_fresh))
                prev_routed = row
        finally:
            R._tick_merging = orig
        return self

    # ------------------------------------------------------------ 段

    def segments(self):
        """[(start_tick_i, end_tick_i|None, start_verdict, end_verdict)]。"""
        out = []
        open_i = None
        for i, (_, v) in enumerate(self.ticks):
            if v.verdict == "merge_start" and open_i is None:
                open_i = i
            elif open_i is not None and v.phase != R.PHASE_MERGING:
                out.append((open_i, i, self.ticks[open_i][1], v))
                open_i = None
        if open_i is not None:
            out.append((open_i, None, self.ticks[open_i][1], None))
        return out

    def finishes(self):
        return [(i, v) for i, (_, v) in enumerate(self.ticks)
                if v.verdict == "finish"]


# ---------------------------------------------------------------- 分析

def fidelity(replay: Replay, rows) -> list:
    """A 版必须逐拍复现生产 route 列；返回不符清单（最多留 20 条）。"""
    bad = []
    for idx, v in replay.ticks:
        got = v.as_trace()
        want = route_of(rows[idx])
        for k in ("ph", "vd", "why", "ref", "tgt", "ep"):
            a, b = got.get(k), want.get(k)
            if k in ("ref", "tgt") and a is not None and b is not None \
                    and abs(a - b) < 1e-3:
                continue
            if a != b:
                bad.append({"fid": rows[idx].get("fid"), "key": k,
                            "replay": a, "trace": b})
                break
    return bad


def tick_input(rows, idx) -> dict:
    r = rows[idx]
    return {"fid": r.get("fid"), "state": r.get("state"),
            "dt": r.get("dt"), "ego": coarse_ego(r),
            "ro": r.get("road_offset"), "hold": r.get("ro_hold"),
            "new": r.get("dgeo_new"), "src": r.get("ro_source"),
            "hop": r.get("ro_hop"), "pos_class": pos_class(r),
            "exec": r.get("executed_lane"), "v_lat": r.get("v_lat_est"),
            "route": route_of(r)}


def segment_inputs(rows, replay: Replay, start_i, end_i) -> list:
    lo = max(0, start_i - 1)
    hi = min(len(replay.ticks), end_i + 2)
    return [tick_input(rows, replay.ticks[k][0]) for k in range(lo, hi)]


def outcome(v) -> str:
    return "unended" if v is None else v.verdict


def first_divergence(a: Replay, b: Replay, start_i: int):
    for k in range(start_i, min(len(a.ticks), len(b.ticks))):
        if a.ticks[k][0] != b.ticks[k][0]:
            return k, "tick_misaligned"
        va, vb = a.ticks[k][1], b.ticks[k][1]
        if (va.phase, va.verdict, va.why, va.lane_ref,
                va.lane_target) != (vb.phase, vb.verdict, vb.why,
                                    vb.lane_ref, vb.lane_target):
            return k, vb.verdict
    return None, None


def delay_ticks(a, b, start_i, a_end, b_end) -> int | None:
    if a_end is None or b_end is None:
        return None
    return b_end - a_end


def analyse(rows, params):
    rp = {k: Replay(rows, params, k).run()
          for k in ("A", "B", "C", "D", "S")}
    bad = fidelity(rp["A"], rows)
    segs = {k: rp[k].segments() for k in rp}
    fins = {k: rp[k].finishes() for k in rp}

    # A 的每个 finish 段 → B/C/D 的对应段与结局
    detail = []
    for si, ei, sv, ev in segs["A"]:
        if ev is None or ev.verdict != "finish":
            continue
        end_row = rows[rp["A"].ticks[ei][0]]
        prev_row = rows[rp["A"].ticks[ei - 1][0]] if ei > 0 else None
        cur_ego = coarse_ego(end_row)
        prev_ego = coarse_ego(prev_row) if prev_row is not None else None
        dt = end_row.get("dt")
        v_est = (None if cur_ego is None or prev_ego is None or not dt
                 else (cur_ego - prev_ego) / dt)
        v_class = ("measured" if prev_row is not None and fresh(prev_row)
                   and fresh(end_row) else "hold_diff")
        lin = rp["A"].lineage[ei][1] if ei < len(rp["A"].lineage) else None
        if fresh(end_row):
            pos_hop = end_row.get("ro_hop")
        elif lin is not None and end_row.get("road_offset") is not None \
                and abs(end_row["road_offset"] - lin[0]) <= 1e-3:
            pos_hop = lin[1]
        else:
            pos_hop = None
        item = {
            "start_fid": rows[rp["A"].ticks[si][0]].get("fid"),
            "end_fid": end_row.get("fid"),
            "target": sv.lane_target, "start_ego": coarse_ego(
                rows[rp["A"].ticks[si][0]]),
            "end_ego": cur_ego, "end_pos_class": pos_class(end_row),
            "end_v_est": None if v_est is None else round(v_est, 3),
            "end_v_class": v_class, "pos_hop": pos_hop,
            "end_hold": end_row.get("ro_hold"),
            "end_src": end_row.get("ro_source"),
            "variants": {},
            "inputs": segment_inputs(rows, rp["A"], si, ei),
        }
        for k in ("B", "C", "D", "S"):
            hit = next((s for s in segs[k] if s[0] == si), None)
            if hit is None:
                div, kind = first_divergence(rp["A"], rp[k], si)
                item["variants"][k] = {
                    "same_segment": False,
                    "first_divergence_fid": None if div is None else
                    rows[rp["A"].ticks[div][0]].get("fid"),
                    "first_divergence_kind": kind,
                    "outcome": "undetermined(segment_missing)",
                    "delay_ticks": None}
                continue
            _, hei, _, hev = hit
            div, kind = first_divergence(rp["A"], rp[k], si)
            oc = outcome(hev)
            ok = bool(hev is not None and hev.verdict == "finish")
            d = delay_ticks(rp["A"], rp[k], si, ei, hei)
            item["variants"][k] = {
                "same_segment": True,
                "first_divergence_fid": None if div is None else
                rows[rp["A"].ticks[div][0]].get("fid"),
                "first_divergence_kind": kind,
                "outcome": oc,
                "end_fid": None if hei is None else
                rows[rp[k].ticks[hei][0]].get("fid"),
                "delay_ticks": d,
                "finish_later": ok,
                "new_finish_v_class": (
                    None if not ok else (
                        "measured" if hei > 0
                        and fresh(rows[rp[k].ticks[hei][0]])
                        and fresh(rows[rp[k].ticks[hei - 1][0]])
                        else "hold_diff")),
            }
        detail.append(item)

    # 全局结局变化
    counts = {}
    for k in ("A", "B", "C", "D", "S"):
        c = {}
        for _, v in rp[k].ticks:
            c[v.verdict] = c.get(v.verdict, 0) + 1
        counts[k] = c
    a_fin = {rows[rp["A"].ticks[i][0]].get("fid") for i, _ in fins["A"]}
    global_change = {}
    for k in ("B", "C", "D", "S"):
        kf = {rows[rp[k].ticks[i][0]].get("fid") for i, _ in fins[k]}
        global_change[k] = {
            "finish_lost": sorted(a_fin - kf),
            "finish_new": sorted(kf - a_fin),
            "n_finish": len(kf),
        }
    return {"replays": rp, "fidelity": bad, "detail": detail,
            "counts": counts, "global": global_change}


# ---------------------------------------------------------------- 输出

def render(sessions, res, meta) -> str:
    L = ["# 完成判据证据门的离线反事实（A/B/C/D + 敏感性 S）", ""]
    L.append(f"- 代码版本：`{meta['commit']}`；探针 sha256：`{meta['probe_sha']}`")
    L.append(f"- 决策真源：`{meta['decision']}`")
    L.append(f"- 参数：finish_tol={meta['params'].finish_tol_lane} 道、"
             f"finish_v_tol={meta['params'].finish_v_tol_lane_s} 道/s、"
             f"merge_max={meta['params'].merge_max_s} s")
    for sess, r in zip(sessions, res):
        L += ["", f"## {sess}", ""]
        L.append(f"- 回放拍数 {len(r['replays']['A'].ticks)}；"
                 f"A 对生产 trace 不符 {len(r['fidelity'])} 处"
                 + ("（回放不可作反事实）" if r["fidelity"] else "（逐拍一致）"))
        L.append("- 结局计数：" + "；".join(
            f"{k}: finish {r['counts'][k].get('finish', 0)} / "
            f"cancel {r['counts'][k].get('cancel', 0)} / "
            f"rebuild {r['counts'][k].get('rebuild', 0)} / "
            f"timeout {r['counts'][k].get('timeout', 0)} / "
            f"epoch {r['counts'][k].get('epoch_reset', 0)}"
            for k in ("A", "B", "C", "D", "S")))
        for k in ("B", "C", "D", "S"):
            g = r["global"][k]
            L.append(f"- {k}：finish {g['n_finish']}，丢失 {len(g['finish_lost'])}"
                     f"、新增 {len(g['finish_new'])}"
                     + (f"（丢失 fid {g['finish_lost']}，新增 fid {g['finish_new']}）"
                        if g["finish_lost"] or g["finish_new"] else ""))
        L += ["", "| 起 | 止(A) | 目标 | 终点距目标 | pos/v 类 | hop | B | C | D | S |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for it in r["detail"]:
            tgt = it["target"]
            dist = (None if it["end_ego"] is None or tgt is None
                    else round(abs(it["end_ego"] - tgt), 2))
            cells = []
            for k in ("B", "C", "D", "S"):
                v = it["variants"][k]
                if not v["same_segment"]:
                    cells.append(f"段缺失（分歧 {v['first_divergence_fid']}）")
                elif v["finish_later"]:
                    d = v["delay_ticks"]
                    cells.append(f"finish{'' if d is None else f' +{d}拍'}"
                                 f"（{v['new_finish_v_class']}）")
                else:
                    cells.append(f"{v['outcome']}@{v['end_fid']}"
                                 f"（分歧 {v['first_divergence_fid']}）")
            L.append(f"| {it['start_fid']} | {it['end_fid']} | {tgt} | {dist} | "
                     f"{it['end_pos_class']}/{it['end_v_class']} | "
                     f"{it['pos_hop']} | {cells[0]} | {cells[1]} | "
                     f"{cells[2]} | {cells[3]} |")
    # 合计（跨会话）
    L += ["", "## 合计", ""]
    for k in ("B", "C", "D", "S"):
        same = delayed = other = missing = 0
        measured = 0
        changed = []
        for sess, r in zip(sessions, res):
            for it in r["detail"]:
                v = it["variants"][k]
                if not v["same_segment"]:
                    missing += 1
                    continue
                if v["finish_later"]:
                    if v.get("delay_ticks") == 0:
                        same += 1
                    else:
                        delayed += 1
                    if v.get("new_finish_v_class") == "measured":
                        measured += 1
                else:
                    other += 1
                    changed.append(f"{sess}:{it['start_fid']}->{v['outcome']}"
                                   f"@{v['end_fid']}")
        L.append(f"- {k}：原样完成 {same}、延后完成 {delayed}"
                 f"（其中实测速度 {measured}）、改成其他结局 {other}、"
                 f"段缺失/不可确定 {missing}")
        if changed:
            L.append(f"  - 改变结局的段：{'；'.join(changed)}")
    L += ["", "## 口径", "",
          "- B = 只拒「保持值商 0」（读数没更新且差商恰为 0）。S 是敏感性对照："
          "要求两端都新鲜（与完成判定器的 `measured` 同义），不属于 A/B/C/D 矩阵。",
          "- C = 位置项拒绝由 hop=1（换档）新鲜读数确定的到达位置；hold 值"
          "只有在其等于上次 hop=1 新鲜读数时才一并拒绝。hop 标的是**读数差**"
          "的性质，不是读数本身不可用。",
          "- 变体只拦 `finish`；撤销优先与超时语义原样保留（拦住后按同一函数"
          "重算，超时/撤销照常发生）。",
          "- 没有对应观测的新结局记「不可确定」，不计作完成，也不补造。"]
    return "\n".join(L)


def _inside(path: str, root: str) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def _commit(root: str | None, override: str) -> str:
    if override:
        return override
    if not root:
        return "unknown"
    try:
        return subprocess.check_output(
            ["git", "-C", root, "rev-parse", "--short=7", "HEAD"],
            text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def discover(logs: str) -> list:
    out = []
    if not os.path.isdir(logs):
        return out
    for name in sorted(os.listdir(logs)):
        p = os.path.join(logs, name, "trace", "speedrush.jsonl")
        if os.path.exists(p):
            out.append(name)
    return out


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="完成判据证据门 A/B/C/D 反事实重放")
    ap.add_argument("--logs", default=_LOG_DIR_DEFAULT)
    ap.add_argument("--session", default="", help="会话 token，逗号分隔")
    ap.add_argument("--commit", default="", help="覆盖代码版本号")
    ap.add_argument("--out", default="", help="报告输出（仓库外）")
    ap.add_argument("--baseline-out", default="",
                    help="冻结基线 JSON（仓库外；默认写到 logs/_baselines）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.logs):
        print(f"日志目录不存在：{args.logs}")
        return 2
    sessions = ([s for s in args.session.split(",") if s]
                if args.session else discover(args.logs))
    if not sessions:
        print(f"{args.logs} 下没有带 trace 的会话")
        return 2

    cfg = load_decision()
    params = params_from(cfg)
    probe_sha = hashlib.sha256(open(__file__, "rb").read()).hexdigest()[:16]
    commit = _commit(_ROOT, args.commit)

    results = []
    for sess in sessions:
        p = os.path.join(args.logs, sess, "trace", "speedrush.jsonl")
        if not os.path.exists(p):
            print(f"缺 trace：{p}")
            return 2
        rows = [json.loads(x) for x in open(p, encoding="utf-8")]
        results.append(analyse(rows, params))

    meta = {"commit": commit, "probe_sha": probe_sha,
            "decision": str(getattr(cfg, "path", "decision.json")),
            "params": params}
    text = render(sessions, results, meta)
    if args.out:
        out = os.path.abspath(args.out)
        if _ROOT and _inside(out, _ROOT):
            print(f"拒绝写入仓库内路径：{out}")
            return 2
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入仓库外文件：{out}")
    else:
        print(text)

    # 冻结基线：逐段判定输入 + 版本信息（仓库外）
    base = os.path.abspath(args.baseline_out) if args.baseline_out else \
        os.path.join(args.logs, "_baselines",
                     f"finish_gate_{commit}_{probe_sha}.json")
    if _ROOT and _inside(base, _ROOT):
        print(f"拒绝写入仓库内路径：{base}")
        return 2
    os.makedirs(os.path.dirname(base), exist_ok=True)
    with open(base, "w", encoding="utf-8") as fh:
        json.dump({"commit": commit, "probe_sha": probe_sha,
                   "params": params.__dict__,
                   "sessions": {
                       s: {"fidelity_mismatch": len(r["fidelity"]),
                           "finishes": r["detail"]}
                       for s, r in zip(sessions, results)}},
                  fh, ensure_ascii=False, indent=1)
    print(f"基线已冻结：{base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
