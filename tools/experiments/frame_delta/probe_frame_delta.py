# -*- coding: utf-8 -*-
"""路心参考系只读探针：大差窗口的来源分账与代价（frame_delta/1）。

**只读**：不 import 控制链、不改生产数据；只读 trace 与 policy JSON。

## 口径（复算必须按此）

1. **帧差** `fd`（米）：优先取 `plan.fd`（生产同一量）；无计划但有场景时用
   `scene.ego_m - executed_lane*lane_w` 重建；缺任一侧记未知。
2. **大差**：`|fd| > hold_m`，默认读 policy 的 `motion.frame_delta_hold_m`。
3. **窗口**：连续大差拍；其间夹 **1 拍**非大差记为桥拍并入（桥拍不改变起止
   大差拍）；夹 ≥2 拍断开；`fd` 未知拍**断开并单列**，不冒充非大差拍。
4. **来源**：`ro_source`（wall / wall_occl / region / memory / …）；
   `dgeo_new`=本拍是否有新深度；`ro_hold`=planner 是否按历史保持消费。
   窗口同时记**首拍来源**、**窗口内主导来源**与**切换史**，不得只按结束来源
   归类（来源切换本身是接口边界证据）。
5. **计划**：`none`=未生成；`veto`=生成但被否（按 reason）；`valid`=有效。
   「未生成」与「生成后被否」分开计数。
6. **线观测只作辅助**：`lane_fuse.trusted` 且 `sm` 非空时比较
   `|road-sm|` 与 `|exec-sm|`；线也参与 executed 的融合，不是独立裁判。

## 映射核验口径（重基前后参考系，frame_delta/1 第二批）

重基把 `executed` 一步改到 `road_offset`。要判断「这次跳变是参考系重表达
还是物理横移」，必须把两样东西分开看：

1. **尺子有没有动**：`Δro = ro(i) − ro(i−1)`。尺子（路心观测）自己没动
   （|Δro| ≤ 0.4 道，即供数链自己的跳变门容限）而 executed 跳超半道，说明
   是**状态追平**，参考系没换，道号不变。换纪触发 ① 的场景分带中心按下一拍
   比较，表里 `Δc0 = c0(i+1) − c0(i)`。
2. **物理动没动**：用**自车系原始观测**做证人——`lane_obs.lines[].L`（线的
   横向偏移，不挂在深度锚点上）与 `dgeo_left/right`（深度缘，原点=车）。
   线观测约 10Hz，两拍间可含 ~0.2s 真实横移，所以绝对上限放到 0.35 道；
   更关键的是比例：证人位移不到尺子位移的 60% 即记「不是车动的」。跨拍按
   同色同侧 + `L` 差最小配对，配不上记无证人。
3. **新中心是否被证人复核**：同拍 `lane_obs.x`（锚定横位，量化 ±0.5 道）
   或深度缘中心（双侧齐）与 `post` 的差。锚定值不是独立真值，只作弱复核。

据此给每次重基一个映射结论（只定性、不判控制对错）：

- `恒等（参考系未动）`：尺子没动而 executed 跳超半道 → 状态追平，旧道号
  仍指同一条物理车道，无需映射。物理证人 + 同拍复核两项齐全记「双证」，
  缺一项记「单证」，两项都缺记「无证」——只说明证据量；线值与深度中心同源，
  双证也不等于「新中心是绝对正确的」。
- `无换纪`：`|Δexec| ≤ 0.5`，换纪门本就不会动。
- `平移-确认`：尺子动了（|Δro| ≥ 0.5）+ 物理证人无位移 + Δro 落在整数道
  ±0.25 + 新值被复核 → 同一物理车道在新参考系下为 `ref + round(Δro)`；
  同时报平移残差（Δro 与整数道的差）。
- `平移-未确认`：尺子动了，但位移非整数道、证人缺失或与 Δro 矛盾 → 不能
  认定旧目标等价于新编号下的哪一条道。
- `感知跳变-无法判定`：证人位移与尺子位移同量级——一拍内不可能有这种
  物理横移，两路一起跳，分不清车动还是感知跳。
- `陈旧可疑`：尺子动且新源是保持/记忆，证人位移与 Δro 明显不符 → 新中心
  本身可疑。

「原换纪门是否清空了仍有效的承诺」只回答**机制问题**：尺子没动的子集里，
下一拍是否 `ep_up + vd=epoch_reset`，旧 `ref` 是否仍在下一拍候选道表里，
以及旧目标相对车是否翻到另一侧（两端都离道 >0.3 道才算翻侧）。

## 输出

- 覆盖表：每局 tick / 有帧差 / 大差 / 窗口 / 未生成计划 / 未知帧差。
- 来源表：每个路心来源的拍数、新深度比例、planner 接纳比例、大差拍、窗口
  首拍数、窗口主导数、窗口长度。
- 代价表：按窗口首拍来源分账——计划否决、并线中、10 拍内并线结局、目标变化、
  换道请求、线仲裁、窗口前是否紧跟来源切换/换档/重基/换纪。
- 并线期重基映射核验：逐次给出 Δexec/Δro/Δc0、物理证人、新值复核、证据
  等级与映射结论（恒等 / 平移-确认 / 平移-未确认 / 陈旧可疑 / 无换纪），
  以及换纪门实际触发与旧目标道是否仍在候选表。
- 米制目标与消费时序：逐次给出 e_y（目标−实测 ego）前后变化、旧目标道占用、
  合并距离（merge_start→修正后）、下拍计划有效性，并给出「评审候选」
  （恒等 ∩ 米制通过）。消费时序按源码顺序记录在报告头部。
- 自测：`--selftest`（窗口合并、未知拍断开、来源分类、目标变化、映射结论、
  米制通过/占用/计划/越过/前提变化）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_HOLD_M = 1.0
ENDINGS = ("finish", "cancel", "timeout", "rebuild", "epoch_reset")


# ---------- 读取与分类 ----------

def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_hold_m(policy: Path | None) -> float:
    policy = policy or (repo_root() / "maaracing_master" / "plugins"
                        / "speedrush" / "resources" / "policy"
                        / "decision.json")
    try:
        d = json.loads(policy.read_text(encoding="utf-8"))
        return float(d["motion"]["frame_delta_hold_m"])
    except Exception as exc:  # noqa: BLE001 —— 缺 policy 用默认并显式说明
        print(f"[probe] 读 policy 失败（{exc!r}），hold_m={DEFAULT_HOLD_M}",
              file=sys.stderr)
        return DEFAULT_HOLD_M


def discover(root: Path) -> list[tuple[str, Path]]:
    """logs/<token>/trace/speedrush.jsonl → [(token, path)]。"""
    out = []
    for p in sorted(root.glob("*/trace/speedrush.jsonl")):
        out.append((p.parent.parent.name, p))
    return out


def load_runs(path: Path) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or '"type"' in line:
                continue
            r = json.loads(line)
            by[r.get("run_id") or path.parent.parent.name].append(r)
    return dict(by)


def _hop(r: dict) -> int | None:
    h = (r.get("attribution") or {}).get("hop")
    return h if isinstance(h, int) else None


def tick_info(r: dict, hold_m: float) -> dict:
    """一拍的可复算分类（字段缺失诚实 None）。"""
    scene = r.get("scene") or {}
    lw = scene.get("lane_w") or 3.38
    plan = r.get("plan") or {}
    fd = plan.get("fd")
    rebuilt = False
    if fd is None and scene.get("ego_m") is not None \
            and r.get("executed_lane") is not None:
        fd = float(scene["ego_m"]) - float(r["executed_lane"]) * lw
        rebuilt = True
    if plan:
        pstate = "valid" if plan.get("valid") else "veto"
        preason = plan.get("reason")
    else:
        pstate, preason = "none", None
    rt = r.get("route") or {}
    lf = r.get("lane_fuse") or {}
    dbg = r.get("sel_dbg") or {}
    lo = r.get("lane_obs") or {}
    src = r.get("ro_source") or "none"
    new = bool(r.get("dgeo_new"))
    hold = bool(r.get("ro_hold"))
    co = r.get("coarse") or {}
    cand = None if not co.get("cand") else [
        (float(c[0]), c[1], c[2], c[3]) for c in co["cand"]
        if isinstance(c, (list, tuple)) and len(c) >= 4]
    return {
        "fid": r.get("fid"),
        "stage": r.get("stage_id"),
        "fd": None if fd is None else float(fd),
        "fd_rebuilt": rebuilt,
        "big": fd is not None and abs(float(fd)) > hold_m,
        "plan": pstate,
        "preason": preason,
        "src": src,
        "new": new,
        "hold": hold,
        "line": (lf.get("sm") if lf.get("trusted") else None),
        "line_x": lo.get("x"),
        "line_state": lo.get("state"),
        "lines": [(c.get("color"), c.get("side"), c.get("L"), c.get("n"))
                  for c in (lo.get("lines") or [])],
        "w": r.get("ro_w"),
        "edge": r.get("ro_edge"),
        "cand": cand,
        "xt": r.get("x_target"),
        "goal": r.get("goal"),
        "plan_valid": (r.get("plan") or {}).get("valid"),
        "plan_reason": (r.get("plan") or {}).get("reason"),
        "plan_fd": (r.get("plan") or {}).get("fd"),
        "lw": scene.get("lane_w") or 3.38,
        "road": r.get("road_offset"),
        "exec": r.get("executed_lane"),
        "ph": rt.get("ph"),
        "vd": rt.get("vd"),
        "why": rt.get("why"),
        "target": dbg.get("target"),
        "c0": dbg.get("c0"),
        "req": dbg.get("req"),
        "layer": r.get("sel_layer"),
        "ref": rt.get("ref"),
        "tgt": rt.get("tgt"),
        "intent": r.get("intent_lane"),
        "hop": _hop(r),
        "rebase": (r.get("lat_ctrl") or {}).get("rebase"),
        "pre_exec": (r.get("plan_ego") or [None])[0],
        "ep": rt.get("ep"),
        "raw": r,
    }


def build_windows(ticks: list[dict]) -> list[dict]:
    """按口径 3 合并大差拍（允许 1 拍桥；未知拍断开）。

    先取大差拍下标，再按「相邻大差拍间最多 1 拍非大差、且其间无未知帧差」归组，
    窗口范围取首末大差拍（桥拍自然包含）；尾随的非大差拍不算窗口。
    """
    idxs = [i for i, t in enumerate(ticks) if t["big"]]
    groups: list[list[int]] = []
    cur: list[int] = []
    for i in idxs:
        if cur:
            between = ticks[cur[-1] + 1:i]
            if i - cur[-1] > 2 or any(t["fd"] is None for t in between):
                groups.append(cur)
                cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    windows = []
    for gi, g in enumerate(groups):
        w = ticks[g[0]:g[-1] + 1]
        big = [ticks[i] for i in g]
        src_counts = Counter(t["src"] for t in w if t["src"] != "none")
        dom = src_counts.most_common(1)[0][0] if src_counts else "none"
        switches = []
        prev = None
        for t in w:
            key = (t["src"], t["new"], t["hold"])
            if prev is not None and key != prev:
                switches.append({"fid": t["fid"], "src": t["src"],
                                 "new": t["new"], "hold": t["hold"]})
            prev = key
        fd0 = big[0]["fd"]
        fd1 = big[-1]["fd"]
        fmax = max(abs(t["fd"]) for t in big)
        grow = abs(fd1) - abs(fd0)
        if len(big) <= 2:
            shape = "sudden"
        elif grow > 0.5:
            shape = "growing"
        elif len(w) >= 5:
            shape = "sustained"
        else:
            shape = "mixed"
        windows.append({
            "idx": gi,
            "start": big[0]["fid"], "end": big[-1]["fid"],
            "start_i": g[0], "end_i": g[-1],
            "n_ticks": len(w), "n_big": len(big),
            "start_src": big[0]["src"], "dom_src": dom,
            "start_new": big[0]["new"], "start_hold": big[0]["hold"],
            "start_hop": big[0]["hop"], "start_rebase": big[0]["rebase"],
            "fd_start": round(fd0, 2), "fd_end": round(fd1, 2),
            "fd_max": round(fmax, 2), "shape": shape,
            **_freeze_info(w),
            "switches": switches,
            "veto": sum(1 for t in w if t["plan"] == "veto"),
            "noplan": sum(1 for t in w if t["plan"] == "none"),
            "merge": any(t["ph"] == "merging" for t in w),
            "req": any(t["req"] is not None for t in w),
            "target_changed": _target_changed(w),
            "ref_changed": _range_gt(w, "ref", 0.1),
            "layer_changed": _distinct_gt(w, "layer", 1),
            "line": _line_tally(big),
            "ticks": w,
        })
    return windows


def _target_changed(w: list[dict]) -> bool:
    vals = [t["target"] for t in w if t["target"] is not None]
    if len(vals) < 2:
        return False
    return max(vals) - min(vals) > 0.1


def _freeze_info(w: list[dict]) -> dict:
    """路心保持冻结：窗口内同一 ro 的最长连续拍 + 执行位位移。"""
    best = 0
    run = 0
    prev = None
    for t in w:
        ro = t["road"]
        if ro is None:
            run = 0
            prev = None
            continue
        if prev is not None and abs(float(ro) - prev) <= 1e-3:
            run += 1
        else:
            run = 1
        prev = float(ro)
        best = max(best, run)
    execs = [float(t["exec"]) for t in w if t["exec"] is not None]
    move = (max(execs) - min(execs)) if len(execs) >= 2 else 0.0
    return {"frozen_run": best, "exec_move": round(move, 2),
            "frozen": best >= 3 and move > 0.3}


def _range_gt(w: list[dict], key: str, tol: float) -> bool:
    vals = [t[key] for t in w if t.get(key) is not None]
    return bool(vals) and (max(vals) - min(vals)) > tol


def _distinct_gt(w: list[dict], key: str, tol: int) -> bool:
    vals = {t[key] for t in w if t.get(key) is not None}
    return len(vals) > tol


def _line_tally(big: list[dict]) -> dict:
    c = Counter()
    for t in big:
        if t["line"] is None or t["road"] is None or t["exec"] is None:
            c["none"] += 1
            continue
        a = abs(float(t["road"]) - float(t["line"]))
        b = abs(float(t["exec"]) - float(t["line"]))
        c["road" if a < b else "exec" if b < a else "tie"] += 1
    return dict(c)


def _preceded(ticks: list[dict], start_idx: int, window_start_fid) -> dict:
    """窗口首拍是否紧跟来源切换/状态切换/换档/重基（2 拍内）或换纪（5 拍内）。"""
    i = start_idx
    out = {"src_switch": False, "state_switch": False,
           "hop": False, "rebase": False, "epoch": False}
    if i > 0:
        a = ticks[i - 1]
        b = ticks[i]
        out["src_switch"] = a["src"] != b["src"]
        out["state_switch"] = (a["new"], a["hold"]) != (b["new"], b["hold"])
    for j in range(max(0, i - 2), i + 1):
        if ticks[j]["hop"] == 1:
            out["hop"] = True
        if ticks[j]["rebase"]:
            out["rebase"] = True
    for j in range(max(0, i - 5), i + 1):
        if ticks[j]["vd"] in ("epoch_reset", "rebuild"):
            out["epoch"] = True
    return out

def grade_rebase(ev: dict) -> tuple[str, str]:
    """按证据等级给一次重基定性（只回答「输入是否陈旧」，不判重基对错）。

    A 已确认陈旧：输入按保持消费、同一路心值已冻结 ≥3 拍、执行位离它 >0.3 道，
      且重基把执行位采用到该值（adopt 差 <0.2 道）——数值层面「陈旧中心被采信」。
    B 疑似陈旧：保持/冻结/偏离只满足一部分，或映射未完全采用旧值。
    C 无法判定：新鲜源（可能是真实参考系跳变）、非保持、或缺字段。
    """
    if ev["pre_exec"] is None or ev["road"] is None:
        return "C 无法判定", "缺字段"
    if not ev["hold"] and ev["new"]:
        return "C 无法判定", "新鲜源，可能是真实跳变"
    if not ev["hold"]:
        return "C 无法判定", "非保持状态"
    if ev["frozen_age"] >= 3 and ev["exec_ro_gap"] > 0.3             and ev["adopt_gap"] < 0.2:
        return "A 已确认陈旧", "冻结+执行位偏离+重基采用旧值"
    if ev["frozen_age"] >= 2 or ev["exec_ro_gap"] > 0.5:
        return "B 疑似陈旧", "保持/冻结支持，但映射或独立证据不足"
    return "C 无法判定", "保持但冻结/偏离不足"


# ---------- 重基坐标映射核验 ----------

RULER_STABLE_LANE = 0.4     # 供数链 JUMP_TOL_LANE：尺子自己的跳变门
PHYS_MOVE_MAX_LANE = 0.35   # 线观测 ~10Hz：两拍间可含 ~0.2s 真实横移
PHYS_EXPLAIN_FRAC = 0.6     # 证人位移达到尺子位移的此比例 = 由运动解释
LINE_REVIEW_LANE = 0.5      # 锚定线值量化容限
SHIFT_INT_TOL = 0.25        # 平移量离整数道的容限
WITNESS_MATCH_LANE = 0.6    # 同一条线跨拍配对的最大位移


def _match_line(a: dict, b: dict):
    """跨拍配同一条线：同色同侧、L 差 ≤ 0.6；多条取最老（n 最大）。

    返回 (color, side, dL, n_pre, n_now)；配不上 None。L 是自车系物理量，
    不随深度锚点重新编号。"""
    best = None
    for c0, s0, l0, _n0 in a.get("lines") or []:
        if l0 is None:
            continue
        for c1, s1, l1, n1 in b.get("lines") or []:
            if l1 is None or c0 != c1 or s0 != s1:
                continue
            d = float(l1) - float(l0)
            if abs(d) > WITNESS_MATCH_LANE:
                continue
            cand = (int(n1 or 0), c0, s0, d, _n0, n1)
            if best is None or cand[0] > best[0]:
                best = cand
    return None if best is None else best[1:]


def _edge_move(a: dict, b: dict) -> list[tuple[str, float]]:
    """深度缘跨拍位移（同侧字段配对）。"""
    out = []
    for k in ("left", "right"):
        x, y = a.get(k), b.get(k)
        if x is not None and y is not None:
            out.append((k, float(y) - float(x)))
    return out


def _phys_move(a: dict, b: dict) -> tuple[float | None, list[str]]:
    """物理横移证人：正=自车向右。线/缘都在自车系，右移 → 相对读数减小。"""
    vals, srcs = [], []
    m = _match_line(a, b)
    if m is not None:
        vals.append(-m[2])
        srcs.append("line")
    edges = _edge_move(a, b)
    if edges:
        vals.append(-sum(d for _, d in edges) / len(edges))
        srcs.append("edge" + str(len(edges)))
    return (sum(vals) / len(vals) if vals else None), srcs


def mapping_event(ticks: list[dict], i: int) -> dict:
    """一次重基的参考系映射核验（只读，结论分级见模块 docstring）。"""
    t = ticks[i]
    a = ticks[i - 1] if i > 0 else {}
    nxt = ticks[i + 1] if i + 1 < len(ticks) else None
    pre, post = t["pre_exec"], t["exec"]
    dx = None if pre is None or post is None else float(post) - float(pre)
    ro_pre, ro_post = a.get("road"), t["road"]
    d_ro = (None if ro_pre is None or ro_post is None
            else float(ro_post) - float(ro_pre))
    move, msrc = _phys_move(a, t)
    line_x = t.get("line_x")
    line_gap = (None if line_x is None or post is None
                else abs(float(post) - float(line_x)))
    edge = t.get("edge")
    edge_gap = (None if edge is None or post is None
                else abs(float(post) - float(edge)))
    w_pre, w_post = a.get("w"), t.get("w")
    d_w = (None if w_pre is None or w_post is None
           else float(w_post) - float(w_pre))
    c0_now = t.get("c0")
    c0_next = nxt.get("c0") if nxt is not None else None
    # 换纪在下一拍比较 c0：触发 ① 的输入是 c0(i+1) − c0(i)
    d_c0 = (None if c0_now is None or c0_next is None
            else float(c0_next) - float(c0_now))
    ref = t.get("ref")
    dist_pre = (None if ref is None or ro_pre is None
                else float(ref) - float(ro_pre))
    dist_post = (None if ref is None or ro_post is None
                 else float(ref) - float(ro_post))
    # 旧目标相对车的位置是否翻到另一侧（两端都离道 >0.3 才算；中间算已到达）
    side_flip = (dist_pre is not None and dist_post is not None
                 and abs(dist_pre) >= 0.3 and abs(dist_post) >= 0.3
                 and (dist_pre > 0) != (dist_post > 0))
    if d_ro is None:
        ruler = "unknown"
    elif abs(d_ro) <= RULER_STABLE_LANE:
        ruler = "stable"
    else:
        ruler = "moved"
    big = dx is not None and abs(dx) > 0.5
    explained = (move is not None and d_ro is not None
                 and abs(move) >= PHYS_EXPLAIN_FRAC * abs(d_ro))
    phys_quiet = (move is not None and abs(move) <= PHYS_MOVE_MAX_LANE
                  and not explained)
    reviewed = ((line_gap is not None and line_gap <= LINE_REVIEW_LANE)
                or (edge_gap is not None and edge_gap <= 0.35))
    mapped = None
    resid = None
    if ruler == "stable":
        if big:
            verdict = "恒等（参考系未动）"
        else:
            verdict = "无换纪"
        mapped = ref
    elif ruler == "moved":
        resid = float(d_ro) - round(float(d_ro))
        integer = abs(resid) <= SHIFT_INT_TOL
        if t["hold"] or t["src"] in ("memory", "wall_occl"):
            verdict = "陈旧可疑"
        elif phys_quiet and reviewed and integer:
            verdict = "平移-确认"
            mapped = None if ref is None else float(ref) + round(float(d_ro))
        elif explained:
            verdict = "感知跳变-无法判定"
        else:
            verdict = "平移-未确认"
    else:
        verdict = "无法判定"
    ep_up = bool(nxt is not None and nxt.get("ep") is not None
                 and t.get("ep") is not None and nxt["ep"] > t["ep"])
    remerge = None
    for j in range(i + 1, min(len(ticks), i + 11)):
        if ticks[j].get("vd") == "merge_start":
            remerge = ticks[j].get("ref")
            break
    ref_in_cand = None
    if ref is not None and nxt is not None and nxt.get("cand"):
        ref_in_cand = any(abs(float(c[0]) - float(ref)) < 0.25
                          for c in nxt["cand"])
    mapped_in_cand = None
    if mapped is not None and nxt is not None and nxt.get("cand"):
        mapped_in_cand = any(abs(float(c[0]) - float(mapped)) < 0.25
                             for c in nxt["cand"])
    # 证据量：物理证人 + 同拍线/缘复核；「双证」≠ 真值已证（线值与深度中心同源）
    evidence = ("双证" if move is not None and reviewed
                else "单证" if move is not None or reviewed else "无证")
    return {
        "fid": t["fid"], "kind": t["rebase"], "ref": ref,
        "pre": pre, "post": post, "dx": dx,
        "ro_pre": ro_pre, "ro_post": ro_post, "d_ro": d_ro,
        "d_c0": d_c0,
        "dist_pre": dist_pre, "dist_post": dist_post,
        "side_flip": side_flip,
        "src_pre": a.get("src"), "src": t["src"],
        "new": t["new"], "hold": t["hold"],
        "move": move, "move_src": "+".join(msrc) or None,
        "line_x": line_x, "line_gap": line_gap,
        "edge": edge, "edge_gap": edge_gap,
        "d_w": d_w, "ruler": ruler, "evidence": evidence,
        "verdict": verdict, "mapped": mapped, "resid": resid,
        "ep_up": ep_up, "nxt_vd": (nxt.get("vd") if nxt else None),
        "nxt_ref": (nxt.get("ref") if nxt else None),
        "ref_in_cand": ref_in_cand, "mapped_in_cand": mapped_in_cand,
        "remerge": remerge,
    }


# ---------- 米制目标与消费时序核验 ----------

METRIC_E_LANE = 0.10        # 相对横向误差允许的单拍变化（道）
FREE_DROP_M = 5.0           # 同一占用类型下净空下降的恶化阈值（米）
GEOM_AWAY_LANE = 0.7        # 修正后目标变远的允许量（道）；变近不算
_BLK_SEV = {None: 0, "unknown_car": 1, "car": 1, "space": 2}


def _cand_of(cand, lane):
    """候选道表里取某道：返回 (lane, free_m, width_margin, blocker)。"""
    if cand is None or lane is None:
        return None
    for c in cand:
        if abs(float(c[0]) - float(lane)) < 0.25:
            return c
    return None


def metric_event(ticks: list[dict], i: int) -> dict:
    """重基拍与下一拍的米制目标/相对误差/占用/计划可用性核验。

    e_y = (x_target − road_offset)·lane_w：决策消费的目标与实测 ego 在同一
    选择帧；x_target 是决策输出（选择器目标），road_offset 是实测自车横位。
    旧目标道 = 重基拍 route.ref（并线目的地）；占用取同拍 coarse 候选表。
    """
    t = ticks[i]
    nxt = ticks[i + 1] if i + 1 < len(ticks) else None
    if nxt is None:
        return None
    ref = t.get("ref")
    lw = t.get("lw") or 3.38
    ro_t, ro_n = t.get("road"), nxt.get("road")
    xt_t, xt_n = t.get("xt"), nxt.get("xt")
    e_t = (None if xt_t is None or ro_t is None
           else (float(xt_t) - float(ro_t)) * lw)
    e_n = (None if xt_n is None or ro_n is None
           else (float(xt_n) - float(ro_n)) * lw)
    de = None if e_t is None or e_n is None else e_n - e_t
    d_ro_next = (None if ro_t is None or ro_n is None
                 else float(ro_n) - float(ro_t))
    c_t = _cand_of(t.get("cand"), ref)
    c_n = _cand_of(nxt.get("cand"), ref)
    free_t = None if c_t is None else c_t[1]
    free_n = None if c_n is None else c_n[1]
    b_t = None if c_t is None else c_t[3]
    b_n = None if c_n is None else c_n[3]
    occ_worse = (_BLK_SEV.get(b_n, 1) > _BLK_SEV.get(b_t, 0)) or (
        b_t is not None and b_n == b_t and free_t is not None
        and free_n is not None and free_n < free_t - FREE_DROP_M)
    plan_ok = bool(nxt.get("plan_valid"))
    metric_ok = de is not None and abs(de) <= METRIC_E_LANE * lw
    frame_ok = (d_ro_next is not None
                and abs(d_ro_next) <= RULER_STABLE_LANE)
    # 合并几何：回溯本段 merge_start，比较「承诺时目标距离」与「修正后距离」
    ms_ro = None
    ms_fid = None
    for j in range(i - 1, max(-1, i - 400), -1):
        if ticks[j].get("stage") != t.get("stage"):
            break
        if ticks[j].get("vd") == "merge_start":
            ms_ro, ms_fid = ticks[j].get("road"), ticks[j].get("fid")
            break
    start_dist = (None if ms_ro is None or ref is None
                  else abs(float(ref) - float(ms_ro)))
    t_dist = (None if ro_t is None or ref is None
              else abs(float(ref) - float(ro_t)))
    dist_change = (None if start_dist is None or t_dist is None
                   else t_dist - start_dist)
    direction = (None if ms_ro is None or ref is None
                 else (1 if float(ref) > float(ms_ro) else
                       -1 if float(ref) < float(ms_ro) else 0))
    signed_ahead = (None if ro_t is None or ref is None
                    else (float(ref) - float(ro_t)) * direction
                    if direction else 0.0)
    overshoot = signed_ahead is not None and signed_ahead < 0
    away = dist_change is not None and dist_change > GEOM_AWAY_LANE
    if ref is None or e_t is None or c_t is None or c_n is None:
        verdict = "证据不足"
    elif occ_worse:
        verdict = "目标占用恶化"
    elif overshoot:
        verdict = "已越过目标"
    elif away:
        verdict = "目标前提变化"
    elif not frame_ok:
        verdict = "下一拍尺子跳变"
    elif not plan_ok:
        verdict = "计划仍无效"
    elif not metric_ok:
        verdict = "目标误差跳变"
    else:
        verdict = "米制通过"
    return {
        "fid": t["fid"], "kind": t["rebase"], "ref": ref,
        "ro_t": ro_t, "ro_n": ro_n, "d_ro_next": d_ro_next,
        "xt_t": xt_t, "xt_n": xt_n, "e_t": e_t, "e_n": e_n, "de": de,
        "free_t": free_t, "free_n": free_n, "b_t": b_t, "b_n": b_n,
        "occ_worse": occ_worse, "plan_ok": plan_ok,
        "plan_reason": nxt.get("plan_reason"), "frame_ok": frame_ok,
        "metric_ok": metric_ok,
        "ms_fid": ms_fid, "start_dist": start_dist, "t_dist": t_dist,
        "dist_change": dist_change, "direction": direction,
        "signed_ahead": signed_ahead, "overshoot": overshoot,
        "away": away, "verdict": verdict,
    }


def summarize_run(run: str, rows: list[dict], hold_m: float) -> dict:
    ticks = [tick_info(r, hold_m) for r in rows]
    windows = build_windows(ticks)
    index = {t["fid"]: i for i, t in enumerate(ticks)}
    # 窗口前事件（用起拍在 run 内的下标）
    for w in windows:
        i = index.get(w["start"], 0)
        w["before"] = _preceded(ticks, i, w["start"])
        # 10 拍内并线结局
        end_i = min(len(ticks) - 1, index.get(w["end"], i) + 10)
        w["ending"] = None
        for j in range(i, end_i + 1):
            if ticks[j]["vd"] in ENDINGS:
                w["ending"] = {"outcome": ticks[j]["vd"],
                               "fid": ticks[j]["fid"],
                               "dt_ticks": j - i}
                break
    # 并线结局是否被窗口前 10 拍覆盖
    endings = []
    for i, t in enumerate(ticks):
        if t["vd"] in ENDINGS:
            near = [w for w in windows
                    if 0 <= i - index.get(w["start"], -999) <= 10]
            endings.append({
                "fid": t["fid"], "outcome": t["vd"],
                "window": near[0] if near else None,
            })
    rebases = []
    for i, t in enumerate(ticks):
        if not t["rebase"]:
            continue
        road = t["road"]
        pre = t["pre_exec"]
        post = t["exec"]
        delta = (None if pre is None or post is None
                 else float(post) - float(pre))
        hold_age = 0
        j = i
        while j >= 0 and ticks[j]["hold"]:
            hold_age += 1
            j -= 1
        frozen_age = 0
        j = i
        base = road
        while (j >= 0 and base is not None
               and ticks[j]["road"] is not None
               and abs(float(ticks[j]["road"]) - float(base)) <= 1e-3):
            frozen_age += 1
            j -= 1
        exec_ro_gap = (None if pre is None or road is None
                       else abs(float(pre) - float(road)))
        adopt_gap = (None if post is None or road is None
                     else abs(float(post) - float(road)))
        nxt = ticks[i + 1] if i + 1 < len(ticks) else None
        ep_up = bool(nxt is not None and nxt["ep"] is not None
                     and t["ep"] is not None and nxt["ep"] > t["ep"])
        vd_next = nxt["vd"] if nxt is not None else None
        end10 = [ticks[j]["vd"] for j in
                 range(i + 1, min(len(ticks), i + 11))
                 if ticks[j]["vd"] in ENDINGS + ("merge_start",)]
        ref_after = [ticks[j]["ref"] for j in
                     range(i + 1, min(len(ticks), i + 11))
                     if ticks[j]["ref"] is not None]
        ref_changed = bool(ref_after and t["ref"] is not None
                           and max(abs(float(x) - float(t["ref"]))
                                   for x in ref_after) > 0.1)
        win = next((w for w in windows if w["start_i"] <= i <= w["end_i"]),
                   None)
        ev = {
            "run": run, "fid": t["fid"], "kind": t["rebase"],
            "fd": None if t["fd"] is None else round(t["fd"], 2),
            "road": None if road is None else round(float(road), 3),
            "pre_exec": None if pre is None else round(float(pre), 3),
            "post_exec": None if post is None else round(float(post), 3),
            "delta": None if delta is None else round(delta, 2),
            "src": t["src"], "new": t["new"], "hold": t["hold"],
            "hold_age": hold_age, "frozen_age": frozen_age,
            "exec_ro_gap": (None if exec_ro_gap is None
                            else round(exec_ro_gap, 2)),
            "adopt_gap": (None if adopt_gap is None
                          else round(adopt_gap, 2)),
            "ph": t["ph"], "ref": t["ref"], "ep": t["ep"],
            "merging_pre": t["ph"] == "merging",
            "ep_up": ep_up, "vd_next": vd_next, "end10": end10,
            "end": end10[0] if end10 else None,
            "ref_changed": ref_changed,
            "merging_after": any(ticks[j]["ph"] == "merging"
                                 for j in range(i + 1, min(len(ticks),
                                                           i + 11))),
            "win": win is not None,
        }
        ev["grade"], ev["reason"] = grade_rebase(ev)
        ev["cause"] = ("rebase" if (delta is not None
                                    and abs(delta) > 0.5 and ep_up
                                    and vd_next == "epoch_reset")
                       else "other" if ep_up else "none")
        ev["map"] = mapping_event(ticks, i) if t["ph"] == "merging" else None
        ev["metric"] = metric_event(ticks, i) if t["ph"] == "merging" else None
        rebases.append(ev)
    pairs = list(zip(ticks, ticks[1:]))
    base_src = sum(1 for a, b in pairs if a["src"] != b["src"])
    base_state = sum(
        1 for a, b in pairs
        if (a["new"], a["hold"]) != (b["new"], b["hold"]))
    covered = set()
    for w in windows:
        covered.update(range(w["start_i"], min(len(ticks),
                                               w["start_i"] + 11)))
    return {
        "run": run, "rows": len(rows), "ticks": ticks,
        "windows": windows, "endings": endings,
        "fd_known": sum(1 for t in ticks if t["fd"] is not None),
        "big": sum(1 for t in ticks if t["big"]),
        "noplan": sum(1 for t in ticks if t["plan"] == "none"),
        "big_valid": sum(1 for t in ticks
                         if t["big"] and t["plan"] == "valid"),
        "veto": Counter(t["preason"] for t in ticks
                        if t["plan"] == "veto"),
        "veto_not_big": Counter(t["preason"] for t in ticks
                                if t["plan"] == "veto" and not t["big"]),
        "base_src": base_src, "base_state": base_state,
        "n_pairs": len(pairs), "covered": len(covered),
        "rebases": rebases,
    }


def source_table(runs: list[dict]) -> list[dict]:
    ticks = [t for run in runs for t in run["ticks"]]
    total = len(ticks)
    by = defaultdict(lambda: {"ticks": 0, "new": 0, "hold": 0, "big": 0,
                              "starts": 0, "dom": 0, "lens": []})
    for t in ticks:
        if t["src"] == "none":
            by["none"]["ticks"] += 1
            continue
        b = by[t["src"]]
        b["ticks"] += 1
        b["new"] += int(t["new"])
        b["hold"] += int(t["hold"])
        b["big"] += int(t["big"])
    for run in runs:
        for w in run["windows"]:
            by[w["start_src"]]["starts"] += 1
            by[w["dom_src"]]["dom"] += 1
            by[w["start_src"]]["lens"].append(w["n_ticks"])
    out = []
    for src, b in sorted(by.items(), key=lambda kv: -kv[1]["ticks"]):
        lens = sorted(b["lens"])
        out.append({
            "src": src, "ticks": b["ticks"],
            "share": b["ticks"] / max(total, 1),
            "new": b["new"] / max(b["ticks"], 1),
            "hold": b["hold"] / max(b["ticks"], 1),
            "big": b["big"], "starts": b["starts"], "dom": b["dom"],
            "p50": statistics.median(lens) if lens else None,
            "max": max(lens) if lens else None,
        })
    return out


def cost_table(runs: list[dict]) -> tuple[list[dict], dict]:
    by = defaultdict(lambda: {"wins": 0, "veto": 0, "merge": 0,
                              "ending": Counter(), "frozen": 0, "ref": 0, "lay": 0,
                              "xdrift": 0, "req": 0,
                              "line": Counter(), "before": Counter()})
    endings = 0
    endings_after = Counter()
    for run in runs:
        for w in run["windows"]:
            b = by[w["start_src"]]
            b["wins"] += 1
            b["veto"] += int(w["veto"] > 0)
            b["merge"] += int(w["merge"])
            if w["ending"]:
                b["ending"][w["ending"]["outcome"]] += 1
            b["frozen"] += int(w["frozen"])
            b["ref"] += int(w["ref_changed"])
            b["lay"] += int(w["layer_changed"])
            b["xdrift"] += int(w["target_changed"])
            b["req"] += int(w["req"])
            for k, v in w["line"].items():
                b["line"][k] += v
            for k, v in w["before"].items():
                b["before"][k] += int(v)
        for e in run["endings"]:
            endings += 1
            if e["window"] is not None:
                endings_after[e["outcome"]] += 1
    out = []
    for src, b in sorted(by.items(), key=lambda kv: -kv[1]["wins"]):
        out.append({"src": src, **{k: b[k] for k in (
            "wins", "veto", "merge", "frozen", "ref", "lay",
            "xdrift", "req")},
            "ending": dict(b["ending"]), "line": dict(b["line"]),
            "before": dict(b["before"])})
    overall = {"endings": endings,
               "after_window": dict(endings_after)}
    return out, overall


# ---------- 输出 ----------

def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def _num(x, nd: int = 2) -> str:
    return "-" if x is None else f"{float(x):+.{nd}f}"


def print_metric(runs: list[dict]) -> None:
    """并线期重基的米制目标/相对误差/占用/计划核验表。"""
    rows = [(run["run"], e) for run in runs
            for e in run["rebases"] if e.get("metric")]
    print("\n### 米制目标与消费时序（并线期重基）\n")
    print("- 消费时序（源码顺序）：`engine.update` 吃 pre-executed 与 road_offset"
          "（决策/选择/路线）；`plan_motion` 吃同一 pre-executed；"
          "`planner.update` 先按旧状态控杆、再重基 executed；下一拍换纪门比较"
          "新 executed。trace 对应：`plan_ego[0]`=pre、`executed_lane`=post、"
          "`x_target`/`goal`=决策目标、`road_offset`=实测 ego。")
    print("- `e_y = (x_target − road_offset)·lane_w`：目标与实测 ego 的相对"
          "横向误差（米）。旧道占用取同拍 coarse 候选表（free=净空，括号内米）。")
    print("\n| 局 | fid | 旧ref | Δro下拍 | e_y(t) | e_y(t+1) | Δe | "
          "旧道占用 t→t+1 | merge距离 start→t | 下拍计划 | 结论 |")
    print("|---|---:|---:|---:|---:|---:|---:|---|---|---|---|")
    for rn, e in rows:
        m = e["metric"]
        def _occ(b, f):
            return f"{b or 'free'}({'-' if f is None else f'{f:.1f}'})"
        ref_s = "-" if m["ref"] is None else f"{m['ref']:g}"
        sd = "-" if m["start_dist"] is None else f"{m['start_dist']:.2f}"
        td = "-" if m["t_dist"] is None else f"{m['t_dist']:.2f}"
        print(f"| {rn} | {m['fid']} | {ref_s} | "
              f"{_num(m['d_ro_next'])} | {_num(m['e_t'])} | {_num(m['e_n'])} | "
              f"{_num(m['de'])} | {_occ(m['b_t'], m['free_t'])}→"
              f"{_occ(m['b_n'], m['free_n'])} | {sd}→{td} | "
              f"{m['plan_reason'] or '-'} | {m['verdict']} |")
    vc = Counter(e["metric"]["verdict"] for _, e in rows)
    print("\n- 结论分布：" + "，".join(f"{k} {v}" for k, v in
                                     sorted(vc.items(), key=lambda kv: -kv[1])))
    review = [(rn, e) for rn, e in rows
              if e.get("map") and e["map"]["verdict"].startswith("恒等")
              and e["metric"]["verdict"] == "米制通过"]
    print(f"- 评审候选（恒等 ∩ 米制通过）= {len(review)} 次："
          + "，".join(str(e["metric"]["fid"]) for _, e in review))
    if review:
        print("- 这组满足：旧目标道未被占/未恶化、下拍计划有效、下拍尺子稳定"
              "（|Δro|≤0.4）、相对误差变化 ≤0.10 道、参考系未重编号。**只有"
              "这组**支持提出「保留承诺」的候选方案；其余保持现状。")
    rest = [(e["metric"]["fid"], e["metric"]["verdict"])
            for _, e in rows
            if e.get("map") and e["map"]["verdict"].startswith("恒等")
            and e["metric"]["verdict"] != "米制通过"]
    if rest:
        print("- 恒等但未过米制核验："
              + "，".join(f"{fid}（{v}）" for fid, v in rest) + "。")
    print("- 尚未核验：跨道路径的**中间道**占用（本次只查目标道，候选表不含"
          "路径）；`lane_from` 不在 trace 里，合并源道无法逐次复核。因此"
          "即便这组全部通过，也不能无条件跳过换纪门。")


def print_mapping(runs: list[dict]) -> None:
    """并线期重基的参考系映射核验表与聚合。"""
    mrows = [(run["run"], e["map"]) for run in runs
             for e in run["rebases"] if e.get("map")]
    print("\n### 并线期重基：坐标映射核验\n")
    print(f"- 并线期重基 {len(mrows)} 次（重基拍 route.ph=merging）。"
          "「尺子」= road_offset 单拍变化：stable=|Δro|≤0.4（供数链跳变门容限）、"
          "moved=尺子自己动了。")
    print("\n| 局 | fid | 类型 | ref | Δexec | Δro | Δc0 | 尺子 | 物理证人 | "
          "复核gap | 目标距离 pre→post | 证据 | 结论 | 映射道 | 下拍换纪/ref | "
          "旧道在候选 | 10拍内并线 |")
    print("|---|---:|---|---:|---:|---:|---:|---|---|---:|---|---|---|---:|---|---|---|")
    for rn, m in mrows:
        ref_s = "-" if m["ref"] is None else f"{m['ref']:g}"
        src = m["move_src"] or "-"
        gap = m["line_gap"] if m["line_gap"] is not None else m["edge_gap"]
        gap_s = "-" if gap is None else f"{gap:.2f}"
        dp = "-" if m["dist_pre"] is None else f"{m['dist_pre']:+.2f}"
        dpo = "-" if m["dist_post"] is None else f"{m['dist_post']:+.2f}"
        map_s = "-" if m["mapped"] is None else f"{m['mapped']:g}"
        nref = "-" if m["nxt_ref"] is None else f"{m['nxt_ref']:g}"
        nxt_s = f"{'Y' if m['ep_up'] else '-'}/{nref}"
        inc = ("-" if m["ref_in_cand"] is None
               else "Y" if m["ref_in_cand"] else "N")
        print(f"| {rn} | {m['fid']} | {m['kind']} | {ref_s} | "
              f"{_num(m['dx'])} | {_num(m['d_ro'])} | {_num(m['d_c0'])} | "
              f"{m['ruler']} | {_num(m['move'])} ({src}) | {gap_s} | "
              f"{dp}→{dpo} | {m['evidence']} | {m['verdict']} | {map_s} | "
              f"{nxt_s} | {inc} | "
              f"{m['remerge'] if m['remerge'] is not None else '-'} |")
    rc = Counter(m["ruler"] for _, m in mrows)
    vc = Counter(m["verdict"] for _, m in mrows)
    fired = [m for _, m in mrows
             if m["ep_up"] and m["nxt_vd"] == "epoch_reset"]
    c0_fired = [m for m in fired
                if m["d_c0"] is not None and abs(m["d_c0"]) > 0.5]
    still = [m for m in fired
             if m["verdict"].startswith("恒等") and m["ref_in_cand"]]
    shift_ok = [m for _, m in mrows if m["verdict"] == "平移-确认"]
    print(f"\n- 尺子：stable {rc.get('stable', 0)}、moved {rc.get('moved', 0)}、"
          f"unknown {rc.get('unknown', 0)}。")
    print("- 映射结论：" + "，".join(f"{k} {v}" for k, v in
                                     sorted(vc.items(), key=lambda kv: -kv[1])))
    print(f"- 换纪门实际触发 {len(fired)} 次；其中 c0（场景尺子，①）自己跳超"
          f"半道 {len(c0_fired)} 次——其余全是 executed 单跳（②）触发的。")
    print(f"- 触发里结论为恒等（参考系未动、①也没跳）且旧 ref 仍在下一拍候选"
          f"道表 {len(still)} 次——这些清空的是**参考系没变、旧目标道仍存在**"
          f"的承诺。")
    flips = [m for m in still if m["side_flip"]]
    print(f"- 这 {len(still)} 次里，旧目标相对车的位置翻到另一侧 {len(flips)} 次"
          f"（两端都离道 >0.3 道才计）。")
    if shift_ok:
        print("- 平移-确认子集（旧道号在新参考系 = ref + round(Δro)）：")
        for m in shift_ok:
            lgap = ("-" if m["line_gap"] is None else f"{m['line_gap']:.2f}")
            print(f"  - fid {m['fid']}：ref {m['ref']:g} → "
                  f"{m['mapped']:g}（平移残差 {_num(m['resid'])} 道），"
                  f"物理证人 {_num(m['move'])} 道，线复核 gap {lgap}，"
                  f"映射道在下拍候选表 "
                  f"{'是' if m['mapped_in_cand'] else '否'}，"
                  f"下拍 ref {m['nxt_ref']}，10 拍内并线 {m['remerge']}。")
    else:
        print("- 平移-确认子集：0 次（尺子动了的事件里没有一次同时满足"
              "「物理证人无位移 + 平移量为整数道 + 新值被复核」）。")
    re_merge = [(rn, m) for rn, m in mrows if m["remerge"] is not None]
    same = sum(1 for _, m in re_merge
               if m["remerge"] is not None and m["ref"] is not None
               and abs(float(m["remerge"]) - float(m["ref"])) < 0.25)
    print(f"- 10 拍内重新并线 {len(re_merge)} 次，其中去向仍是旧 ref {same} 次。")



def print_report(runs: list[dict], hold_m: float, groups: dict[str, str]) -> None:
    print(f"\n# 路心帧差探针（hold={hold_m:.2f} m）\n")
    print("## 覆盖\n")
    print("| 局 | 分组 | tick | 有帧差 | 大差 | 大差且计划有效 | 窗口 | "
          "未生成计划 | 否决(帧差) |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for run in runs:
        print(f"| {run['run']} | {groups.get(run['run'], '-')} | "
              f"{run['rows']} | {run['fd_known']} | {run['big']} | "
              f"{run['big_valid']} | {len(run['windows'])} | "
              f"{run['noplan']} | "
              f"{run['veto'].get('plan:frame_shift', 0)} |")
    print("\n## 来源表（全部）\n")
    print("| 来源 | tick | 占比 | 新深度 | planner保持 | 大差拍 | 窗口首拍 | "
          "窗口主导 | 窗口长 p50/max |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in source_table(runs):
        print(f"| {r['src']} | {r['ticks']} | {_pct(r['share'])} | "
              f"{_pct(r['new'])} | {_pct(r['hold'])} | {r['big']} | "
              f"{r['starts']} | {r['dom']} | "
              f"{r['p50'] if r['p50'] is not None else '-'} / "
              f"{r['max'] if r['max'] is not None else '-'} |")
    costs, overall = cost_table(runs)
    print("\n## 代价表（按窗口首拍来源）\n")
    print("| 首拍来源 | 窗口 | 有否决 | 并线中 | 冻结中心 | 10拍内结局 | "
          "ref变化 | 层变化 | 有请求 | 选择x漂移 | 线仲裁 road/exec/none | "
          "前2拍源切换/状态切换/换档/重基 | 前5拍换纪 |")
    print("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|---|---|")
    for r in costs:
        line = r["line"]
        before = r["before"]
        print(f"| {r['src']} | {r['wins']} | {r['veto']} | {r['merge']} | "
              f"{r['frozen']} | {r['ending'] or '-'} | {r['ref']} | {r['lay']} | "
              f"{r['req']} | {r['xdrift']} | "
              f"{line.get('road', 0)}/{line.get('exec', 0)}/"
              f"{line.get('none', 0)} | "
              f"{before.get('src_switch', 0)}/"
              f"{before.get('state_switch', 0)}/"
              f"{before.get('hop', 0)}/{before.get('rebase', 0)} | "
              f"{before.get('epoch', 0)} |")
    windows = sum(len(r["windows"]) for r in runs)
    n_pairs = sum(r["n_pairs"] for r in runs)
    base_src = sum(r["base_src"] for r in runs)
    base_state = sum(r["base_state"] for r in runs)
    sw = sum(c["before"].get("src_switch", 0) for c in costs)
    st = sum(c["before"].get("state_switch", 0) for c in costs)
    cover = sum(r["covered"] for r in runs)
    tick_n = sum(r["rows"] for r in runs)
    end_n = overall["endings"]
    end_after = sum(overall["after_window"].values())
    print(f"\n- 基线：相邻拍源切换 {_pct(base_src / max(n_pairs, 1))}、"
          f"状态切换 {_pct(base_state / max(n_pairs, 1))}；窗口首拍源切换 "
          f"{_pct(sw / max(windows, 1))}、状态切换 {_pct(st / max(windows, 1))}。")
    print(f"- 并线结局 {end_n}，其中前 10 拍内有窗口 {end_after}"
          f"（{_pct(end_after / max(end_n, 1))}）；窗口起点后 10 拍覆盖 "
          f"{_pct(cover / max(tick_n, 1))} 的拍——前者要和这个基线比。")
    big_valid = sum(r["big_valid"] for r in runs)
    other = Counter()
    for r in runs:
        other.update(r["veto_not_big"])
    print(f"- 机制检查：大差且计划有效 {big_valid}（应≈0）；非大差但被否 "
          f"{dict(other)}。")
    print()
    print("## 重基事件（lat_ctrl.rebase）")
    rb = defaultdict(lambda: {"n": 0, "d": [], "big": 0, "end": Counter()})
    for run in runs:
        for e in run["rebases"]:
            b = rb[e["kind"]]
            b["n"] += 1
            if e["delta"] is not None:
                b["d"].append(abs(e["delta"]))
            b["big"] += int(e["win"])
            if e["end"]:
                b["end"][e["end"]] += 1
    print("| 类型 | 次数 | |exec跳变| p50/max | >0.5道 | 大差窗口内 | "
          "后10拍结局 |")
    print("|---|---:|---|---:|---:|---|")
    for kind, b in sorted(rb.items(), key=lambda kv: -kv[1]["n"]):
        ds = sorted(b["d"])
        p50 = round(statistics.median(ds), 2) if ds else "-"
        mx = round(ds[-1], 2) if ds else "-"
        gt = sum(1 for x in ds if x > 0.5)
        print(f"| {kind} | {b['n']} | {p50} / {mx} | {gt} | {b['big']} | "
              f"{dict(b['end']) or '-'} |")
    if groups:
        for g in sorted(set(groups.values())):
            sub = [r for r in runs if groups.get(r["run"]) == g]
            c = Counter(e["kind"] for r in sub for e in r["rebases"])
            print(f"- {g} 重基：" + str(dict(c)))
    print()
    print("### 逐次分层（证据等级）")
    grade = defaultdict(lambda: {"n": 0, "kinds": Counter(), "src": Counter(),
                                 "hold": [], "exec": [], "adopt": [],
                                 "epoch": 0, "cause": Counter(),
                                 "merging": 0, "ref": 0})
    for run in runs:
        for e in run["rebases"]:
            g = grade[e["grade"]]
            g["n"] += 1
            g["kinds"][e["kind"]] += 1
            g["src"][e["src"]] += 1
            g["hold"].append(e["hold_age"])
            if e["exec_ro_gap"] is not None:
                g["exec"].append(e["exec_ro_gap"])
            if e["adopt_gap"] is not None:
                g["adopt"].append(e["adopt_gap"])
            g["epoch"] += int(e["ep_up"])
            g["cause"][e["cause"]] += 1
            g["merging"] += int(e["merging_pre"])
            g["ref"] += int(e["ref_changed"])
    print("| 等级 | 次数 | 类型 | 来源 | 保持年龄 p50/max | exec-ro p50 | "
          "adopt p50 | 下一拍换纪 | 并线前/ref变 |")
    print("|---|---:|---|---|---|---:|---:|---:|---|")
    for name, g in sorted(grade.items()):
        hold = sorted(g["hold"])
        h50 = statistics.median(hold) if hold else "-"
        hmax = hold[-1] if hold else "-"
        e50 = round(statistics.median(g["exec"]), 2) if g["exec"] else "-"
        a50 = round(statistics.median(g["adopt"]), 2) if g["adopt"] else "-"
        print(f"| {name} | {g['n']} | {dict(g['kinds'])} | "
              f"{dict(g['src'])} | {h50} / {hmax} | {e50} | {a50} | "
              f"{g['epoch']} | {g['merging']}/{g['ref']} |")
    evs = [e for run in runs for e in run["rebases"]]
    n = len(evs)
    reb_cause = sum(1 for e in evs if e["cause"] == "rebase")
    other_cause = sum(1 for e in evs if e["cause"] == "other")
    no_epoch = sum(1 for e in evs if e["cause"] == "none")
    print(f"- 因果：{n} 次重基中，|Δexec|>0.5 且下一拍换纪 {reb_cause}、"
          f"换纪但非重基引起 {other_cause}、无换纪 {no_epoch}。")
    merge_pre = sum(1 for e in evs if e["merging_pre"])
    ref_chg = sum(1 for e in evs
                  if e["merging_pre"] and e["ref_changed"])
    new_merge = sum(1 for e in evs if "merge_start" in e["end10"])
    print(f"- 承诺：重基前在并线 {merge_pre} 次，其中 10 拍内 ref 变化 "
          f"{ref_chg} 次；10 拍内重新起意并线 {new_merge} 次。")
    print_mapping(runs)
    print_metric(runs)
    srcs = source_table(runs)
    print("\n## 三问\n")
    top = sorted(srcs, key=lambda r: -r["starts"])[:3]
    print("1. 窗口首拍来源前三：" + "，".join(
        f"{r['src']} {r['starts']}" for r in top if r["starts"]) + "。")
    veto_w = sum(c["veto"] for c in costs)
    merge_w = sum(c["merge"] for c in costs)
    end_w = sum(sum(c["ending"].values()) for c in costs)
    print(f"2. 共 {windows} 个窗口：有帧差否决 {veto_w}（与大差同源，机械关系）、"
          f"并线中 {merge_w}、窗口内/后 10 拍有结局 {end_w}。")
    hop = sum(r["before"].get("hop", 0) for r in costs)
    reb = sum(r["before"].get("rebase", 0) for r in costs)
    ep = sum(r["before"].get("epoch", 0) for r in costs)
    line_tot = {k: sum(r["line"].get(k, 0) for r in costs)
                for k in ("road", "exec", "none")}
    print(f"3. 首拍前 2 拍：源切换 {sw}、状态切换 {st}、换档 {hop}、重基 "
          f"{reb}；前 5 拍换纪/重建 {ep}；大差拍线仲裁 {line_tot}。")
    if groups:
        print("\n## 分组摘要\n")
        print("| 分组 | 窗口 | 大差拍 | 有否决 | 并线中 | 冻结中心 | "
              "有结局 | ref变化 | 有请求 |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for g in sorted(set(groups.values())):
            sub = [r for r in runs if groups.get(r["run"]) == g]
            if not sub:
                continue
            c, _ = cost_table(sub)
            print(f"| {g} | {sum(len(r['windows']) for r in sub)} | "
                  f"{sum(r['big'] for r in sub)} | "
                  f"{sum(x['veto'] for x in c)} | "
                  f"{sum(x['merge'] for x in c)} | "
                  f"{sum(x['frozen'] for x in c)} | "
                  f"{sum(sum(x['ending'].values()) for x in c)} | "
                  f"{sum(x['ref'] for x in c)} | "
                  f"{sum(x['req'] for x in c)} |")
    print("\n## 代表性窗口（按长度）\n")
    all_w = [(w["n_ticks"], r["run"], w) for r in runs for w in r["windows"]]
    for _, run, w in sorted(all_w, key=lambda t: -t[0])[:8]:
        print(f"- {run} fid {w['start']}–{w['end']}（{w['n_ticks']} 拍，"
              f"首 {w['start_src']}，主导 {w['dom_src']}，"
              f"fd {w['fd_start']}→{w['fd_end']}，{w['shape']}，"
              f"冻结 {w['frozen_run']}拍/执行 {w['exec_move']}道，"
              f"切换 {len(w['switches'])}，结局 {w['ending']}）")


# ---------- 自测 ----------

def _mk(fid, fd, src="wall", new=True, hold=False, plan="veto",
        preason="plan:frame_shift", ph="keep", vd="keep", target=0.0,
        req=None, hop=0, rebase=None):
    return {"fid": fid, "fd": fd, "big": fd is not None and abs(fd) > 1.0,
            "src": src, "new": new, "hold": hold, "plan": plan,
            "preason": preason, "ph": ph, "vd": vd, "target": target,
            "req": req, "hop": hop, "rebase": rebase, "line": None,
            "road": None, "exec": None, "stage": 1, "fd_rebuilt": False,
            "raw": {}}


def _mtick(fid, pre, post, road, ref=0.0, src="wall", new=True,
           hold=False, lines=(), line_x=None, edge=None, w=4.5, ep=0,
           vd="keep", xt=None, plan_valid=True):
    """映射/米制核验自测用的最小 tick（字段与 tick_info 同口径）。"""
    return {"fid": fid, "pre_exec": pre, "exec": post, "road": road,
            "src": src, "new": new, "hold": hold, "lines": list(lines),
            "line_x": line_x, "edge": edge, "w": w, "ref": ref, "ep": ep,
            "vd": vd, "rebase": "jump_confirm", "stage": 1,
            "xt": road if xt is None else xt, "goal": None,
            "plan_valid": plan_valid, "plan_reason": "plan:ok" if plan_valid
            else "plan:frame_shift", "plan_fd": 0.0, "lw": 3.38,
            "cand": [(float(k), 24.0, 3.0, None) for k in range(-2, 3)]}


def selftest() -> None:
    ticks = [_mk(1, 0.0), _mk(2, 1.2), _mk(3, 1.3), _mk(4, 0.9),
             _mk(5, 1.5), _mk(6, 0.0), _mk(7, 0.0), _mk(8, 1.6),
             _mk(9, 0.0)]
    ws = build_windows(ticks)
    assert [w["start"] for w in ws] == [2, 8], ws
    assert ws[0]["n_ticks"] == 4 and ws[0]["n_big"] == 3
    # 未知拍断开
    ws2 = build_windows([_mk(1, 1.2), _mk(2, None), _mk(3, 1.3)])
    assert [w["start"] for w in ws2] == [1, 3], ws2
    # 两拍非大差断开
    ws3 = build_windows([_mk(1, 1.2), _mk(2, 0.0), _mk(3, 0.0),
                         _mk(4, 1.3)])
    assert [w["start"] for w in ws3] == [1, 4], ws3
    # 目标变化
    assert _target_changed([_mk(1, 1.2, target=0.0),
                            _mk(2, 1.2, target=0.5)])
    assert not _target_changed([_mk(1, 1.2, target=0.0),
                                _mk(2, 1.2, target=0.05)])
    # 来源/计划分类（走真实读取路径）
    row = {"fid": 1, "scene": {"ego_m": 3.0, "lane_w": 3.38},
           "executed_lane": 0.5, "plan": {"valid": False,
                                          "reason": "plan:frame_shift",
                                          "fd": 1.31},
           "route": {"ph": "keep", "vd": "keep"},
           "ro_source": "wall_occl", "dgeo_new": True, "ro_hold": True,
           "lane_fuse": {"trusted": True, "sm": 0.4},
           "road_offset": 0.8, "sel_dbg": {"target": 0.8}, "lat_ctrl": {}}
    t = tick_info(row, 1.0)
    assert t["big"] and t["plan"] == "veto" and t["src"] == "wall_occl"
    assert t["new"] and t["hold"] and t["line"] == 0.4
    # 映射核验：尺子没动 + 状态追平 + 线复核 → 恒等-确认
    a = _mtick(1, 0.2, 0.2, 0.1, lines=[("y", -1, -1.3, 18)], line_x=0.1)
    b = _mtick(2, 0.2, 1.0, 0.12, lines=[("y", -1, -1.28, 18)], line_x=0.9)
    n = _mtick(3, 1.0, 1.0, 0.12, ep=1, vd="epoch_reset")
    m = mapping_event([a, b, n], 1)
    assert m["ruler"] == "stable" and m["verdict"].startswith("恒等"), m
    assert m["evidence"] == "双证" and abs(m["move"]) < 0.05, m
    assert m["mapped"] == 0.0 and m["ep_up"], m
    # 尺子动了 + 保持/记忆源 → 陈旧可疑
    a = _mtick(1, 0.0, 0.0, 0.0, src="memory", hold=True)
    b = _mtick(2, 0.0, 0.9, 0.9, src="memory", hold=True)
    m = mapping_event([a, b], 1)
    assert m["ruler"] == "moved" and m["verdict"] == "陈旧可疑", m
    # 尺子动 + 证人无位移 + 整数平移 → 平移-确认
    a = _mtick(1, 0.0, 0.0, 0.4, ref=1.0, lines=[("y", 1, 1.5, 15)])
    b = _mtick(2, 0.0, -1.0, -0.6, ref=1.0, lines=[("y", 1, 1.48, 16)],
               line_x=-0.98)
    m = mapping_event([a, b], 1)
    assert m["verdict"] == "平移-确认" and m["mapped"] == 0.0, m
    # 线配对拒绝 >0.6 道跳变
    assert _match_line({"lines": [("y", 1, 1.0, 5)]},
                       {"lines": [("y", 1, 2.0, 5)]}) is None
    # 米制核验：旧目标未被占 + 计划有效 + 尺子稳 + 误差连续 → 米制通过
    a = _mtick(1, 0.0, 0.0, 0.0, ref=1.0)
    b = _mtick(2, 0.0, 1.0, 0.0, ref=1.0, xt=0.8)
    c = _mtick(3, 1.0, 1.0, 0.0, ref=0.0, xt=0.8, plan_valid=True)
    m = metric_event([a, b, c], 1)
    assert m["verdict"] == "米制通过", m
    # 旧目标道在下一拍出现车 → 占用恶化
    c2 = _mtick(3, 1.0, 1.0, 0.0, ref=0.0, xt=0.8)
    c2["cand"] = [(float(k), 6.0, 3.0, "car" if k == 1 else None)
                  for k in range(-2, 3)]
    assert metric_event([a, b, c2], 1)["verdict"] == "目标占用恶化"
    # 下一拍计划仍被否 → 计划仍无效
    c3 = _mtick(3, 1.0, 1.0, 0.0, ref=0.0, xt=0.8, plan_valid=False)
    assert metric_event([a, b, c3], 1)["verdict"] == "计划仍无效"
    # 修正后已越过目标 → 已越过目标
    ms = _mtick(1, 0.0, 0.0, -1.0, ref=0.0, vd="merge_start")
    b2 = _mtick(2, 0.0, 1.2, 1.2, ref=0.0, xt=0.0)
    c2 = _mtick(3, 1.2, 1.2, 1.2, ref=1.0, xt=0.0)
    assert metric_event([ms, b2, c2], 1)["verdict"] == "已越过目标"
    # 修正后离目标更远（>0.7 道）→ 目标前提变化
    ms = _mtick(1, 0.0, 0.0, -1.0, ref=1.0, vd="merge_start")
    b3 = _mtick(2, 0.0, -2.0, -2.0, ref=1.0, xt=1.0)
    c3 = _mtick(3, -2.0, -2.0, -2.0, ref=-1.0, xt=1.0)
    assert metric_event([ms, b3, c3], 1)["verdict"] == "目标前提变化"
    print("selftest ok")


# ---------- 入口 ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="logs 根目录（默认 %%APPDATA%%/MaaRacingMaster/logs）")
    ap.add_argument("--session", action="append", default=None,
                    help="只跑指定会话（可重复）")
    ap.add_argument("--manual", action="append", default=[],
                    help="标记为人工局的会话（可重复）")
    ap.add_argument("--hold-m", type=float, default=None)
    ap.add_argument("--csv", default=None, help="重基逐次事件 CSV 输出路径")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if args.root:
        root = Path(args.root)
    else:
        root = Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs"
    if not root.is_dir():
        raise SystemExit(f"logs 根目录不存在：{root}")
    hold_m = args.hold_m if args.hold_m is not None else load_hold_m(None)
    runs = []
    groups = {}
    for token, path in discover(root):
        if args.session and token not in args.session:
            continue
        for run, rows in load_runs(path).items():
            runs.append(summarize_run(run, rows, hold_m))
            groups[run] = "manual" if token in args.manual else "program"
    if not runs:
        raise SystemExit("没有可分析的会话")
    print_report(runs, hold_m, groups)
    if args.csv:
        import csv
        keys = ["run", "fid", "kind", "grade", "reason", "cause", "src",
                "new", "hold", "hold_age", "frozen_age", "road",
                "pre_exec", "post_exec", "delta", "fd", "exec_ro_gap",
                "adopt_gap", "ph", "ref", "ep", "ep_up", "vd_next",
                "end10", "ref_changed", "merging_pre", "merging_after",
                "win"]
        map_keys = ["fid", "kind", "ref", "pre", "post", "dx", "d_ro",
                    "d_c0", "dist_pre", "dist_post", "side_flip",
                    "src", "new", "hold", "move", "move_src", "line_gap",
                    "edge_gap", "d_w", "ruler", "evidence", "verdict",
                    "mapped", "resid", "ep_up", "nxt_vd", "nxt_ref",
                    "ref_in_cand", "mapped_in_cand", "remerge"]
        fields = keys + [f"map_{k}" for k in map_keys]
        metric_keys = ["fid", "kind", "ref", "ro_t", "ro_n", "d_ro_next",
                       "xt_t", "xt_n", "e_t", "e_n", "de", "free_t",
                       "free_n", "b_t", "b_n", "occ_worse", "plan_ok",
                       "plan_reason", "frame_ok", "metric_ok", "ms_fid",
                       "start_dist", "t_dist", "dist_change", "direction",
                       "signed_ahead", "overshoot", "away", "verdict"]
        fields += [f"metric_{k}" for k in metric_keys]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for run in runs:
                for e in run["rebases"]:
                    row = {k: e.get(k) for k in keys}
                    m = e.get("map")
                    if m:
                        row.update({f"map_{k}": m.get(k) for k in map_keys})
                    me = e.get("metric")
                    if me:
                        row.update({f"metric_{k}": me.get(k)
                                    for k in metric_keys})
                    w.writerow(row)
        print()
        print(f"CSV: {args.csv}")


if __name__ == "__main__":
    main()
