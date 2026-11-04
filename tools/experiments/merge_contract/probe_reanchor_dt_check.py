# -*- coding: utf-8 -*-
"""换纪门的长帧反事实探针（只读，版本 reanchor-dt-check/1）。

**它回答什么**：路线层换纪门 `DecisionEngine._ruler_epoch` 比较决策入口的
``executed`` 与上一拍值，单拍位移 > ``RULER_TOL_LANE``（0.5 道）就 +1 纪元，
**不看 dt**。本探针在 trace 的 ``executed_lane`` 序列上用同一判据找事件，逐事件
列出 dt、位移、隐含速度、生产重锚标签与独立参考系证据，再给出两种反事实
（速度归一 / 长帧屏蔽）在各档下会抑制哪些事件——**只给数字，不选阈值、不改
控制**。

**不新造判定门**：0.5 道取生产常量 ``RULER_TOL_LANE``；跳变检测与「读数到位
提前量」直接复用同目录 ``probe_reanchor_attribution.py``（±0.3 道 / 200 拍口径），
会话切局复用 ``probe_merge_baseline.load_session``。反事实里的 V / D 是扫描
档位，不是建议值。

**独立参考系证据**（逐条布尔，不合并成分数）：
1. ``ro_source`` 在跳变拍 ±3 拍内发生变化；
2. 新鲜读数在 ±3 拍内出现 ≥0.5 道的跳变（``ro_hop==1``，或相邻两个新鲜读数
   之差 ≥0.5 道）；
3. 影子 ``lateral_frame.ev == "reanchor"`` 出现在 i 或 i+1。

**时序说明**：trace 行 i 的 ``executed_lane`` 到 tick i+1 才被 ``_ruler_epoch``
消费；本探针的反事实针对**这次位移本身**（行 i），不按 i+1 的换纪行配对。

用法：
  .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_reanchor_dt_check.py
  ... --session 20261028_083410_1,20261028_171845_1
  ... --logs <会话日志目录>
  ... --out <仓库外路径>

输出：默认只打印 stdout；``--out`` 只接受仓库外路径。缺列记 ``missing_field``，
不拿 0 冒充；只读 trace。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

PROBE_VERSION = "reanchor-dt-check/1"

# 跳变门 = 生产 DecisionEngine.RULER_TOL_LANE（decision.py `_ruler_epoch`）。
JUMP_TOL_LANE = 0.5
# 独立证据的观察半窗（拍）：±3。
EVID_WINDOW = 3
# 新鲜读数跳变的幅度门（道）：与「半道」同源，不另立数值。
READ_JUMP_TOL_LANE = 0.5
# 反事实扫描档位（不是建议值，也不代表任何一档被推荐）。
V_GRID = (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0)
D_GRID = (0.15, 0.2, 0.25, 0.3, 0.4)
# 反例分档（任务口径）：长帧 = dt>0.3，正常帧 = dt≤0.2。
LONG_DT_S = 0.3
NORMAL_DT_S = 0.2

DEFAULT_SESSIONS = (
    "20261028_083410_1",
    "20261028_171845_1",
    "20261028_175557_1",
)

# 事件表需要的列（缺列 ≠ 缺值：缺列记 missing_field）
REQUIRED_KEYS = (
    "fid", "executed_lane", "dt", "ts_ns", "road_offset", "ro_source",
    "ro_hop", "ro_hold", "dgeo_new", "lateral_frame", "sel_dbg", "route",
    "lat_ctrl",
)


# ---------------------------------------------------------------- 基础工具


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")


def _default_logs() -> str:
    return os.environ.get(
        "SPEEDRUSH_LOGS",
        os.path.join(_appdata(), "MaaRacingMaster", "logs"))


def _repo_root() -> str | None:
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _commit(root: str | None) -> str:
    if root is None:
        return "unknown"
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 —— 版本号取不到不阻断分析
        return "unknown"


def _sibling(filename: str, modname: str):
    """按路径加载同目录探针（实验区脚本不靠包导入）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(modname, path)
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


def _pct(vals, q: float):
    """最近秩分位（小样本不插值）。"""
    if not vals:
        return None
    s = sorted(vals)
    k = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, k))]


def _rank(v: float, vals) -> float | None:
    """v 在 vals 中的分位位置（0~1，含并列按 ≤v 的比例）。"""
    if not vals:
        return None
    n = len(vals)
    return sum(1 for x in vals if x <= v) / n


# ---------------------------------------------------------------- 读数与证据


def is_fresh(row: dict) -> bool:
    """新鲜路心读数：有值、dgeo_new=true、未保持。"""
    return (_num(row.get("road_offset")) is not None
            and row.get("dgeo_new") is True
            and row.get("ro_hold") is False)


def prev_fresh(rows, i: int) -> int | None:
    """i 之前（不含）最近的新鲜读数行。"""
    for k in range(i - 1, -1, -1):
        if is_fresh(rows[k]):
            return k
    return None


def source_changes(rows, i: int, win: int = EVID_WINDOW) -> list[dict]:
    """证据①：±win 拍内 ro_source 的变化（跳过空值行，按出现顺序成对比较）。"""
    lo, hi = max(0, i - win), min(len(rows), i + win + 1)
    seq = [(k, rows[k].get("ro_source")) for k in range(lo, hi)
           if rows[k].get("ro_source") is not None]
    out = []
    for (k0, s0), (k1, s1) in zip(seq, seq[1:]):
        if s0 != s1:
            out.append({"fid": rows[k1].get("fid"), "from": s0, "to": s1,
                        "dt_tick": k1 - k0})
    return out


def reading_jumps(rows, i: int, win: int = EVID_WINDOW) -> list[dict]:
    """证据②：±win 拍内的新鲜读数跳变（ro_hop==1 或相邻新鲜读数差 ≥0.5 道）。"""
    lo, hi = max(0, i - win), min(len(rows), i + win + 1)
    out = []
    for k in range(lo, hi):
        if _num(rows[k].get("ro_hop")) == 1:
            out.append({"kind": "hop", "fid": rows[k].get("fid"),
                        "d": None, "gap_tick": None})
    fresh = [k for k in range(lo, hi) if is_fresh(rows[k])]
    for a, b in zip(fresh, fresh[1:]):
        dd = rows[b]["road_offset"] - rows[a]["road_offset"]
        if abs(dd) >= READ_JUMP_TOL_LANE:
            out.append({"kind": "fresh", "fid": rows[b].get("fid"),
                        "d": dd, "gap_tick": b - a})
    return out


def dt_of(rows, i: int):
    """本拍 dt；缺失/非正时用 ts_ns 差分并注明来源。"""
    d = _num(rows[i].get("dt"))
    if d is not None and d > 0:
        return d, "dt"
    a = _num(rows[i - 1].get("ts_ns"))
    b = _num(rows[i].get("ts_ns"))
    if a is not None and b is not None and b > a:
        return (b - a) / 1e9, "ts_ns"
    return None, "缺"


# ---------------------------------------------------------------- 事件


def analyse_game(label, rows, path, attr, present):
    keys = present
    blocked = ("executed_lane" not in keys)
    jumps = [] if blocked else attr.find_jumps(rows)
    events = []
    for j in jumps:
        i = j["i"]
        d = j["d"]
        dt, dt_src = dt_of(rows, i)
        ta = _num(rows[i - 1].get("ts_ns"))
        tb = _num(rows[i].get("ts_ns"))
        dt_ts = (tb - ta) / 1e9 if (ta is not None and tb is not None) else None
        info = attr.labels_for(rows, i)
        row, nxt = rows[i], rows[i + 1] if i + 1 < len(rows) else None
        pf = prev_fresh(rows, i)
        ro_i = _num(row.get("road_offset"))
        ro_pf = _num(rows[pf].get("road_offset")) if pf is not None else None
        src = source_changes(rows, i)
        rj = reading_jumps(rows, i)
        lf = attr.lf_of(row)
        lf_n = attr.lf_of(nxt) if nxt is not None else {}
        shadow_hit = [(k, attr.lf_of(rows[k]).get("ev")) for k in (i, i + 1)
                      if k < len(rows) and attr.lf_of(rows[k]).get("ev")
                      == "reanchor"]
        rt = attr.route_of(row)
        sel = attr.sel_of(row)
        rebase_col = ("lat_ctrl" in keys
                      and any("rebase" in (r.get("lat_ctrl") or {})
                              for r in rows))
        ev = {
            "i": i, "fid": row.get("fid"),
            "d": d, "dt": dt, "dt_src": dt_src, "dt_ts": dt_ts,
            "v": None if dt is None else abs(d) / dt,
            "prod": (row.get("lat_ctrl") or {}).get("rebase")
            if rebase_col else "缺列",
            "rebase_col": rebase_col,
            "ro": ro_i, "fresh": is_fresh(row),
            "align": None if ro_i is None else abs(j["ex1"] - ro_i),
            "src": row.get("ro_source"), "hop": _num(row.get("ro_hop")),
            "pf": None if pf is None else rows[pf].get("fid"),
            "pf_gap": None if pf is None else i - pf,
            "d_ro": None if (ro_i is None or ro_pf is None) else ro_i - ro_pf,
            "settled": info["settled"], "shadow": shadow_hit,
            "v_lat": info["v"],
            "lf_i": lf.get("ev"), "lf_n": lf_n.get("ev"),
            "ph": rt.get("ph"), "vd": rt.get("vd"), "why": rt.get("why"),
            "crc": _num(rt.get("crc")), "req": sel.get("req"),
            "merge": rt.get("ph") == "merging",
            "ev_src": src, "ev_read": rj,
            "ev1": bool(src), "ev2": bool(rj), "ev3": bool(shadow_hit),
        }
        ev["ev_any"] = ev["ev1"] or ev["ev2"] or ev["ev3"]
        ev["ev_n"] = int(ev["ev1"]) + int(ev["ev2"]) + int(ev["ev3"])
        events.append(ev)
    return {"label": label, "path": path, "rows": rows, "present": keys,
            "blocked": blocked, "events": events}


# ---------------------------------------------------------------- 反事实


def scan(events):
    """C0/C1/C2 各档的触发/抑制集合。

    C0：|Δ| > 0.5 → 触发（事件定义本身，全触发）。
    C1：|Δ|/dt > V 才触发；无 dt 的事件记「无法判速」，保守算作仍触发。
    C2：dt > D 时不允许仅凭位移触发；无 dt 的不屏蔽。
    """
    c1 = []
    for v in V_GRID:
        keep = [e for e in events if e["v"] is None or e["v"] > v]
        cut = [e for e in events if e["v"] is not None and e["v"] <= v]
        c1.append({"V": v, "keep": keep, "cut": cut})
    c2 = []
    for dd in D_GRID:
        cut = [e for e in events if e["dt"] is not None and e["dt"] > dd]
        keep = [e for e in events if e not in cut]
        c2.append({"D": dd, "keep": keep, "cut": cut})
    return {"c0_keep": list(events), "c1": c1, "c2": c2}


def speed_samples(games):
    """正常拍横速样本：A=全部 dt>0 拍，B=非事件拍，C=非事件且 dt≤0.2。"""
    a, b, c = [], [], []
    jump_idx = {(g["label"], e["i"]) for g in games for e in g["events"]}
    for g in games:
        if g["blocked"]:
            continue
        rows = g["rows"]
        for i in range(1, len(rows)):
            e0 = _num(rows[i - 1].get("executed_lane"))
            e1 = _num(rows[i].get("executed_lane"))
            dt, _src = dt_of(rows, i)
            if e0 is None or e1 is None or dt is None or dt <= 0:
                continue
            v = abs(e1 - e0) / dt
            a.append(v)
            if (g["label"], i) in jump_idx:
                continue
            b.append(v)
            if dt <= NORMAL_DT_S:
                c.append(v)
    return {"A": a, "B": b, "C": c}


def over_speed(vals, v):
    n = sum(1 for x in vals if x > v)
    return n, (n / len(vals) if vals else None)


def dt_consistency(games):
    """`dt` 字段与 `ts_ns` 差分的一致性（用于「dt 来源」那条口径）。"""
    n = bad = 0
    worst = 0.0
    for g in games:
        if g["blocked"]:
            continue
        rows = g["rows"]
        for i in range(1, len(rows)):
            d = _num(rows[i].get("dt"))
            a, b = _num(rows[i - 1].get("ts_ns")), _num(rows[i].get("ts_ns"))
            if d is None or a is None or b is None:
                continue
            n += 1
            diff = abs(d - (b - a) / 1e9)
            worst = max(worst, diff)
            if diff > 1e-3:
                bad += 1
    return n, bad, worst


# ---------------------------------------------------------------- 渲染


def _src_txt(ev, n: int = 2) -> str:
    if not ev["ev_src"]:
        return "无"
    return ", ".join(f"{c['from']}→{c['to']}@{c['fid']}"
                     for c in ev["ev_src"][:n])


def _read_txt(ev, n: int = 3) -> str:
    if not ev["ev_read"]:
        return "无"
    parts = []
    for c in ev["ev_read"][:n]:
        parts.append(f"hop1@{c['fid']}" if c["kind"] == "hop"
                     else f"Δ{c['d']:+.2f}@{c['fid']}")
    return ", ".join(parts)


def _shadow_txt(ev) -> str:
    if not ev["ev3"]:
        return "无"
    return ", ".join(("i" if kk == ev["i"] else "i+1") + ":" + str(vv)
                     for kk, vv in ev["shadow"])


def render(games, meta, missing_sessions) -> str:
    L = []
    ev_all = [e for g in games if not g["blocked"] for e in g["events"]]
    long_ev = [e for e in ev_all if e["dt"] is not None and e["dt"] > LONG_DT_S]
    norm_ev = [e for e in ev_all if e["dt"] is not None and e["dt"] <= NORMAL_DT_S]
    sc = scan(ev_all)
    samp = speed_samples(games)
    dtc = dt_consistency(games)
    ev_dt_bad = sum(1 for e in ev_all
                    if e["dt"] is not None and e["dt_ts"] is not None
                    and abs(e["dt"] - e["dt_ts"]) > 1e-3)

    L.append("# 换纪门长帧反事实探针（只读；不选阈值、不改控制）")
    L.append("")
    L.append(f"- 探针版本：{PROBE_VERSION}　代码版本：commit {meta['commit']}")
    L.append(f"- 数据目录：{meta['logs']}（会话流，按 stage 切局）")
    L.append(f"- 会话：{', '.join(meta['sessions']) or '-'}"
             + (f"（缺失：{', '.join(missing_sessions)}）"
                if missing_sessions else ""))
    L.append(f"- 跳变门：{JUMP_TOL_LANE} 道 = 生产 `RULER_TOL_LANE`"
             "（`_ruler_epoch` 比较决策入口 executed，单拍位移、不看 dt）")
    L.append(f"- 反事实档位：C1 速度 V ∈ {', '.join(f'{v:g}' for v in V_GRID)}"
             f" 道/s；C2 长帧 D ∈ {', '.join(f'{d:g}' for d in D_GRID)} s")
    L.append(f"- 分档：长帧 dt>{LONG_DT_S:g}s、正常帧 dt≤{NORMAL_DT_S:g}s；"
             f"独立证据观察窗 ±{EVID_WINDOW} 拍")
    L.append("")
    L.append("**速览（只给数字）**："
             f"事件 {len(ev_all)} 次（C0 全触发）；长帧事件 {len(long_ev)} 次，"
             "其中证据②（读数跳）"
             f"{sum(1 for e in long_ev if e['ev2'])} 次、证据③（影子重锚）"
             f"{sum(1 for e in long_ev if e['ev3'])} 次；正常帧事件 {len(norm_ev)} "
             "次，其中既无生产标签也无②③证据 "
             f"{sum(1 for e in norm_ev if (e['prod'] is None or e['prod'] == '缺列') and not e['ev2'] and not e['ev3'])}"
             " 次（生产标签缺列的局单列）。")
    L.append("")

    # ------------------------------------------------------------ 数据源
    L.append("## 数据源与缺列")
    L.append("")
    for g in games:
        lack = [k for k in REQUIRED_KEYS if k not in g["present"]]
        tag = "（缺 executed_lane，事件统计停用）" if g["blocked"] else ""
        L.append(f"- {g['label']} ← {g['path']}（{len(g['rows'])} 拍，"
                 f"{len(g['events'])} 次跳变）{tag}")
        if lack:
            L.append(f"  - missing_field：{', '.join(lack)}")
    for g in games:
        if g["blocked"]:
            continue
        if "lat_ctrl" in g["present"] and not any(
                "rebase" in (r.get("lat_ctrl") or {}) for r in g["rows"]):
            L.append(f"- {g['label']}：`lat_ctrl` 存在但无 `rebase` 字段——"
                     "生产重锚标签记「缺列」，不用旁证顶替。")
    L.append("")

    # ------------------------------------------------------------ ① 逐事件
    L.append("## ① 逐事件表")
    L.append("")
    L.append("口径：`dt` 取本拍 `dt` 字段（与 `ts_ns` 差分一致时记 `dt`，否则"
             "注明来源）；`|Δ|` = 相邻行 `executed_lane` 差（道）；"
             "`v=`|Δ|/dt（道/s）；`生产` = 本拍 `lat_ctrl.rebase`（缺列记"
             "「缺列」、未重基记 `-`）；`ro` 为本拍 `road_offset`（非新鲜拍是"
             "保持值，`新鲜=否`）；`Δro` 相对**上一新鲜读数**；`提前量` = "
             "跳变前有多少个新鲜读数已在终值 ±0.3 道内（沿用既有探针口径，"
             "按新鲜读数计、不是按拍计）；`|exec−ro|` = 跳变后 executed 与"
             "当拍读数之差（≈0 表示这一步落在读数上）；`v_lat前→后` = 模型"
             "横速在跳变前/后的值（道/s，描述性，用于识别「陈旧重基」签名："
             "前明显非零、后落到 ~0）。")
    L.append("")
    head = ("| 局 | # | 跳变fid | dt(s) | 来源 | \\|Δ\\| | v(道/s) | 生产 | "
            "ro(道) | 新鲜 | ro_source | 上新鲜fid | Δro | 间隔(拍) | ro_hop | "
            "影子ev(i/i+1) | 提前量 | \\|exec−ro\\| | v_lat前→后 |")
    L.append(head)
    L.append("|" + "---|" * 19)
    for g in games:
        if g["blocked"]:
            continue
        for k, e in enumerate(g["events"], 1):
            hop = ("-" if e["hop"] is None
                   else str(int(e["hop"])))
            if not e["fresh"] and e["hop"] is None:
                hop = "n/a"
            if "ro_hop" not in g["present"]:
                hop = "缺列"
            src = "缺列" if "ro_source" not in g["present"] else (e["src"] or "-")
            ro = "缺列" if "road_offset" not in g["present"] else _fmt(e["ro"], 3)
            dro = "缺列" if "road_offset" not in g["present"] else _fmt(e["d_ro"], 2)
            pf = "缺列" if "road_offset" not in g["present"] else (
                "-" if e["pf"] is None else str(e["pf"]))
            fresh_txt = ("缺列" if not {"dgeo_new", "ro_hold", "road_offset"}
                         <= g["present"] else ("是" if e["fresh"] else "否"))
            lf_txt = ("缺列" if "lateral_frame" not in g["present"]
                      else f"{e['lf_i'] or '-'}/{e['lf_n'] or '-'}")
            dts = _fmt(e["dt"], 3) if e["dt"] is not None else "缺"
            vlat = ("缺列" if "v_lat_est" not in g["present"]
                    else f"{_fmt(e['v_lat'][0], 2)}→{_fmt(e['v_lat'][1], 2)}")
            L.append(
                f"| {g['label']} | {k} | {e['fid']} | {dts} | {e['dt_src']} | "
                f"{abs(e['d']):.2f} | {_fmt(e['v'], 2)} | "
                f"{e['prod'] if e['prod'] is not None else '-'} | {ro} | "
                f"{fresh_txt} | {src} | {pf} | {dro} | "
                f"{'-' if e['pf_gap'] is None else e['pf_gap']} | {hop} | "
                f"{lf_txt} | {e['settled']} | "
                f"{'缺列' if 'road_offset' not in g['present'] else _fmt(e['align'], 2)} | "
                f"{vlat} |")
    L.append("")

    L.append("控制上下文（跳变当拍）与独立参考系证据（逐条布尔，不合并成分数）：")
    L.append("")
    L.append("证据①=`ro_source` ±3 拍内变化；②=新鲜读数 ±3 拍内跳 ≥0.5 道"
             "（`ro_hop==1` 或相邻新鲜读数差）；③=影子 `lateral_frame.ev` 在 "
             "i/i+1 为 `reanchor`。`在途` = 并线中/有请求/计分中三者任一"
             "（位序固定为 并·请·计，`·` 表示该位不成立）。")
    L.append("")
    n1 = sum(1 for e in ev_all if e["ev1"])
    n2 = sum(1 for e in ev_all if e["ev2"])
    n3 = sum(1 for e in ev_all if e["ev3"])
    L.append(f"证据命中率（分母 {len(ev_all)} 次事件）：① {n1} 次、② {n2} 次、"
             f"③ {n3} 次。**读表时先看这一行**：① 只要 `ro_source` 在 ±3 拍内"
             "换过一次就命中，而换源在本批 trace 里是常态——它对单个事件几乎"
             "没有判别力；②③ 才是「读数本身翻面」的旁证。")
    L.append("")
    head2 = ("| 局 | # | 跳变fid | ph/vd/why/crc | req | 并线 | 在途 | 证据①换源 | "
             "证据②读数跳 | 证据③影子 | 独立证据 |")
    L.append(head2)
    L.append("|" + "---|" * 11)
    for g in games:
        if g["blocked"]:
            continue
        for k, e in enumerate(g["events"], 1):
            if "route" in g["present"]:
                ctx = (f"{e['ph']}/{e['vd']}/{e['why']}/"
                       f"{'-' if e['crc'] is None else format(e['crc'], 'g')}")
                why = e["why"]
                crc = e["crc"]
            else:
                ctx, why, crc = "缺列", None, None
            req = ("缺列" if "sel_dbg" not in g["present"] else
                   ("-" if e["req"] is None else _fmt(_num(e["req"]), 1)))
            merge = ("缺列" if "route" not in g["present"]
                     else ("是" if e["merge"] else "否"))
            in_flight = ("并" if e["merge"] else "·") + \
                ("请" if e["req"] is not None else "·") + \
                ("计" if (crc or 0) > 0 or why == "scoring" else "·")
            e1_txt = ("缺列" if "ro_source" not in g["present"] else _src_txt(e))
            e2_txt = ("缺列" if not {"road_offset", "ro_hop", "dgeo_new",
                                     "ro_hold"} <= g["present"]
                      else _read_txt(e))
            e3_txt = ("缺列" if "lateral_frame" not in g["present"]
                      else _shadow_txt(e))
            L.append(
                f"| {g['label']} | {k} | {e['fid']} | {ctx} | {req} | "
                f"{merge} | {in_flight} | {e1_txt} | {e2_txt} | "
                f"{e3_txt} | {e['ev_n']}/3 |")
    L.append("")

    # ------------------------------------------------------------ ② 反事实
    L.append("## ② 反事实敏感性（C0/C1/C2 各档抑制哪些事件）")
    L.append("")
    L.append("### C0 现状")
    L.append("")
    L.append("| 局 | 拍 | 事件 | C0 触发 | 触发率 |")
    L.append("|---|---|---|---|---|")
    for g in games:
        if g["blocked"]:
            L.append(f"| {g['label']} | {len(g['rows'])} | missing_field | - | - |")
            continue
        n = len(g["events"])
        L.append(f"| {g['label']} | {len(g['rows'])} | {n} | {n} | "
                 f"{'100%' if n else '-'} |")
    L.append(f"| 合计 | {sum(len(g['rows']) for g in games if not g['blocked'])} "
             f"| {len(ev_all)} | {len(sc['c0_keep'])} | "
             f"{'100%' if ev_all else '-'} |")
    L.append("")

    L.append("### C1 速度归一（`|Δ|/dt > V` 才触发）")
    L.append("")
    L.append("| V(道/s) | 触发 | 抑制 | 其中带②或③证据 | 被抑制的跳变 fid |")
    L.append("|---|---|---|---|---|")
    for r in sc["c1"]:
        fids = ", ".join(str(e["fid"]) for e in r["cut"]) or "—"
        L.append(f"| {r['V']:g} | {len(r['keep'])} | {len(r['cut'])} | "
                 f"{sum(1 for e in r['cut'] if e['ev2'] or e['ev3'])} | {fids} |")
    L.append("")
    if sc["c1"]:
        tail = sc["c1"][1:]  # V ≥ 第二档
        same_tail = all([e["fid"] for e in r["cut"]]
                        == [e["fid"] for e in tail[0]["cut"]] for r in tail)
        if same_tail and len(sc["c1"]) > 1:
            first = [e["fid"] for e in sc["c1"][0]["cut"]]
            rest = [e["fid"] for e in tail[0]["cut"]]
            extra = [f for f in rest if f not in first]
            L.append("**网格基本是平的**：" +
                     f"V≥{sc['c1'][1]['V']:g} 的每一档抑制的都是同一批事件"
                     "（" + ", ".join(str(f) for f in rest) + "）" +
                     (f"；V={sc['c1'][0]['V']:g} 只比它少 "
                      f"{', '.join(str(f) for f in extra)}（v 擦线保住）。"
                      if first != rest else "。") +
                     "即这批数据只能分出「v≈2 道/s」与「v 远高于网格上限」"
                     "两群，档位再细也没有信息；数字本身不指向任何一档。")
        else:
            L.append("各档抑制集合不同，逐档见上表；每档只代表一个候选，"
                     "不是推荐值。")
    L.append("")
    L.append("每个事件保住触发所需的 V：判据是 `v > V`，故**只要 V < v 就保住**，"
             "绑定量是事件自身的 v；下表给 v 与「保住触发的最高一档 V」"
             "（网格内）。")
    L.append("")
    L.append("| 局 | # | 跳变fid | v(道/s) | 保住触发的最高 V 档 | 首个抑制档 |")
    L.append("|---|---|---|---|---|---|")
    for g in games:
        if g["blocked"]:
            continue
        for k, e in enumerate(g["events"], 1):
            if e["v"] is None:
                L.append(f"| {g['label']} | {k} | {e['fid']} | 无法判速 | "
                         "无法判速（不抑制） | — |")
                continue
            keep = [v for v in V_GRID if e["v"] > v]
            cut = [v for v in V_GRID if e["v"] <= v]
            keep_txt = f"{max(keep):g}" if keep else "无（最低档也抑制）"
            cut_txt = f"{min(cut):g}" if cut else "无（最高档也保住）"
            L.append(f"| {g['label']} | {k} | {e['fid']} | {e['v']:.2f} | "
                     f"{keep_txt} | {cut_txt} |")
    L.append("")

    L.append("### C2 长帧屏蔽（`dt > D` 时不允许仅凭位移触发）")
    L.append("")
    L.append("| D(s) | 抑制 | 其中带②或③证据（可被证据放行） | 被抑制的跳变 fid |")
    L.append("|---|---|---|---|")
    for r in sc["c2"]:
        fids = ", ".join(f"{e['fid']}(dt {e['dt']:.3f})" for e in r["cut"]) or "—"
        L.append(f"| {r['D']:g} | {len(r['cut'])} | "
                 f"{sum(1 for e in r['cut'] if e['ev2'] or e['ev3'])} | {fids} |")
    L.append("")
    if sc["c2"] and all([e["fid"] for e in r["cut"]]
                        == [e["fid"] for e in sc["c2"][0]["cut"]]
                        for r in sc["c2"][1:]):
        dts = sorted(e["dt"] for e in ev_all if e["dt"] is not None)
        below = [d for d in dts if d <= max(D_GRID)]
        L.append("**网格是平的**：0.15~0.4 s 五档抑制的是同一批事件。原因是"
                 f"本批事件 dt 分布断层——最大的非长帧事件只有 "
                 f"{_fmt(max(below), 3) if below else '-'} s，长帧事件最小 "
                 f"{_fmt(min((e['dt'] for e in long_ev), default=None), 3)} s，"
                 "中间没有任何样本能把档位分开。D 取 0.15 还是 0.4，在这批"
                 "数据上结果完全相同；这不构成对任一档的支持。")
    L.append("")

    L.append("### 正常拍横速分布（`|Δexec|/dt`，道/s）")
    L.append("")
    L.append("A=全部 `dt>0` 拍（含事件拍，任务口径）；B=非事件拍；C=非事件且 "
             "`dt≤0.2`。B/C 用来排除事件自身对分布的污染。")
    L.append("")
    L.append("| 集合 | 拍 | p50 | p90 | p99 | max |")
    L.append("|---|---|---|---|---|---|")
    for key, name in (("A", "A 全部 dt>0"), ("B", "B 非事件"), ("C", "C 非事件 dt≤0.2")):
        vals = samp[key]
        L.append(f"| {name} | {len(vals)} | {_fmt(_pct(vals, 0.5), 3)} | "
                 f"{_fmt(_pct(vals, 0.9), 3)} | {_fmt(_pct(vals, 0.99), 3)} | "
                 f"{_fmt(max(vals) if vals else None, 2)} |")
    L.append("")
    L.append("各 V 档下「会被判超速」（`v > V`）的拍数与占比：")
    L.append("")
    L.append("| V(道/s) | A 超速拍 | A 占比 | B 超速拍 | B 占比 | C 超速拍 | C 占比 |")
    L.append("|---|---|---|---|---|---|---|")
    for v in V_GRID:
        na, ra = over_speed(samp["A"], v)
        nb, rb = over_speed(samp["B"], v)
        nc, rc = over_speed(samp["C"], v)
        f = lambda x: "-" if x is None else f"{x:.2%}"  # noqa: E731
        L.append(f"| {v:g} | {na} | {f(ra)} | {nb} | {f(rb)} | {nc} | {f(rc)} |")
    L.append("")

    # ------------------------------------------------------------ ③ 反例
    L.append("## ③ 反例与边界")
    L.append("")
    L.append(f"### 反例 1：长帧事件（dt>{LONG_DT_S:g}s）里有没有独立参考系证据")
    L.append("")
    if not long_ev:
        L.append("- 本批数据没有长帧事件。")
    else:
        for e in long_ev:
            g = next(x for x in games if e in x["events"])
            L.append(
                f"- {g['label']} fid {e['fid']}：dt={_fmt(e['dt'], 3)} s、"
                f"|Δ|={abs(e['d']):.2f} 道、v={_fmt(e['v'], 2)} 道/s、"
                f"生产={e['prod'] if e['prod'] is not None else '-'}、"
                f"新鲜={'是' if e['fresh'] else '否'}、"
                f"ro={_fmt(e['ro'], 3)}({e['src']})、"
                f"证据①={'有' if e['ev1'] else '无'}（{_src_txt(e)}）、"
                f"证据②={'有' if e['ev2'] else '无'}（{_read_txt(e)}）、"
                f"证据③={'有' if e['ev3'] else '无'}（{_shadow_txt(e)}）")
        l2 = sum(1 for e in long_ev if e["ev2"])
        l3 = sum(1 for e in long_ev if e["ev3"])
        l1 = sum(1 for e in long_ev if e["ev1"])
        aligned = [e for e in long_ev
                   if e["align"] is not None and e["align"] <= 0.05]
        L.append("")
        L.append(f"结论：长帧 {len(long_ev)} 次事件里，证据② {l2} 次、证据③ {l3} 次、"
                 f"证据① {l1} 次（① 在全批 {len(ev_all)} 次事件里命中 {n1} 次，"
                 "本身没有判别力）。三个附加读法：")
        L.append("")
        L.append("读法 A：这几次的「读数到位提前量」分别是 "
                 + "、".join(str(e["settled"]) for e in long_ev)
                 + " 个新鲜读数——读数在跳变前就已在终值附近稳定，跳变当拍"
                 "并没有读数翻面。")
        L.append("读法 B：跳变后 `|exec−ro|` 分别为 "
                 + "、".join(_fmt(e["align"], 2) for e in long_ev)
                 + f" 道，落在读数上（≤0.05 道）的 {len(aligned)} 次"
                 + ("（fid " + ", ".join(str(e["fid"]) for e in aligned) + "）"
                    if aligned else "")
                 + "，其余落点离冻结读数还有半道以上。")
        L.append("读法 C：`Δro`（相对上一新鲜读数，可跨多拍）分别为 "
                 + "、".join(_fmt(e["d_ro"], 2) for e in long_ev)
                 + " 道——读数自上一新鲜值以来没有动过，当拍也没有读数位移。")
        if aligned:
            L.append("")
            parts = []
            for e in aligned:
                v0, v1 = e["v_lat"]
                tag = []
                if v0 is not None and v1 is not None:
                    tag.append(f"v_lat {v0:+.2f}→{v1:+.2f}")
                    if abs(v0) >= 0.2 and abs(v1) <= 0.02:
                        tag.append("陈旧重基签名")
                    if v0 * e["d"] < 0:
                        tag.append("位移与跳变前运动反向")
                parts.append(f"fid {e['fid']}"
                             + (f"（{'，'.join(tag)}）" if tag else ""))
            word = "这一次" if len(aligned) == 1 else "这几次"
            L.append("其中 " + "、".join(parts) + f"。{word}的落点与读数完全重合"
                     "（|exec−ro| ≤0.05 道），即状态一步重锚到读数，而不是"
                     "模型积出来的连续横移。**屏蔽长帧会把这一次重锚动作一起"
                     "挡掉**（若翻面发生在更早的拍，它本来就是迟到的纠正），"
                     f"代价是本批 {len(long_ev)} 次里 {len(aligned)} 次；"
                     "另几次没有这个签名。")
        L.append("")
        loss = ("，但会丢 " + "、".join(f"fid {e['fid']}" for e in aligned)
                + " 那次真实的道号重对齐") if aligned else ""
        L.append("因此：本批长帧事件**在跳变当拍都没有读数翻面的证据**"
                 "（②③皆 0，且读数已稳定若干新鲜读数）——挡掉它们不会丢一个"
                 "「当拍发生的翻面」" + loss + "。"
                 "反过来，它们是否属于「翻面其实发生在"
                 "更早的拍、状态迟到才追上」的情形，trace 单独判不了；若属"
                 "后者，换纪门在这些事件上本来就只是迟到响应，挡不挡都要在更早"
                 "的拍上解决。也不能据此判它们就是「长帧误触发」：trace 无法"
                 "区分「车在长帧里真的连续横移」与「状态在长帧后一步换位」。")
    L.append("")

    L.append(f"### 反例 2：正常帧事件（dt≤{NORMAL_DT_S:g}s）里既无生产标签、"
             "也无独立证据的「疑似误触发」")
    L.append("")
    strict = [e for e in norm_ev if e["prod"] is None and not e["ev_any"]]
    loose = [e for e in norm_ev
             if (e["prod"] is None or e["prod"] == "缺列")
             and not e["ev2"] and not e["ev3"]]
    if not ev_all:
        L.append("- 无事件可判。")
    else:
        L.append(f"- 按三条证据的原口径（任一命中即算证据）："
                 f"{len(strict)} 次。")
        L.append(f"- 但证据①在 {n1}/{len(ev_all)} 次事件里都命中，等于常数；"
                 f"把①剔除、只认②③，并把生产标签缺列也算「无标签」后，"
                 f"**{len(loose)} 次**正常帧事件属于这一类：")
        L.append("")
        for e in loose:
            g = next(x for x in games if e in x["events"])
            L.append(f"  - {g['label']} fid {e['fid']}：dt={_fmt(e['dt'], 3)} s、"
                     f"|Δ|={abs(e['d']):.2f} 道、v={_fmt(e['v'], 2)} 道/s、"
                     f"生产={e['prod'] if e['prod'] is not None else '-'}、"
                     f"新鲜={'是' if e['fresh'] else '否'}、"
                     f"ro_source={e['src']}、Δro(上新鲜)={_fmt(e['d_ro'], 2)}、"
                     f"读数间隔={'-' if e['pf_gap'] is None else e['pf_gap']} 拍、"
                     f"影子={e['lf_i'] or '-'}/{e['lf_n'] or '-'}、"
                     f"提前量={e['settled']}")
        L.append("")
        n_settled = sum(1 for e in loose if e["settled"] >= 1)
        v_lo = min((e["v"] for e in loose if e["v"] is not None), default=None)
        v_hi = max((e["v"] for e in loose if e["v"] is not None), default=None)
        gap_jump = [e for e in loose
                    if e["d_ro"] is not None and abs(e["d_ro"]) >= 0.5]
        aligned_all = [e for e in loose
                       if e["align"] is not None and e["align"] <= 0.05]
        L.append(f"读法：这 {len(loose)} 次里 {n_settled} 次的「读数到位提前量」"
                 "≥1 个新鲜读数——读数早就在终值上，位移是**状态追读数**，不是"
                 "读数在当拍翻面。它们是不是「误触发」取决于换纪门的语义（道号"
                 "所指是否真的换了一道），trace 单看这些列回答不了；能确定的是："
                 "**只做时间归一挡不住这一类**——它们 dt 正常、v 在 "
                 f"{_fmt(v_lo, 1)}~{_fmt(v_hi, 1)} 道/s，C1 任何一档"
                 f"（≤{max(V_GRID):g}）都会照单放行，C2 也碰不到它们。")
        L.append("")
        L.append(f"补充两条（都是口径边界，不改上面的 ②③ 判定）："
                 f"①这些事件里有 {len(aligned_all)}/{len(loose)} 次跳变后 "
                 "`|exec−ro|`=0.00 道，即这一步落在读数上；"
                 f"②有 {len(gap_jump)} 次的 `Δro`（相对上一新鲜读数）≥0.5 道"
                 + ("（fid " + ", ".join(str(e["fid"]) for e in gap_jump) + "）"
                    if gap_jump else "")
                 + "，但证据②的 ±3 拍窗口没命中——读数只有 2~5 Hz，相邻新鲜"
                 "读数常隔 5~15 拍，±3 拍窗口会漏掉这类跨间隔的读数位移。"
                 "**不要把 ②=0 读成「读数没动」**，要对照 `Δro` 列。")
    L.append("")

    L.append("### 反例 3：事件隐含速度与正常横移速度的对照")
    L.append("")
    L.append("| 局 | 事件 v 中位 | 事件 v 范围 | 非事件 p99 | 非事件 max | "
             "v≤非事件 p99 的事件数 | 事件最小 v 的分位（最接近正常的一次） |")
    L.append("|---|---|---|---|---|---|---|")
    for g in games:
        if g["blocked"] or not g["events"]:
            continue
        evv = [e["v"] for e in g["events"] if e["v"] is not None]
        # 该局的非事件样本
        sv = []
        jumps = {(g["label"], e["i"]) for e in g["events"]}
        rows = g["rows"]
        for i in range(1, len(rows)):
            e0 = _num(rows[i - 1].get("executed_lane"))
            e1 = _num(rows[i].get("executed_lane"))
            dt, _s = dt_of(rows, i)
            if e0 is None or e1 is None or dt is None or dt <= 0:
                continue
            if (g["label"], i) in jumps:
                continue
            sv.append(abs(e1 - e0) / dt)
        p99 = _pct(sv, 0.99)
        ranks = [(_rank(v, sv), v) for v in evv]
        n_le = sum(1 for v in evv if p99 is not None and v <= p99)
        lo = min(ranks, key=lambda t: t[0]) if ranks else (None, None)
        L.append(
            f"| {g['label']} | {_fmt(_pct(evv, 0.5), 2)} | "
            f"{_fmt(min(evv) if evv else None, 2)}~{_fmt(max(evv) if evv else None, 2)} | "
            f"{_fmt(p99, 2)} | {_fmt(max(sv) if sv else None, 2)} | "
            f"{n_le}/{len(evv)} | "
            f"{_fmt(lo[0], 3)}（v={_fmt(lo[1], 2)}） |")
    L.append("")
    if ev_all:
        long_v = [e["v"] for e in long_ev if e["v"] is not None]
        short_v = [e["v"] for e in ev_all
                   if e["v"] is not None and e["dt"] is not None
                   and e["dt"] <= NORMAL_DT_S]
        pooled = samp["B"]
        pct_long = "、".join(f"{v:.2f}（非事件分位 {_fmt(_rank(v, pooled), 2)}）"
                            for v in long_v)
        L.append(f"- 长帧事件 v：{pct_long or '-'} 道/s，"
                 "与正常横移同量级（落在非事件拍 p80~p95 一带）；"
                 f"正常帧事件 v 中位 {_fmt(_pct(short_v, 0.5), 2)}、范围 "
                 f"{_fmt(min(short_v) if short_v else None, 2)}~"
                 f"{_fmt(max(short_v) if short_v else None, 2)} 道/s，"
                 "**比正常横移高一到两个数量级**——那不是车在横移，是状态在"
                 "单拍内换位。两类事件在速度维上是两种东西；C1 之所以只切掉"
                 "长帧那几次，正是因为这个量级差，不是因为档位选得准。")
    L.append("")

    L.append("### 边界与歧义（不改文档，只登记）")
    L.append("")
    L.append("1. **本探针不选阈值、不改控制**。C1/C2 的每一档都只是候选："
             "C1 在 V≥2.5 各档、C2 在全部 D 档抑制的都是同一批事件，"
             "V=2.0 只比 2.5 少切一次（v≈2.01 擦线）；说明**本批数据无法在"
             "这些档位之间做区分**，任何一档都需要新的实跑（更多长帧样本）"
             "才能验证。")
    L.append("2. **dt 的来源**："
             f"全批 {dtc[0]} 拍里 `dt` 与 `ts_ns` 差分差 >1 ms 的有 "
             f"{dtc[1]} 拍（最大 {_fmt(dtc[2], 3)} s）；事件拍里两者差 >1 ms 的 "
             f"{ev_dt_bad}/{len(ev_all)} 次，`来源` 列全部记 `dt`，没有走 "
             "`ts_ns` 回退。回退规则仍保留（缺 dt 时用 ts_ns，表里注明来源）。")
    L.append("3. **长帧事件的位移归属**：`executed` 是 planner 状态，长帧期间"
             "它可能一次补上多拍的累积量；单看 trace 无法区分「车真的横移了 "
             "0.5 道」与「状态在长帧后一步追上读数」。这也是本探针把「新鲜"
             "读数跳变」「影子重锚」单列、而不拿换源当判据的原因。")
    L.append("4. **生产标签只在本拍**：`lat_ctrl.rebase` 记的是 planner 在"
             "该行实际执行的分支；跳变的产生拍与标签拍同为 i（位移发生在"
             "两次 planner 更新之间）。i+1 的标签不复用（那是下一次更新）。")
    L.append(f"5. **证据②的口径**：±{EVID_WINDOW} 拍内只要有 `ro_hop==1`，或任意"
             "两个相邻新鲜读数差 ≥0.5 道即算命中；`ro_hop` 只在新鲜拍存在，"
             "非新鲜拍记 `n/a`，不当 0。**这个窗口对稀疏读数偏紧**：读数只有 "
             "2~5 Hz，相邻新鲜读数常隔 5~15 拍，跨间隔的读数位移会被漏掉——"
             "所以看 ②=0 时必须对照 `Δro` 列（相对上一新鲜读数，可跨多拍）。")
    L.append("6. **证据①的口径**：只比较 ±3 拍内**非空** `ro_source` 的相邻出现；"
             "跨空档的一对也会被记成一次换源，表里给出两行的 fid 供核对。")
    L.append("7. **正常拍分布含事件拍（A）**：任务口径要求「所有 dt>0 拍」，"
             "事件拍会把 A 的 p99/max 抬高；B/C 两列是排除事件后的对照，"
             "判断「阈值该落在哪」应看 B/C。")
    L.append(f"8. **样本量**：{len(games)} 个 stage、{len(ev_all)} 次事件、"
             f"其中长帧 {len(long_ev)} 次；同一会话内各事件共享同一套感知"
             "误差，不是独立样本。本表只够核对解析与分母，不支撑统计结论，"
             "任何一档都只是候选，需新的实跑验证。")
    L.append("")
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
        description="换纪门长帧反事实探针（只读；不选阈值、不改控制）")
    ap.add_argument("--logs", default=_default_logs(), help="会话日志目录")
    ap.add_argument("--session", default=",".join(DEFAULT_SESSIONS),
                    help="会话 token，逗号分隔（默认任务指定的三个）")
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
        base = _sibling("probe_merge_baseline.py", "merge_baseline_probe")
        attr = _sibling("probe_reanchor_attribution.py", "reanchor_attr_probe")
    except ImportError as exc:
        print(f"无法复用同目录探针：{exc}")
        return 2

    games, missing = [], []
    sessions = _split(args.session)
    for tok in sessions:
        path = os.path.join(args.logs, tok, "trace", "speedrush.jsonl")
        try:
            for label, rows in base.load_session(tok, args.logs):
                present = set()
                for r in rows:
                    present.update(r.keys())
                games.append(analyse_game(label, rows, path, attr, present))
        except FileNotFoundError:
            missing.append(tok)
    if not games:
        print("没有可分析的局（会话 trace 缺失："
              f"{', '.join(missing) or '无'}）。")
        return 2

    meta = {"commit": _commit(root), "logs": args.logs, "sessions": sessions}
    text = render(games, meta, missing)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入仓库外文件：{os.path.abspath(args.out)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
