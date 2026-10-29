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

## 输出

- 覆盖表：每局 tick / 有帧差 / 大差 / 窗口 / 未生成计划 / 未知帧差。
- 来源表：每个路心来源的拍数、新深度比例、planner 接纳比例、大差拍、窗口
  首拍数、窗口主导数、窗口长度。
- 代价表：按窗口首拍来源分账——计划否决、并线中、10 拍内并线结局、目标变化、
  换道请求、线仲裁、窗口前是否紧跟来源切换/换档/重基/换纪。
- 自测：`--selftest`（窗口合并、未知拍断开、来源分类、目标变化）。
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
    src = r.get("ro_source") or "none"
    new = bool(r.get("dgeo_new"))
    hold = bool(r.get("ro_hold"))
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
        "road": r.get("road_offset"),
        "exec": r.get("executed_lane"),
        "ph": rt.get("ph"),
        "vd": rt.get("vd"),
        "why": rt.get("why"),
        "target": dbg.get("target"),
        "req": dbg.get("req"),
        "layer": r.get("sel_layer"),
        "ref": rt.get("ref"),
        "tgt": rt.get("tgt"),
        "intent": r.get("intent_lane"),
        "hop": _hop(r),
        "rebase": (r.get("lat_ctrl") or {}).get("rebase"),
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
        prev_exec = ticks[i - 1]["exec"] if i > 0 else None
        delta = (None if prev_exec is None or t["exec"] is None
                 else float(t["exec"]) - float(prev_exec))
        win = next((w for w in windows if w["start_i"] <= i <= w["end_i"]),
                   None)
        end = None
        for j in range(i, min(len(ticks), i + 11)):
            if ticks[j]["vd"] in ENDINGS:
                end = {"outcome": ticks[j]["vd"], "dt": j - i}
                break
        rebases.append({"kind": t["rebase"], "fid": t["fid"],
                        "fd": None if t["fd"] is None else round(t["fd"], 2),
                        "delta": None if delta is None else round(delta, 2),
                        "win": win is not None, "end": end})
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
                b["end"][e["end"]["outcome"]] += 1
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


if __name__ == "__main__":
    main()
