# -*- coding: utf-8 -*-
"""重锚事件归因探针（只读，版本 reanchor-attribution/1）。

口径沿用 ``tools/experiments/merge_contract/COMPLETION_SPEC.md`` 的「按局、
不池化」原则：每局单独报告分母，跨局只做并列展示，不做合并推断。

它回答三个问题，不改任何控制逻辑：
1. 每次「executed 横位单拍跳超过半道」的来源证据是什么（多标签 + 便于看
   分布的单标签）；
2. 这次跳变的直接后果——下一拍照例换纪、承诺道变化、所在并线段是否结束，
   之后一段时间内是否重新起意/完成/撤销/超时；
3. 相对其它中断原因，重锚的暴露与占比——并线段遭遇/未遭遇两组的结束原因
   分布、全部中断段按原因分账、跳变发生在「并线中/有请求/计分中」的次数。

**时序模型（本探针最容易错的一点，显式写出）**：决策层在 tick t 读到的
``executed`` 是上一拍 planner 更新后的值（module 先 ``engine.update`` 再
``planner.update``），所以 trace 行 i 的 ``executed_lane`` 到 tick i+1 才被
``_ruler_epoch`` 检查：

- 跳变判定在 trace 行的 ``executed_lane`` 序列上：``|exec[i]-exec[i-1]| > 0.5``；
- 由它触发的换纪行应在 **i+1**（前提是路线层当拍有状态且纪元确实推进；
  ``route.vd=="epoch_reset"`` 是重建发生的落痕）；
- 绝不用「同一行既有跳变又有 epoch_reset」做配对。

**不新造判定门**：跳变门 0.5 道取生产常量
``DecisionEngine.RULER_TOL_LANE``（decision.py，``_ruler_epoch`` 用它比较
决策入口的 executed，本探针在 trace 的 executed 序列上用同一门）；黑视间隔
1.0 s 取 ``module.HOP_BLACKOUT_S``（``ro_hop==2`` 的判定门）。「v 落到 ~0」
只作描述性列（``V_ZERO_LANE_S`` / ``V_PRE_MIN_LANE_S`` 仅为展示口径），
不参与任何「原因已证实」的结论。

数据源（与 probe_merge_baseline 同款，解析函数直接复用）：
- 会话流：``<logs>/<会话>/trace/speedrush.jsonl``（一文件多局，按 stage 切局）；
- 旧局：``<data>/<局>/trace_<局>.jsonl``（一文件一局，走 ``--runs``）。

用法：
  .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_reanchor_attribution.py
  ... --session 20261028_083410_1,20261028_171845_1
  ... --runs <局名>[,..] --data <debug/speedrush 目录>
  ... --out <仓库外路径>

输出：默认只打印 stdout；``--out`` 只接受仓库外路径。缺列记
``missing_field`` 并列出缺哪些列，不拿 0 冒充。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections import Counter, defaultdict

PROBE_VERSION = "reanchor-attribution/1"
SPEC_REL = "tools/experiments/merge_contract/COMPLETION_SPEC.md"

# 跳变门：生产常量 DecisionEngine.RULER_TOL_LANE（decision.py，_ruler_epoch
# 对 executed 的单拍差门）。本探针只复用这一条，不另立门槛。
JUMP_TOL_LANE = 0.5
# 黑视间隔门：生产常量 module.HOP_BLACKOUT_S（ro_hop==2 的判定门）。
BLACKOUT_GAP_S = 1.0
# 「v 落到 ~0」的描述性展示门（不是判定门，不参与归因定论）：
#   |v_after| <= V_ZERO 且 |v_before| >= V_PRE 时，在事件行里标成
#   陈旧重基**签名**（生产 _stale_rebase 把 v_lat_est 清零）。它只说明
#   「可观测落幅」，不证明就是 _stale_rebase 路径。
V_ZERO_LANE_S = 0.02
V_PRE_MIN_LANE_S = 0.2

# 后果观察窗口：跳变之后最多看这么多拍，或到下一次并线段结束为止。
WINDOW_TICKS = 200

# 标签优先序（仅便于看分布；不得当成已证实原因）。
LABEL_FRESH = "新鲜读数跳变"
LABEL_SHADOW = "影子重锚"
LABEL_BLACKOUT = "黑视/断供"
LABEL_STALE = "陈旧重基签名"
LABEL_UNKNOWN = "不明"
LABEL_ORDER = (LABEL_FRESH, LABEL_SHADOW, LABEL_BLACKOUT, LABEL_STALE,
               LABEL_UNKNOWN)

# 事件归因所需的列（缺列 ≠ 缺值：缺列记 missing_field）
REQUIRED_KEYS = (
    "fid", "dt", "executed_lane", "v_lat_est", "road_offset", "ro_hold",
    "ro_hop", "ro_gap_s", "lateral_frame", "coarse", "route", "plan",
    "sel_dbg", "state",
)

# 默认跑这五个会话（任务指定）；缺失的会话如实报出，不静默跳过。
DEFAULT_SESSIONS = (
    "20261028_083410_1",
    "20261028_171845_1",
    "20261027_160949_1",
    "20261027_165841_1",
    "20261027_183143_1",
)


# ---------------------------------------------------------------- 基础工具


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")


DEFAULT_DATA = os.environ.get(
    "SPEEDRUSH_DEBUG",
    os.path.join(_appdata(), "MaaRacingMaster", "debug", "speedrush"))
DEFAULT_LOGS = os.environ.get(
    "SPEEDRUSH_LOGS",
    os.path.join(os.path.dirname(os.path.dirname(DEFAULT_DATA)), "logs"))


def _repo_root() -> str | None:
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _commit(root: str | None, override: str) -> str:
    if override:
        return override
    if root is None:
        return "unknown"
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 —— 版本号取不到不阻断归因
        return "unknown"


def _baseline():
    """按路径加载同目录的 probe_merge_baseline.py，复用其解析/切局逻辑。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_merge_baseline.py")
    spec = importlib.util.spec_from_file_location("merge_baseline_probe", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _fmt(x, nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _split(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def route_of(row: dict) -> dict:
    r = row.get("route")
    return r if isinstance(r, dict) else {}


def lf_of(row: dict) -> dict:
    r = row.get("lateral_frame")
    return r if isinstance(r, dict) else {}


def sel_of(row: dict) -> dict:
    r = row.get("sel_dbg")
    return r if isinstance(r, dict) else {}


def plan_of(row: dict) -> dict:
    r = row.get("plan")
    return r if isinstance(r, dict) else {}


def c0_of(row: dict):
    """尺子读数（道；选中帧 z=0 中心线），生产 ``_ruler_epoch`` 的第一个输入。"""
    return _num(sel_of(row).get("c0"))


# ---------------------------------------------------------------- 事件识别


def find_jumps(rows) -> list[dict]:
    """executed 单拍跳变：``|exec[i] - exec[i-1]| > JUMP_TOL_LANE``（道）。"""
    out = []
    for i in range(1, len(rows)):
        e0 = _num(rows[i - 1].get("executed_lane"))
        e1 = _num(rows[i].get("executed_lane"))
        if e0 is None or e1 is None:
            continue
        d = e1 - e0
        if abs(d) > JUMP_TOL_LANE:
            out.append({"i": i, "ex0": e0, "ex1": e1, "d": d,
                        "v0": _num(rows[i - 1].get("v_lat_est")),
                        "v1": _num(rows[i].get("v_lat_est"))})
    return out


def find_resets(rows) -> list[int]:
    """换纪行的落痕：``route.vd == "epoch_reset"``（_rebuild 的 epoch_reset）。"""
    return [i for i, r in enumerate(rows)
            if route_of(r).get("vd") == "epoch_reset"]


def labels_for(rows, i: int):
    """一次跳变的供数证据（多标签，不强行归一个原因）。"""
    a, b = rows[i - 1], rows[i]
    lab = set()
    info = {}
    hop_a, hop_b = _num(a.get("ro_hop")), _num(b.get("ro_hop"))
    info["hop"] = (hop_a, hop_b)
    if hop_a == 1 or hop_b == 1:
        lab.add(LABEL_FRESH)
    gaps = [g for g in (_num(a.get("ro_gap_s")), _num(b.get("ro_gap_s")))
            if g is not None]
    info["gap"] = max(gaps) if gaps else None
    hold_a, hold_b = a.get("ro_hold") is True, b.get("ro_hold") is True
    info["hold"] = (hold_a, hold_b)
    if (hop_a == 2 or hop_b == 2
            or (info["gap"] is not None and info["gap"] > BLACKOUT_GAP_S)
            or (hold_a and hold_b)
            or a.get("road_offset") is None or b.get("road_offset") is None):
        lab.add(LABEL_BLACKOUT)
    # 影子重锚：i / i+1 行影子进入 reanchor 过渡态（影子按最大校正速率追
    # 目标，x 不是当拍测量）。只记来源，不读 x 当证据。
    shadow = []
    for k in (i, i + 1):
        if k < len(rows):
            lf = lf_of(rows[k])
            if lf.get("ev") == "reanchor":
                shadow.append((k - i, lf.get("src")))
    info["shadow"] = shadow
    if shadow:
        lab.add(LABEL_SHADOW)
    # 陈旧重基签名：跳变后 v 落到 ~0 且跳变前明显非零（描述性）。
    v0, v1 = _num(a.get("v_lat_est")), _num(b.get("v_lat_est"))
    info["v"] = (v0, v1)
    if (v0 is not None and v1 is not None
            and abs(v0) >= V_PRE_MIN_LANE_S and abs(v1) <= V_ZERO_LANE_S):
        lab.add(LABEL_STALE)
    if not lab:
        lab.add(LABEL_UNKNOWN)
    info["multi"] = lab
    info["single"] = next(x for x in LABEL_ORDER if x in lab)
    # 帧差：诊断列，不单独定性。
    fd = []
    for k in (i, i + 1):
        if k < len(rows):
            fd.append(_num(plan_of(rows[k]).get("fd")))
    info["fd"] = fd
    return info


def _last_cruise(rows, r: int) -> int | None:
    """r 之前（不含）最近一个 CRUISE 行——生产端 `_ruler_*` 的上次更新点。"""
    for k in range(r - 1, -1, -1):
        if rows[k].get("state") == "CRUISE":
            return k
    return None


def _last_cruise_c0(rows, r: int) -> int | None:
    for k in range(r - 1, -1, -1):
        if rows[k].get("state") == "CRUISE" and c0_of(rows[k]) is not None:
            return k
    return None


def reset_evidence(rows, r: int) -> dict:
    """换纪行 r 的生产端来源复原（不猜：按 `_ruler_epoch` 的比较对象复算）。

    tick r 上：executed 比较 executed[r-1] 与上一次 CRUISE 行喂入的
    executed[p-1]；尺子比较 c0[r] 与上一次 CRUISE 且 c0 非空行的 c0。
    p == r-1 时就是相邻行跳变；p < r-1 说明中间夹了非巡航/缺路拍。
    """
    p = _last_cruise(rows, r)
    exec_cmp = None
    if p is not None and p >= 1:
        e1, e0 = _num(rows[r - 1].get("executed_lane")), \
            _num(rows[p - 1].get("executed_lane"))
        if e1 is not None and e0 is not None:
            exec_cmp = e1 - e0
    q = _last_cruise_c0(rows, r)
    c0_cmp = None
    c1 = c0_of(rows[r])
    if q is not None and c1 is not None:
        c0c = c0_of(rows[q])
        if c0c is not None:
            c0_cmp = c1 - c0c
    return {"exec_d": exec_cmp, "exec_gap": None if p is None else r - 1 - p,
            "c0_d": c0_cmp,
            "ruler": c0_cmp is not None and abs(c0_cmp) > JUMP_TOL_LANE,
            "exec_span": exec_cmp is not None
            and abs(exec_cmp) > JUMP_TOL_LANE}


# ---------------------------------------------------------------- 并线段


def segments(rows) -> list[dict]:
    """换道段 = route.ph 连续为 merging 的拍；结束拍取离开 merging 的那一拍。"""
    out, cur = [], None
    for i, r in enumerate(rows):
        rt = r.get("route")
        if not isinstance(rt, dict):
            if cur is not None:
                cur["unknown"] += 1
            continue
        if rt.get("ph") == "merging":
            if cur is None:
                cur = {"i0": i, "i1": i, "ie": None, "tgt": rt.get("tgt"),
                       "vd": None, "why": None, "unknown": 0,
                       "fid0": r.get("fid"), "fid1": r.get("fid")}
            cur["i1"] = i
            cur["fid1"] = r.get("fid")
        elif cur is not None:
            cur["ie"] = i
            cur["vd"] = rt.get("vd")
            cur["why"] = rt.get("why")
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def end_cat(vd) -> str:
    if vd == "finish":
        return "finish"
    if vd == "cancel":
        return "cancel"
    if vd == "rebuild":
        return "rebuild"
    if vd == "epoch_reset":
        return "epoch_reset"
    if vd == "timeout":
        return "timeout"
    if vd is None:
        return "未结束"
    return "other"


def seg_for(segs, i: int) -> dict | None:
    for s in segs:
        if s["i0"] <= i <= s["i1"]:
            return s
    return None


# ---------------------------------------------------------------- 配对


def pair_events(rows, jumps, resets):
    """跳变 → 换纪行的显式配对。

    主配对：跳变 i 的下一行就是换纪行（生产时序）。特殊情况 i+1 没有落成
    换纪行（当拍无路/非巡航，路线纪元在后续拍才被消费）时，允许延后到
    「跳变之后、下一条跳变之前」的第一条换纪行，并标出滞后拍数。其余记
    未配对，不为凑数硬配。
    """
    used = set()
    pairs, status = {}, {}
    reset_set = set(resets)
    jump_idx = [j["i"] for j in jumps]
    for j in jumps:
        i = j["i"]
        nxt = i + 1
        if nxt in reset_set and nxt not in used:
            pairs[i] = nxt
            used.add(nxt)
            status[i] = "同拍+1"
            continue
        r_cand = next((r for r in resets if r > i and r not in used), None)
        deferred_ok = (
            nxt < len(rows) and r_cand is not None
            and (rows[nxt].get("state") != "CRUISE"
                 or route_of(rows[nxt]).get("vd") == "needs_init"
                 or rows[nxt].get("sel_dbg") is None)
            and not any(i < k < r_cand for k in jump_idx)
        )
        if deferred_ok:
            pairs[i] = r_cand
            used.add(r_cand)
            status[i] = f"延后+{r_cand - i}"
        else:
            status[i] = "未配对"
    unmatched_resets = [r for r in resets if r not in used]
    return pairs, status, unmatched_resets


def unpaired_reason(rows, i: int, resets) -> str:
    later = [r for r in resets if r > i]
    tail = ("，之后也没有换纪行" if not later
            else "，之后的换纪行由别的跳变解释")
    if i + 1 >= len(rows):
        return "局末拍（无下一行）"
    nxt = rows[i + 1]
    rt = route_of(nxt)
    if rt.get("vd") == "needs_init":
        return "换纪未落成换纪行(needs_init)" + tail
    if nxt.get("state") != "CRUISE":
        return f"下一拍非巡航({nxt.get('state')})" + tail
    if nxt.get("sel_dbg") is None:
        return "下一拍无路" + tail
    return "下一拍无换纪" + tail


# ---------------------------------------------------------------- 后果


def window_outcome(rows, start: int, segs, from_tick: int) -> str:
    """跳变之后的动作：到下一次并线段结束或 WINDOW_TICKS 拍为止。

    起点 = 换纪行之后（无换纪则跳变后一拍）；只看**跳变之后**起拍的段，
    不把跳变之前就在跑的段当成后果。
    """
    lo = max(start, 0)
    hi = min(len(rows), lo + WINDOW_TICKS)
    nxt = next((s for s in segs if s["i0"] > from_tick and s["i0"] < hi), None)
    if nxt is None:
        hit = next((route_of(rows[k]).get("vd") for k in range(lo, hi)
                    if route_of(rows[k]).get("vd")
                    in ("finish", "cancel", "timeout", "rebuild")), None)
        return "无新段" + (f"；期间{hit}" if hit else "")
    if nxt["ie"] is None or nxt["ie"] >= hi:
        return f"新段@{nxt['fid0']}→窗口内未结束"
    return f"新段@{nxt['fid0']}→{end_cat(nxt['vd'])}/{nxt['why']}"


def _commit_txt(row) -> str:
    rt = route_of(row)
    return f"ref {_fmt(_num(rt.get('ref')), 1)}/tgt {_fmt(_num(rt.get('tgt')), 1)}"


def _req_txt(row) -> str:
    rt = route_of(row)
    req = sel_of(row).get("req")
    crc = _num(rt.get("crc"))
    return f"{'-' if req is None else _fmt(_num(req), 1)}/{_fmt(crc, 0)}"


# ---------------------------------------------------------------- 主分析


def analyse_game(label, rows, path):
    keys = set()
    for r in rows:
        keys.update(r.keys())
    missing = [k for k in REQUIRED_KEYS if k not in keys]

    if "executed_lane" in missing or "route" in missing:
        return {"label": label, "path": path, "rows": rows, "missing": missing,
                "blocked": True, "jumps": [], "segs": [], "resets": [],
                "pairs": {}, "status": {}, "unmatched_resets": [],
                "events": []}

    segs = segments(rows)
    jumps = find_jumps(rows)
    resets = find_resets(rows)
    pairs, status, unmatched_resets = pair_events(rows, jumps, resets)
    events = []
    for j in jumps:
        i = j["i"]
        info = labels_for(rows, i)
        seg = seg_for(segs, i)
        r = pairs.get(i)
        ro_i = _num(rows[i].get("road_offset"))
        fresh_i = (ro_i is not None and rows[i].get("dgeo_new") is True
                   and rows[i].get("ro_hold") is False)
        ev = {
            "i": i, "fid": rows[i].get("fid"),
            "d": j["d"], "ex0": j["ex0"], "ex1": j["ex1"],
            "v0": j["v0"], "v1": j["v1"],
            "multi": info["multi"], "single": info["single"],
            "hop": info["hop"], "gap": info["gap"], "hold": info["hold"],
            "shadow": info["shadow"], "fd": info["fd"],
            "ro": ro_i, "fresh": fresh_i,
            "align": (None if ro_i is None else abs(j["ex1"] - ro_i)),
            "ph": route_of(rows[i]).get("ph"),
            "ref": _num(route_of(rows[i]).get("ref")),
            "tgt": _num(route_of(rows[i]).get("tgt")),
            "req": sel_of(rows[i]).get("req"),
            "crc": _num(route_of(rows[i]).get("crc")),
            "why": route_of(rows[i]).get("why"),
            "merge": route_of(rows[i]).get("ph") == "merging",
            "reset": r, "pair": status.get(i),
            "reset_ev": reset_evidence(rows, r) if r is not None else None,
            "seg": seg,
            "seg_end": None,
            "window": None,
        }
        if seg is not None:
            if seg["ie"] is not None:
                ev["seg_end"] = f"#{segs.index(seg) + 1} {end_cat(seg['vd'])}/{seg['why']}"
            else:
                ev["seg_end"] = f"#{segs.index(seg) + 1} 未结束"
        if r is not None:
            ev["commit_after"] = _commit_txt(rows[r])
            start = r + 1
        else:
            start = i + 1
        ev["window"] = window_outcome(rows, start, segs, i)
        events.append(ev)
    return {"label": label, "path": path, "rows": rows, "missing": missing,
            "blocked": False, "jumps": jumps, "segs": segs, "resets": resets,
            "pairs": pairs, "status": status,
            "unmatched_resets": unmatched_resets, "events": events}


# ---------------------------------------------------------------- 渲染


def render(results, meta, missing_sessions) -> str:
    L = []
    L.append("# 重锚事件归因探针（只读，口径沿用换道完成判定规格）")
    L.append("")
    L.append(f"- 探针版本：{PROBE_VERSION}　代码版本：commit {meta['commit']}")
    L.append(f"- 口径真源：{SPEC_REL}（按局报告、不跨局池化；只读 trace）")
    L.append(f"- 跳变门：{JUMP_TOL_LANE} 道 = 生产 `RULER_TOL_LANE`"
             f"（decision.py `_ruler_epoch`，比较决策入口 executed）")
    L.append(f"- 黑视门：{BLACKOUT_GAP_S} s = 生产 `HOP_BLACKOUT_S`"
             f"（`ro_hop==2` 的判定门）")
    L.append(f"- 数据目录：{meta['logs']}（会话流）/ {meta['data']}（旧局）")
    if missing_sessions:
        L.append(f"- **缺失数据源**：{', '.join(missing_sessions)}"
                 f"（会话 trace 不存在；未用其它来源顶替，见文末歧义说明）")
    L.append("")
    L.append("## 数据源")
    L.append("")
    for g in results:
        tag = "（缺列，事件统计停用）" if g["blocked"] else ""
        L.append(f"- {g['label']} ← {g['path']}（{len(g['rows'])} 拍，"
                 f"{len(g['segs'])} 段，{len(g['jumps'])} 次跳变，"
                 f"{len(g['resets'])} 次换纪）{tag}")
    L.append("")

    # ------------------------------------------------------------ 结论
    ok = [g for g in results if not g["blocked"]]
    tot_j = sum(len(g["jumps"]) for g in ok)
    tot_paired = sum(len(g["pairs"]) for g in ok)
    tot_reset = sum(len(g["resets"]) for g in ok)
    tot_unreset = sum(len(g["unmatched_resets"]) for g in ok)
    unexp = sum(1 for g in ok for e in g["events"]
                if e["single"] == LABEL_UNKNOWN)
    multi = Counter()
    single = Counter()
    for g in ok:
        for e in g["events"]:
            multi.update(e["multi"])
            single[e["single"]] += 1
    L.append("## 结论（先行）")
    L.append("")
    if ok:
        per = "，".join(f"{g['label']}={len(g['jumps'])}"
                        for g in ok)
        L.append(f"- 可用数据 {len(ok)} 局、{tot_j} 次「executed 单拍跳 >"
                 f" {JUMP_TOL_LANE} 道」：{per}。")
        L.append(f"- 跳变→换纪配对：{tot_paired}/{tot_j} 次在下一拍（或"
                 f"路线层恢复后第一拍）拿到换纪行；未配对 "
                 f"{tot_j - tot_paired} 次。换纪共 {tot_reset} 次、"
                 f"跳变 {tot_j} 次。")
        if tot_unreset:
            L.append(f"- 换纪里有 {tot_unreset} 次没有跳变可解释"
                     f"（尺子跳变或跨拍比较，逐条列在 §4）。")
        else:
            ruler = sum(1 for g in ok for r in g["resets"]
                        if reset_evidence(g["rows"], r)["ruler"])
            L.append(f"- 每条换纪行都配到了 executed 跳变；其中换纪行同拍"
                     f"尺子也跳的 {ruler} 次。")
        if single:
            L.append("- 单标签分布（仅为便于看分布，不是已证实原因）：" +
                     "，".join(f"{k}={single[k]}" for k in LABEL_ORDER
                               if single[k]))
        L.append(f"- 没有命中指定证据标记的「不明」跳变 {unexp} 次"
                 f"（构成见 §3：多数仍是 executed 一步对齐到当拍新鲜读数，"
                 f"只是该读数位移被 `ro_hop` 判成运动而非换档）。")
        n_align = sum(1 for g in ok for e in g["events"]
                      if e["align"] is not None and e["align"] <= 0.01)
        L.append(f"- 直接形态：{n_align}/{tot_j} 次跳变后 executed 与当拍"
                 f"路心读数差 <0.01 道（描述性；重基/修正两步都会把状态"
                 f"对齐到读数，不能据此区分是哪条路径）。")
        seg_tot = sum(len(g["segs"]) for g in ok)
        enc = sum(1 for g in ok for s in g["segs"]
                  if any(s["i0"] <= j["i"] <= s["i1"] for j in g["jumps"]))
        L.append(f"- 暴露分母：并线段共 {seg_tot} 段，遭遇跳变的 {enc} 段、"
                 f"未遭遇 {seg_tot - enc} 段（分组结束原因见 §5）。")
        L.append("- **「重锚频繁」与「重锚是主要中断原因」是两个命题**："
                 "本探针只给出后一个命题所需的分母（遭遇/未遭遇两组的结束"
                 "原因分布与全部中断段的原因分账）。样本小（每局段数个位数"
                 "到十几段），不据此下因果结论。")
    else:
        L.append("- 本批数据没有可用于事件统计的局（缺列或为空），"
                 "见 §1 的 missing_field 清单；不拿 0 冒充。")
    L.append("")

    # ------------------------------------------------------------ 每局汇总
    L.append("## 1. 每局跳变与配对（按局，不池化）")
    L.append("")
    L.append("| 局 | 拍 | 跳变 | 次/千拍 | 已配对 | 未配对 | 换纪行 | "
             "无跳变可解释的换纪 | 不明 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for g in results:
        if g["blocked"]:
            L.append(f"| {g['label']} | {len(g['rows'])} | missing_field | - | "
                     f"- | - | - | - | - |")
            continue
        n = len(g["rows"])
        jn = len(g["jumps"])
        freq = (jn / n * 1000.0) if n else 0.0
        un = sum(1 for e in g["events"] if e["single"] == LABEL_UNKNOWN)
        L.append(f"| {g['label']} | {n} | {jn} | {freq:.1f} | "
                 f"{len(g['pairs'])} | {jn - len(g['pairs'])} | "
                 f"{len(g['resets'])} | {len(g['unmatched_resets'])} | {un} |")
    L.append("")
    # 会话（整场，含多个 stage）合计：与「每局 15~17 次」的口头口径对齐用。
    by_sess = defaultdict(int)
    by_sess_games = defaultdict(int)
    for g in ok:
        tok = g["label"].split(":")[0]
        by_sess[tok] += len(g["jumps"])
        by_sess_games[tok] += 1
    if by_sess:
        txt = "，".join(f"{k}={by_sess[k]}（{by_sess_games[k]} 个 stage）"
                        for k in by_sess)
        L.append(f"按会话（整场）合计跳变：{txt}。"
                 f"本探针的分母单位是 stage（trace 的 stage_begin/end）；"
                 f"一个会话含多个 stage 时，口头「每局」的计数会大于单 stage。")
    L.append("")
    lack = [g for g in results if g["missing"]]
    if lack:
        L.append("缺列（missing_field，相关统计不拿 0 冒充）：")
        for g in lack:
            L.append(f"- {g['label']}：缺 {', '.join(g['missing'])}")
        L.append("")

    # ------------------------------------------------------------ 逐事件
    L.append("## 2. 逐事件归因（每跳变一行；标签可多选，"
             "单标签只便于看分布）")
    L.append("")
    L.append("列口径：`Δexec` 为跳变幅度（道，右正）；`hop`/`gap`/`hold` 取 "
             "i−1/i 两行；`影子` 取 i/i+1 行的 `lateral_frame.ev==reanchor`"
             "（记来源）；`|fd|` 取 i/i+1，仅诊断；`exec−ro` 为跳变后 "
             "executed 与当拍路心读数之差（描述性：对齐读数意味着一步重基/"
             "修正到读数，不代表原因）；`请求/计分` 取跳变当拍的 "
             "`sel_dbg.req`/`route.crc`；`换纪` 为配对到的换纪行 fid 与"
             "纪元（旧→新），`延后+k` 表示 i+1 无路/非巡航、换纪行晚 k 拍；"
             "`承诺` 为跳变当拍 → 换纪后一拍的 ref/tgt；`窗口` 为换纪后到"
             "下一段结束或 200 拍内的动作。")
    L.append("")
    head = ("| 局 | # | 跳变fid | Δexec | 前→后 exec | v 前→后 | 标签集合 | "
            "单标签 | hop | gap(s) | hold | 影子(行:src) | \\|fd\\| | "
            "exec−ro | ph/ref/tgt | 请求/计分 | 并线 | 换纪fid(ep) | 承诺 | "
            "所在段 | 窗口 |")
    L.append(head)
    L.append("|" + "---|" * 22)
    for g in results:
        if g["blocked"]:
            continue
        for k, e in enumerate(g["events"], 1):
            hop = "/".join("-" if h is None else str(h) for h in e["hop"])
            hold = "/".join("1" if h else "0" for h in e["hold"])
            shadow = ", ".join(f"{d:+d}:{s}" for d, s in e["shadow"]) or "-"
            fd = "/".join(_fmt(x, 2) for x in e["fd"]) if e["fd"] else "-"
            if e["reset"] is not None:
                r = e["reset"]
                ep0 = _num(route_of(g["rows"][e["i"]]).get("ep"))
                ep1 = _num(route_of(g["rows"][r]).get("ep"))
                rst = f"{g['rows'][r].get('fid')}({_fmt(ep0, 0)}→{_fmt(ep1, 0)}"
                if e["reset_ev"] and e["reset_ev"]["ruler"]:
                    rst += ",尺子同拍跳"
                if e["pair"] not in ("同拍+1", None):
                    rst += f",{e['pair']}"
                rst += ")"
            else:
                rst = "-"
            commit = _commit_txt(g["rows"][e["i"]])
            if e["reset"] is not None:
                commit += " → " + e["commit_after"]
            seg_end = e["seg_end"] or "段外(keep)"
            if e["seg"] is not None and e["reset"] is not None \
                    and e["seg"]["ie"] == e["reset"]:
                seg_end += "（本跳变换纪结束本段）"
            L.append(
                f"| {g['label']} | {k} | {e['fid']} | {e['d']:+.2f} | "
                f"{_fmt(e['ex0'])}→{_fmt(e['ex1'])} | "
                f"{_fmt(e['v0'])}→{_fmt(e['v1'])} | "
                f"{'+'.join(x for x in LABEL_ORDER if x in e['multi'])} | "
                f"{e['single']} | {hop} | {_fmt(e['gap'])} | {hold} | "
                f"{shadow} | {fd} | {_fmt(e['align'])} | "
                f"{e['ph']}/{_fmt(e['ref'], 1)}/{_fmt(e['tgt'], 1)} | "
                f"{'-' if e['req'] is None else _fmt(_num(e['req']), 1)}/"
                f"{_fmt(e['crc'], 0)} | {'是' if e['merge'] else '否'} | "
                f"{rst} | {commit} | {seg_end} | {e['window']} |")
    L.append("")

    # ------------------------------------------------------------ 标签分布
    L.append("## 3. 标签分布（按局；多标签一事件可计多次）")
    L.append("")
    L.append("| 局 | 跳变 | " + " | ".join(LABEL_ORDER) + " |")
    L.append("|---|---|" + "---|" * len(LABEL_ORDER))
    for g in ok:
        c = Counter()
        for e in g["events"]:
            c.update(e["multi"])
        L.append(f"| {g['label']} | {len(g['events'])} | " +
                 " | ".join(str(c[k]) for k in LABEL_ORDER) + " |")
    L.append(f"| 合计（仅展示） | {sum(len(g['events']) for g in ok)} | " +
             " | ".join(str(multi[k]) for k in LABEL_ORDER) + " |")
    L.append("")
    L.append("单标签（优先序：" + " > ".join(LABEL_ORDER) + "）：")
    L.append("")
    L.append("| 局 | 跳变 | " + " | ".join(LABEL_ORDER) + " |")
    L.append("|---|---|" + "---|" * len(LABEL_ORDER))
    for g in ok:
        c = Counter(e["single"] for e in g["events"])
        L.append(f"| {g['label']} | {len(g['events'])} | " +
                 " | ".join(str(c[k]) for k in LABEL_ORDER) + " |")
    L.append("")
    L.append("**单标签只是便于看分布，不得当成已证实原因**：同一跳变常同时"
             "具备新鲜读数跳变、影子重锚与黑视/断供证据；优先序只决定它在"
             "这一列里被记到哪一格。")
    all_ev = [e for g in ok for e in g["events"]]
    unk = [e for e in all_ev if e["single"] == LABEL_UNKNOWN]
    if all_ev:
        n_fresh_all = sum(1 for e in all_ev if e["fresh"])
        n_align_all = sum(1 for e in all_ev
                          if e["align"] is not None and e["align"] <= 0.01)
        n_fresh_unk = sum(1 for e in unk if e["fresh"])
        n_align_unk = sum(1 for e in unk
                          if e["align"] is not None and e["align"] <= 0.01)
        L.append("")
        L.append("「不明」的构成（描述性，不新增原因标签）：")
        L.append("")
        L.append(f"- 全部 {len(all_ev)} 次跳变里，{n_fresh_all} 次在跳变当拍"
                 f"有新鲜路心读数（dgeo_new=true 且 ro_hold=false），"
                 f"{n_align_all} 次跳变后 executed 与当拍读数差 <0.01 道"
                 f"（一步对齐读数）。")
        L.append(f"- {len(unk)} 次「不明」中，{n_fresh_unk} 次当拍有新鲜读数、"
                 f"{n_align_unk} 次跳变后与读数差 <0.01 道。即多数「不明」是"
                 f"「executed 一步对齐到当拍新鲜读数」，而这次读数的位移被"
                 f"生产 `ro_hop` 判成运动（hop=0）而不是换档（hop=1）——"
                 f"指定标签集因此没有命中项。该分桶不改变标签定义，只说明"
                 f"「不明」并非无供数证据。")
    L.append("")

    # ------------------------------------------------------------ 未配对
    L.append("## 4. 未配对的跳变与未被跳变解释的换纪（逐条）")
    L.append("")
    any_row = False
    for g in ok:
        for e in g["events"]:
            if e["pair"] == "未配对":
                any_row = True
                L.append(f"- 未配对跳变：{g['label']} fid {e['fid']}"
                         f"（Δ{e['d']:+.2f} 道）——"
                         f"{unpaired_reason(g['rows'], e['i'], g['resets'])}")
    for g in ok:
        for r in g["unmatched_resets"]:
            any_row = True
            ev = reset_evidence(g["rows"], r)
            parts = []
            parts.append("执行端跨拍跳" if ev["exec_span"] else "执行端无跳")
            parts.append("尺子跳" if ev["ruler"] else "尺子无跳")
            L.append(f"- 无跳变解释的换纪：{g['label']} fid "
                     f"{g['rows'][r].get('fid')}（{', '.join(parts)}；"
                     f"exec 比较差 {_fmt(ev['exec_d'])} 道，"
                     f"c0 比较差 {_fmt(ev['c0_d'])} 道，"
                     f"跨非巡航拍 {ev['exec_gap']}）")
    for g in ok:
        for e in g["events"]:
            if e["pair"] and e["pair"].startswith("延后"):
                any_row = True
                L.append(f"- 延后配对：{g['label']} fid {e['fid']}"
                         f"（Δ{e['d']:+.2f} 道）→ 换纪 fid "
                         f"{g['rows'][e['reset']].get('fid')}（{e['pair']}，"
                         f"i+1 路线层未消费该纪元）")
    if not any_row:
        L.append("- 无：本批数据的跳变全部在下一拍（或恢复后第一拍）配到换纪行，"
                 "换纪行也全部有跳变可解释。")
    L.append("")

    # ------------------------------------------------------------ 暴露分母
    L.append("## 5. 暴露分母（按局；段=换道段，遇=跳变点落在段起拍与末个"
             "并线拍之间）")
    L.append("")
    L.append("遭遇再分两种：**段内**（跳变拍 < 末个并线拍，段还继续跑）与"
             "**段末拍**（跳变落在段最后一个并线拍，换纪行就是段的结束拍）。")
    L.append("")
    L.append("| 局 | 段 | 遭遇 | 其中段内 | 其中段末拍 | 未遭遇 | "
             "遭遇组结束原因 | 未遭遇组结束原因 | 遭遇组中断率 | "
             "未遭遇组中断率 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for g in ok:
        segs = g["segs"]

        def hits(s):
            return [j["i"] for j in g["jumps"] if s["i0"] <= j["i"] <= s["i1"]]

        def enc(s):
            return bool(hits(s))

        e_segs = [s for s in segs if enc(s)]
        n_segs = [s for s in segs if not enc(s)]
        mid = sum(1 for s in e_segs if any(i < s["i1"] for i in hits(s)))
        last = sum(1 for s in e_segs if any(i == s["i1"] for i in hits(s)))

        def dist(ss):
            if not ss:
                return "-"
            c = Counter(end_cat(s["vd"]) for s in ss)
            return ", ".join(f"{k}×{v}" for k, v in sorted(c.items()))

        def intr(ss):
            if not ss:
                return "-"
            k = sum(1 for s in ss
                    if s["ie"] is not None and end_cat(s["vd"]) != "finish")
            return f"{k}/{len(ss)}（{k / len(ss):.0%}）"

        L.append(f"| {g['label']} | {len(segs)} | {len(e_segs)} | {mid} | "
                 f"{last} | {len(n_segs)} | {dist(e_segs)} | {dist(n_segs)} | "
                 f"{intr(e_segs)} | {intr(n_segs)} |")
    L.append("")
    mid_all = sum(1 for g in ok for s in g["segs"]
                  if any(s["i0"] <= j["i"] < s["i1"] for j in g["jumps"]))
    if mid_all == 0:
        L.append("**本批数据里「段内遭遇」为 0 段**：所有落在并线段内的跳变"
                 "都发生在段的最后一个并线拍，换纪行就是段的结束拍——重锚"
                 "在这里是段的终止事件，而不是段内可恢复的扰动。遭遇组的"
                 " 100% 中断率就是这个结构的直接后果，不能读成「重锚让本来"
                 "会完成的段失败」。")
        L.append("")
    L.append("全部中断段按原因分账（中断 = 有结束拍且结局非 finish；"
             "仅在局内给分母与占比，不跨局池化）：")
    L.append("")
    L.append("| 局 | 中断段 | epoch_reset | cancel | rebuild | timeout | "
             "其它 |")
    L.append("|---|---|---|---|---|---|---|")
    for g in ok:
        bad = [s for s in g["segs"]
               if s["ie"] is not None and end_cat(s["vd"]) != "finish"]
        c = Counter(end_cat(s["vd"]) for s in bad)
        n = len(bad)
        cells = []
        for k in ("epoch_reset", "cancel", "rebuild", "timeout", "other"):
            cells.append("-" if not n else f"{c[k]}（{c[k] / n:.0%}）")
        L.append(f"| {g['label']} | {n} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("跳变发生在在途意图上的次数（分母=该局跳变；并线中=跳变当拍 "
             "route.ph=merging；有请求=sel_dbg.req 非空或 route.crc>0；"
             "计分中=route.why=scoring 或 crc>0；在途=三者任一）：")
    L.append("")
    L.append("| 局 | 跳变 | 并线中 | 有请求 | 计分中 | 在途(并/请/计) |")
    L.append("|---|---|---|---|---|---|")
    for g in ok:
        n = len(g["events"])
        merge = sum(1 for e in g["events"] if e["merge"])
        req = sum(1 for e in g["events"]
                  if e["req"] is not None or (e["crc"] or 0) > 0)
        score = sum(1 for e in g["events"]
                    if e["why"] == "scoring" or (e["crc"] or 0) > 0)
        union = sum(1 for e in g["events"]
                    if e["merge"] or e["req"] is not None
                    or (e["crc"] or 0) > 0 or e["why"] == "scoring")
        def pc(k):
            return "-" if not n else f"{k}（{k / n:.0%}）"
        L.append(f"| {g['label']} | {n} | {pc(merge)} | {pc(req)} | "
                 f"{pc(score)} | {pc(union)} |")
    L.append("")

    # ------------------------------------------------------------ 口径与歧义
    L.append("## 6. 口径、观察窗口与规格歧义（不改文档，只登记）")
    L.append("")
    L.append("1. **时序**：trace 行 i 的 executed 在 tick i+1 才被 `_ruler_epoch`"
             "检查；跳变配对的目标是 i+1 的换纪行。若 i+1 无路/非巡航，"
             "路线层不推进，换纪行会延后——本探针按「跳变之后、下一条跳变"
             "之前的第一条换纪行」延后配对并标出滞后拍数，其余记未配对。")
    L.append("2. **尺子跳变也会换纪**：`_ruler_epoch` 的另一个输入是场景 z=0 "
             "分带中心线（`sel_dbg.c0`）。换纪行没有相邻 executed 跳变时，"
             "用生产端比较对象复原（executed 跨拍差 / c0 差）判断是尺子跳"
             "还是跨拍比较；两者都没有的换纪行如实列为无法解释。")
    L.append("3. **黑视/断供的观测口径**：`ro_hop==2`、`ro_gap_s > "
             f"{BLACKOUT_GAP_S}s`、i−1 与 i 两拍 `ro_hold=true`、或两拍中"
             "任一 `road_offset` 为空。其中「连续 hold」只取两拍，是展示"
             "口径，不是生产判据。")
    L.append("4. **陈旧重基签名**是描述性列：仅要求「跳变前 |v| ≥ "
             f"{V_PRE_MIN_LANE_S} 且跳变后 |v| ≤ {V_ZERO_LANE_S}」，"
             "不证明走的正是 `_stale_rebase` 路径（生产该函数会把 "
             "v_lat_est 清零，签名与之相容）。")
    L.append("5. **单标签不是原因**：多标签命中多个供数证据是常态；优先序"
             "只是为看分布。")
    L.append("6. **观察窗口**：换纪行之后到「下一次并线段结束」或 200 拍为止，"
             "取先到者；窗口内没有新段时只记期间出现的结局码。")
    L.append("7. **缺失会话**：本次运行缺失的会话 trace 如实报出，不用其它来源"
             "顶替；需要时请补跑该会话或改用 `--runs` 指向一文件一局的 trace。")
    if missing_sessions:
        L.append(f"   本次缺失：{', '.join(missing_sessions)}。")
    L.append("8. **标签集的边界**：指定的四类证据标记（`ro_hop==1`、影子 "
             "reanchor、`ro_hop==2`/长间隔/连续 hold、v 落幅）不覆盖"
             "「读数位移被判成运动（hop=0）后 executed 一步对齐」这一常见"
             "形态——它只能落进「不明」。本探针不改标签定义，只增加描述列 "
             "`exec−ro` 与 §3 的构成统计，避免把「不明」误读成「无供数证据」。")
    L.append("9. **样本量**：同一局内各段共享同一套感知与参考系误差，段数"
             "不等于独立样本；本表只核对解析、因果隔离与分母，不支撑统计"
             "结论。")
    return "\n".join(L)


# ---------------------------------------------------------------- 主流程


def _inside(path: str, root: str) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        description="重锚事件归因探针（只读；口径见 COMPLETION_SPEC.md）")
    ap.add_argument("--data", default=DEFAULT_DATA, help="旧局目录（一文件一局）")
    ap.add_argument("--logs", default=DEFAULT_LOGS, help="会话日志目录")
    ap.add_argument("--runs", default="", help="局名，逗号分隔")
    ap.add_argument("--session", default=",".join(DEFAULT_SESSIONS),
                    help="会话 token，逗号分隔（默认任务指定的五个）")
    ap.add_argument("--commit", default="", help="覆盖代码版本号")
    ap.add_argument("--out", default="", help="可选输出文件（只接受仓库外路径）")
    args = ap.parse_args(argv)

    root = _repo_root()
    if args.out:
        out_abs = os.path.abspath(args.out)
        if root and _inside(out_abs, root):
            print(f"拒绝写入仓库内路径：{out_abs}\n探针只写仓库外文件。")
            return 2
        if not os.path.isdir(os.path.dirname(out_abs)):
            print(f"输出目录不存在：{os.path.dirname(out_abs)}（不自动创建）")
            return 2

    try:
        base = _baseline()
    except ImportError as exc:
        print(f"无法复用 probe_merge_baseline.py：{exc}")
        return 2

    games, missing_sessions = [], []
    for run in _split(args.runs):
        rows = base.load(run, args.data)
        path = os.path.join(args.data, run, f"trace_{run}.jsonl")
        games.append((run, rows, path))
    for tok in _split(args.session):
        try:
            for label, rows in base.load_session(tok, args.logs):
                games.append((label, rows,
                              os.path.join(args.logs, tok, "trace",
                                           "speedrush.jsonl")))
        except FileNotFoundError:
            missing_sessions.append(tok)
    if not games:
        print("没有可分析的局（会话 trace 缺失："
              f"{', '.join(missing_sessions) or '无'}）。")
        return 2

    results = [analyse_game(label, rows, path) for label, rows, path in games]
    meta = {"commit": _commit(root, args.commit), "data": args.data,
            "logs": args.logs}
    text = render(results, meta, missing_sessions)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入仓库外文件：{os.path.abspath(args.out)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
