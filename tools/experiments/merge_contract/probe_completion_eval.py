# -*- coding: utf-8 -*-
"""换道完成判定探针（只读，判定器版本 completion-eval/1）。

口径唯一真源：``tools/experiments/merge_contract/COMPLETION_SPEC.md``（下称规格）。
本脚本按规格逐条实现「位置项 + 速度项 + 证据类 → 三值结论」，并单独输出
事后核验列；阈值全部从决策真源读，脚本内不写任何判定数值。

用法：
  .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_completion_eval.py
  .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_completion_eval.py --session <会话 token>
  .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_completion_eval.py --data <debug/speedrush 目录> --runs <局名>
  .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_completion_eval.py --session a,b --out <仓库外路径>

输入两种形态（与 probe_merge_baseline 同款，解析函数直接复用）：
- 旧局：``<data>/<局>/trace_<局>.jsonl``（一文件一局）；
- 会话流：``<logs>/<会话>/trace/speedrush.jsonl``（一文件多局，按 stage 切局）。

判定与核验的边界（规格 §0/§6）：
- 判定只用 ≤ 结束拍的证据；结束拍 = 离开 merging 的那一拍。
- 位置项：``coarse.ego``（路线层内部 ``v_ref`` 的 trace 投影，见 route.tick_route）
  与段目标道之差；证据类按 ``road_offset``/``ro_hold``/``dgeo_new`` 分新鲜/保持。
  结束拍的 ``route.tgt`` 已被清空，目标道取自段内最后一拍 merging 的承诺值。
- 速度项：路线层的 ``v_est_lane_s`` 不落 trace，按 ``(ego_t − ego_{t−1}) / dt``
  复算；证据类取**两端较差**（两端都新鲜才算实测横速）。
- 事后核验：只用结束拍**之后**的拍，优先 ``lateral_frame.x``（只取影子接受
  新鲜观测的拍，排除 reanchor 跳变过渡态；与判定链共享路心读数，属半独立
  估计器），其次 ``lane_obs.x``（异源线检测，已知会离谱，仅作参考）。
  核验列与判定列分开保存，任何方向都不回流。

容差来源（规格 §0.4 / §3）：决策真源 ``resources/policy/decision.json``——
位置容差 = ``[selection].center_band_lane``（route.RouteParams.finish_tol_lane
由 decision.py 从这里取），速度容差 = ``[route].finish_v_tol_lane_s``。
缺键即拒判，不拿默认值顶上。

规格落地时的两处判断（规格未逐字写死，按规格 §3 第 1 行与 §0.2 取严）：
- 「完成」要求位置项是**新鲜**证据且进带：位置只有保持值时，即使实测横速
  落稳也只记 ``unconfirmed``（位置先验不能替代新测量）。
- 段末若 ``route.ep`` 变化（换纪），位置与目标道分属两把尺子、速度差商也跨
  尺子：本段记 ``unconfirmed`` + ``undecidable(epoch_changed_at_end)``。

输出：默认只打印 stdout，不写仓库文件；``--out`` 只接受仓库外路径。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter

JUDGE_VERSION = "completion-eval/1"
SPEC_REL = "tools/experiments/merge_contract/COMPLETION_SPEC.md"
# 判定链所需的列（缺列 ≠ 缺值：缺列记 missing_field，缺值记证据不足）
REQUIRED_KEYS = ("fid", "dt", "route", "coarse", "road_offset", "ro_hold",
                 "dgeo_new")
FRESH_SOURCES = frozenset({"wall", "region", "pair"})
# 核验只认影子**接受新鲜观测**的拍：``reanchor`` 是跳变过渡态（影子按最大
# 校正速率追目标，x 不是当拍测量），``jump`` 已被影子自己判为跳变，两者都不
# 能当核验读数。
VERIFY_EV = frozenset({"accept", "init"})


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
    except Exception:  # noqa: BLE001 —— 版本号取不到不阻断判定
        return "unknown"


def _baseline():
    """按路径加载同目录的 probe_merge_baseline.py，复用其解析/切局逻辑。

    该文件是脚本不是包，走 importlib 按文件加载；只用其中的 ``load`` /
    ``load_session``，不碰它的默认局清单与统计口径。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_merge_baseline.py")
    spec = importlib.util.spec_from_file_location("merge_baseline_probe", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_tolerances(path: str) -> tuple[float, float]:
    """从决策真源读两个完成容差；缺键/不可读即拒判（不写默认值）。"""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path or "<未找到 decision.json>")
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    pos = (d.get("selection") or {}).get("center_band_lane")
    vel = (d.get("route") or {}).get("finish_v_tol_lane_s")
    if not isinstance(pos, (int, float)) or not isinstance(vel, (int, float)):
        raise KeyError(
            f"{path} 缺容差键（selection.center_band_lane="
            f"{pos!r}，route.finish_v_tol_lane_s={vel!r}）——按 fail-loud 拒判")
    return float(pos), float(vel)


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _fmt(x, nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _split(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


# ---------------------------------------------------------------- 数据加载


def discover(data_dir: str, logs_dir: str, root: str | None):
    """列出可用数据源；不臆造路径，只报真实存在的入口。"""
    runs, sessions = [], []
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            if os.path.isfile(os.path.join(
                    data_dir, name, f"trace_{name}.jsonl")):
                runs.append(name)
    if os.path.isdir(logs_dir):
        for name in sorted(os.listdir(logs_dir)):
            if os.path.isfile(os.path.join(
                    logs_dir, name, "trace", "speedrush.jsonl")):
                sessions.append(name)
    repo_logs = os.path.join(root, "logs") if root else ""
    repo_sessions = []
    if repo_logs and os.path.isdir(repo_logs):
        for name in sorted(os.listdir(repo_logs)):
            if os.path.isfile(os.path.join(
                    repo_logs, name, "trace", "speedrush.jsonl")):
                repo_sessions.append(name)
    return runs, sessions, repo_sessions


def load_games(args, base):
    games = []
    for run in _split(args.runs):
        rows = base.load(run, args.data)
        path = os.path.join(args.data, run, f"trace_{run}.jsonl")
        games.append((run, rows, path))
    for tok in _split(args.session):
        path = os.path.join(args.logs, tok, "trace", "speedrush.jsonl")
        for label, rows in base.load_session(tok, args.logs):
            games.append((label, rows, path))
    return games


# ---------------------------------------------------------------- 证据分类


def pos_class(row: dict) -> str:
    """位置证据类（规格 §1/§2）：新鲜 / 保持 / 缺失 / 未知。

    新鲜 = ``dgeo_new=true`` 且 ``ro_hold=false``（同拍消费的新深度证据）；
    无路心读数 = 缺失；有读数但不是新鲜证据 = 保持；列在而值为 null = 未知。
    """
    if row.get("road_offset") is None:
        return "missing"
    if row.get("dgeo_new") is True and row.get("ro_hold") is False:
        return "fresh"
    if row.get("ro_hold") is True or row.get("dgeo_new") is False:
        return "hold"
    return "unknown"


def coarse_ego(row: dict):
    c = row.get("coarse")
    return _num(c.get("ego")) if isinstance(c, dict) else None


def route_of(row: dict) -> dict:
    r = row.get("route")
    return r if isinstance(r, dict) else {}


def _dt_of(row: dict, prev: dict | None):
    """本拍时间步长：优先 trace ``dt``，缺失时用相邻 ts_ns 差分；都不行=缺失。"""
    dt = _num(row.get("dt"))
    if dt is not None and dt > 0:
        return dt, "trace.dt"
    t1, t0 = _num(row.get("ts_ns")), _num((prev or {}).get("ts_ns"))
    if t1 is not None and t0 is not None and t1 > t0:
        return (t1 - t0) / 1e9, "ts_ns 差分"
    return None, "缺失"


def segments(rows) -> list[dict]:
    """按 ``route.ph == merging`` 的连续拍切段；结束拍 = 离开 merging 的那一拍。

    route 为 null 的拍不改判阶段（相位未知），记 ``ph_unknown`` 计数并留在段内
    ——否则一条缺 route 的拍会把一段劈成两段、把分母放大。
    """
    out, cur = [], None
    for i, r in enumerate(rows):
        ro = r.get("route")
        if not isinstance(ro, dict):
            if cur is not None:
                cur["ph_unknown"] += 1
            continue
        if ro.get("ph") == "merging":
            if cur is None:
                cur = {"i0": i, "i1": i, "ie": None, "tgt": ro.get("tgt"),
                       "tgts": [], "eps": [], "ph_unknown": 0}
            cur["i1"] = i
            cur["tgts"].append(ro.get("tgt"))
            cur["eps"].append(ro.get("ep"))
        elif cur is not None:
            cur["ie"] = i
            cur["ep_end"] = ro.get("ep")
            out.append(cur)
            cur = None
    if cur is not None:
        cur["ep_end"] = None
        out.append(cur)      # 段到局末仍未离开 merging：无结束拍
    return out


def segment_identity(seg: dict) -> str | None:
    """段内身份核验：目标道全程不变且非空、参考系纪元不换。"""
    tgts = seg["tgts"]
    if not tgts or tgts[0] is None:
        return "tgt_missing"
    first = tgts[0]
    if any(t != first for t in tgts[1:]):
        return "tgt_changed"
    eps = [e for e in seg["eps"] if e is not None]
    if len(set(eps)) > 1:
        return "epoch_changed_in_segment"
    if seg["ph_unknown"]:
        return "phase_unknown_ticks"
    return None


def judge(rows, seg, tgt, tol_p, tol_v) -> dict:
    """按规格 §3 的表出一段的三值结论（只用 ≤ 结束拍 的证据）。"""
    ie = seg["ie"]
    end = rows[ie] if ie is not None else None
    prev = rows[ie - 1] if (ie is not None and ie > 0) else None
    res = {"pos": None, "pos_class": None, "pos_age": None, "pos_src": None,
           "ref_off": None, "ref": None, "v": None, "v_class": None,
           "v_ages": (None, None), "dt_src": None, "conclusion": "unconfirmed",
           "steady": None, "pos_in": None, "reasons": []}
    if end is None:
        res["reasons"].append("no_end_tick")
        return res

    ro = route_of(end)
    res["ref"] = _num(ro.get("ref"))
    if res["ref"] is not None and tgt is not None:
        res["ref_off"] = res["ref"] - tgt

    ego = coarse_ego(end)
    res["pos"] = ego
    res["pos_class"] = pos_class(end)
    res["pos_age"] = _num(end.get("dgeo_age_ms"))
    res["pos_src"] = end.get("ro_source")
    if ego is None or tgt is None:
        res["conclusion"] = "unconfirmed"
        res["reasons"].append("position_missing")
        return res

    off = ego - tgt
    res["pos_in"] = abs(off) <= tol_p

    # 参考系换纪：结束拍的道号属于新尺子，段目标道属于旧尺子——两者不可比。
    # 换纪拍 route.vd 通常是 epoch_reset，不能拿新人旧人混着判完成。
    seg_eps = [e for e in seg["eps"] if e is not None]
    ep_end = route_of(end).get("ep")
    if seg_eps and ep_end is not None and ep_end != seg_eps[0]:
        res["conclusion"] = "unconfirmed"
        res["pos_in"] = None
        res["reasons"].append("epoch_changed_at_end")
        return res

    prev_ego = coarse_ego(prev) if prev is not None else None
    dt, dt_src = _dt_of(end, prev)
    res["dt_src"] = dt_src
    if prev_ego is None or dt is None:
        res["v_class"] = "missing"
        res["v"] = None
        res["reasons"].append(
            "no_prev_ego" if prev_ego is None else "dt_missing")
    else:
        res["v"] = (ego - prev_ego) / dt
        res["v_ages"] = (_num(prev.get("dgeo_age_ms")),
                         _num(end.get("dgeo_age_ms")))
        cls_cur, cls_prev = pos_class(end), pos_class(prev)
        res["v_class"] = ("measured"
                          if cls_cur == "fresh" and cls_prev == "fresh"
                          else "hold_diff")
        if res["v_class"] == "hold_diff" and "unknown" in (cls_cur, cls_prev):
            res["v_class"] = "hold_diff(unknown_end)"

    if res["v"] is not None:
        res["steady"] = abs(res["v"]) <= tol_v

    # 规格 §3 表的判定顺序（先运动证据，再位置证据）：
    # ① 实测横速未落稳 → 未完成（车还在动，与位置读数无关）；
    # ② 位置新鲜：进带且速度实测落稳 → 完成；未进带 → 未完成；
    # ③ 位置只有保持值或缺失：无论读作带内带外都撑不起结论 → 无法确认
    #    （「当时没有新鲜观测」不等于「未完成」，规格 §0.3）。
    # 预测位置暂不可得：trace 里没有路线帧的预测横位，只认新鲜位置。
    if res["v_class"] == "measured" and not res["steady"]:
        res["conclusion"] = "not_completed"
        res["reasons"].append("still_moving")
    elif res["pos_class"] == "fresh":
        if not res["pos_in"]:
            res["conclusion"] = "not_completed"
            res["reasons"].append("fresh_out_of_band")
        elif res["v_class"] == "measured":
            res["conclusion"] = "completed"
        else:
            res["conclusion"] = "unconfirmed"
            res["reasons"].append("speed_not_measured")
    else:
        res["conclusion"] = "unconfirmed"
        res["reasons"].append("position_not_fresh")
    return res


def validity(seg, jd, missing_cols) -> tuple[str, str]:
    """段的有效性：missing_field（旧 trace 无此列）/ undecidable / valid。"""
    if missing_cols:
        return "missing_field", "缺列:" + ",".join(missing_cols)
    ident = segment_identity(seg)
    if ident:
        return "undecidable", ident
    if seg["ie"] is None:
        return "undecidable", "no_end_tick"
    if jd["pos"] is None or seg["tgt"] is None:
        return "undecidable", "position_value_missing"
    if "epoch_changed_at_end" in jd["reasons"]:
        return "undecidable", "epoch_changed_at_end"
    if "dt_missing" in jd["reasons"]:
        return "undecidable", "time_incomplete"
    if "no_prev_ego" in jd["reasons"]:
        return "undecidable", "prev_tick_missing"
    return "valid", ""


# ---------------------------------------------------------------- 事后核验


def verify_tick(row):
    """取一拍可用的核验横向估计：(来源, 值, 独立度, 年龄ms)。"""
    lf = row.get("lateral_frame")
    if isinstance(lf, dict) and lf.get("x") is not None \
            and lf.get("ev") in VERIFY_EV and lf.get("src") in FRESH_SOURCES:
        return ("lateral_frame.x", float(lf["x"]),
                "半独立(共享路心读数,不同估计器)", _num(lf.get("lane_age")))
    lo = row.get("lane_obs")
    if isinstance(lo, dict) and lo.get("x") is not None \
            and lo.get("state") == "TRACK":
        return ("lane_obs.x", float(lo["x"]),
                "异源(车道线检测,已知会离谱)", _num(lo.get("det_age_ms")))
    return None


def post_verify(rows, end_idx, tgt, tol, ep=None) -> dict:
    """只用结束拍之后拍判断「车是否进入目标带」；无后续证据 = 不可核验。

    窗口 = 结束拍之后到**下一次 merging 起拍或下一次换纪之前**（新段=新的横向
    意图；换纪=道号换了尺子，冻结的目标道失效）；不设时间窗阈值。整段只用
    一路估计（优先 ``lateral_frame.x``，它不在才有 ``lane_obs.x``），另一路
    只用来标注分歧。
    """
    out = {"label": "不可核验", "src": None, "indep": None, "first": None,
           "last": None, "near": None, "alt": None, "reason": None,
           "truncated": False}
    if end_idx is None:
        out["reason"] = "无结束拍"
        return out
    stop = len(rows)
    for j in range(end_idx + 1, len(rows)):
        ro = rows[j].get("route")
        if not isinstance(ro, dict):
            continue
        if ro.get("ph") == "merging":
            stop = j
            break
        if ep is not None and ro.get("ep") not in (None, ep):
            stop = j
            out["truncated"] = True
            break
    ticks, alt_ticks = [], []
    for j in range(end_idx + 1, stop):
        got = verify_tick(rows[j])
        if got is None:
            continue
        rec = (rows[j].get("fid"), got[1] - tgt, got[3], got[0], got[2])
        (ticks if got[0] == "lateral_frame.x" else alt_ticks).append(rec)
    if not ticks and not alt_ticks:
        out["reason"] = ("窗口内无后续拍" if stop == end_idx + 1
                         else "后续拍无可用横向估计")
        return out

    def _first_in(seq):
        for rec in seq:
            if abs(rec[1]) <= tol:
                return rec
        return None

    if ticks:
        src, sel, other = "lateral_frame.x", ticks, alt_ticks
    else:
        src, sel, other = "lane_obs.x", alt_ticks, ticks
    out["src"] = src
    out["indep"] = ("半独立(共享路心读数,不同估计器)" if src == "lateral_frame.x"
                    else "异源(车道线检测,已知会离谱)")
    out["near"] = sel[0]
    out["last"] = sel[-1]
    hit = _first_in(sel)
    if hit is not None:
        out["label"] = "进带"
        out["first"] = hit
    else:
        out["label"] = "未进带"
        hit_alt = _first_in(other)
        if hit_alt is not None:
            out["label"] = "未进带(另源进带)"
            out["alt"] = hit_alt
    return out


def sign_of(off, tol) -> int:
    if off is None:
        return 0
    if abs(off) <= tol:
        return 0
    return 1 if off > 0 else -1


def agreement(jd_off, ver_off, tol) -> str:
    if jd_off is None and ver_off is None:
        return "两缺"
    if jd_off is None:
        return "一缺一有(判定缺)"
    if ver_off is None:
        return "一缺一有(核验缺)"
    a, b = sign_of(jd_off, tol), sign_of(ver_off, tol)
    if a == b:
        return "同号(均带内)" if a == 0 else "同号"
    return "异号(带内/带外)" if (a == 0 or b == 0) else "异号"


# ---------------------------------------------------------------- 主流程


def analyse_game(label, rows, path, tol_p, tol_v):
    keys = set()
    for r in rows:
        keys.update(r.keys())
    missing = [k for k in REQUIRED_KEYS if k not in keys]
    nulls = Counter()
    for r in rows:
        for k in ("route", "coarse", "road_offset"):
            if k in keys and r.get(k) is None:
                nulls[k] += 1

    segs = segments(rows)
    out = []
    for si, seg in enumerate(segs, 1):
        tgt = seg["tgt"]
        jd = judge(rows, seg, tgt, tol_p, tol_v)
        valid, why = validity(seg, jd, missing)

        end = rows[seg["ie"]] if seg["ie"] is not None else None
        ro = route_of(end) if end is not None else {}
        vd, why_end = ro.get("vd"), ro.get("why")
        seg_eps = [e for e in seg["eps"] if e is not None]
        ep_end = ro.get("ep") if end is not None else None
        epok = (not seg_eps or ep_end is None or ep_end == seg_eps[0])
        if seg["ie"] is None or tgt is None:
            ver = {"label": "不可核验", "src": None, "indep": None,
                   "first": None, "last": None, "near": None, "alt": None,
                   "reason": "无结束拍" if seg["ie"] is None else "无目标道",
                   "truncated": False}
        elif not epok:
            ver = {"label": "不可核验", "src": None, "indep": None,
                   "first": None, "last": None, "near": None, "alt": None,
                   "reason": "段末换纪,目标道已失效", "truncated": True}
        else:
            ver = post_verify(rows, seg["ie"], tgt, tol_p, ep_end)
        jd_off = (jd["pos"] - tgt) if (jd["pos"] is not None
                                      and tgt is not None) else None
        ver_off = ver["near"][1] if ver["near"] is not None else None
        agree_cls = agreement(jd_off, ver_off, tol_p)
        agree = agree_cls
        if "epoch_changed_at_end" in jd["reasons"]:
            agree_cls, agree = "不可比(段末换纪)", "不可比(段末换纪)"
        elif ver_off is not None:
            agree += f"(核验near{ver_off:+.2f})"

        fi = end.get("fid") if end is not None else None
        out.append({
            "label": label, "path": path, "idx": si,
            "fid0": rows[seg["i0"]].get("fid") if rows else None,
            "fid1": rows[seg["i1"]].get("fid") if rows else None,
            "fid_end": fi, "ticks": seg["i1"] - seg["i0"] + 1,
            "tgt": tgt, "vd": vd, "why": why_end,
            "jd": jd, "ver": ver, "valid": valid, "why_invalid": why,
            "jd_off": jd_off, "ver_off": ver_off,
            "agree": agree, "agree_cls": agree_cls,
        })
    return {"label": label, "path": path, "rows": rows, "missing": missing,
            "nulls": nulls, "segs": out}


def _ev_txt(cls, age=None, src=None) -> str:
    """证据类+年龄。保持值的 ``dgeo_age`` 只是深度证据年龄，不是保持时长
    （规格 G3：保持时长在 trace 里读不出，只能给下界），故标 d 前缀。"""
    if cls == "fresh":
        return f"fresh@{age:.0f}ms" if age is not None else "fresh"
    if cls in ("hold", "unknown"):
        extra = []
        if src:
            extra.append(str(src))
        if age is not None:
            extra.append(f"d{age:.0f}ms")
        return f"{cls}({','.join(extra)})" if extra else str(cls)
    return str(cls)


def render(games, meta, tol_p, tol_v) -> str:
    L = []
    L.append(f"# 换道完成判定探针（判定器 {JUDGE_VERSION}，只读）")
    L.append("")
    L.append(f"- 口径真源：{SPEC_REL}（未改口径；阈值未标定，不做效果结论）")
    L.append(f"- 代码版本：commit {meta['commit']}　判定器：{JUDGE_VERSION}")
    L.append(f"- 容差来源：{meta['decision']}（位置=selection.center_band_lane，"
             f"速度=route.finish_v_tol_lane_s）")
    L.append(f"- 位置容差：{tol_p:.3f} 道；速度容差：{tol_v:.3f} 道/s")
    L.append(f"- 数据目录：{meta['data']}")
    L.append(f"- 日志目录：{meta['logs']}")
    L.append("")
    L.append("## 数据源")
    L.append("")
    for g in games:
        L.append(f"- {g['label']} ← {g['path']}（{len(g['rows'])} 拍，"
                 f"{len(g['segs'])} 段）")
    L.append("")

    L.append("## ① 逐段判定（每段一行；判定列只用 ≤ 结束拍 证据，"
             "核验列只用结束拍之后拍）")
    L.append("")
    head = ("| 局 | 段 | 段拍 | fid 起→末/结束 | tgt | 结束 vd/why | "
            "ego−tgt | 位置证据 | ref−tgt | v(道/s) | 速度证据 | 结论 | "
            "有效性 | 排除原因 | 核验源 | 首达(+帧) | 末值 | "
            "一致性(判定↔核验最近) |")
    L.append(head)
    L.append("|" + "---|" * 18)
    for g in games:
        for s in g["segs"]:
            jd = s["jd"]
            pos_ev = _ev_txt(jd["pos_class"], jd["pos_age"], jd["pos_src"])
            if jd["v_class"] is None or jd["v_class"] == "missing":
                v_ev = "missing"
            else:
                a0, a1 = jd["v_ages"]
                v_ev = f"{jd['v_class']}({_fmt(a0,0)}/{_fmt(a1,0)}ms)"
            epoch_end = "epoch_changed_at_end" in jd["reasons"]
            pos_cell = "换纪(不可比)" if epoch_end else _fmt(s["jd_off"])
            ref_cell = "换纪" if epoch_end else _fmt(jd["ref_off"])
            v_cell = "换纪" if epoch_end else _fmt(jd["v"])
            if epoch_end:
                v_ev = "换纪(差商跨尺子)"
            if s["ver"]["first"] is not None:
                fid, off, _age, _src, _ind = s["ver"]["first"]
                n = (fid - s["fid_end"]) if (fid is not None
                                             and s["fid_end"] is not None) \
                    else None
                first = f"+{n}帧@fid{fid} off{_fmt(off)}"
            elif s["ver"]["label"].startswith("未进带"):
                first = "未进带"
            else:
                first = "-"
            last = "-"
            if s["ver"]["last"] is not None:
                fid, off, _age, _src, _ind = s["ver"]["last"]
                last = f"fid{fid} off{_fmt(off)}"
            if s["ver"]["src"]:
                ver_src = s["ver"]["src"] + (
                    "·半独立" if "半独立" in (s["ver"]["indep"] or "")
                    else "·异源")
                if s["ver"].get("truncated"):
                    ver_src += "·换纪截断"
            else:
                ver_src = "不可核验(" + (s["ver"].get("reason") or "无源") + ")"
            L.append(
                f"| {s['label']} | {s['idx']} | {s['ticks']} | "
                f"{s['fid0']}→{s['fid1']}/{_fmt(s['fid_end'],0)} | "
                f"{_fmt(s['tgt'],1)} | {s['vd']}/{s['why']} | "
                f"{pos_cell} | {pos_ev} | {ref_cell} | "
                f"{v_cell} | {v_ev} | {jd['conclusion']} | "
                f"{s['valid']} | {s['why_invalid'] or '-'} | {ver_src} | "
                f"{first} | {last} | {s['agree']} |")
    L.append("")
    L.append("列口径（与规格 §1/§3 的对应关系）：")
    L.append("")
    L.append("- **ego−tgt**：位置项。用结束拍 `coarse.ego`——它是路线层内部 "
             "`v_ref` 的同拍投影（tick_route 先更新 v_ref 再判完成）。"
             "`route.ref` 是承诺参考道：finish 拍它已等于目标道，`ref−tgt` 恒 0，"
             "不能当位置证据，只作诊断列。")
    L.append("- **位置证据**：`fresh@年龄` = 本拍 `dgeo_new=true` 且 "
             "`ro_hold=false`；`hold(来源,d年龄)` = 保持值（括号里的 d 是深度"
             "证据年龄，保持时长读不出，规格 G3）；`missing` = 无路心读数。")
    L.append("- **v**：`(coarse.ego_t − coarse.ego_{t−1}) / dt`，dt 取本拍 "
             "`dt`（缺失时用 ts_ns 差分）。速度证据取两端较差：两端都 fresh "
             "才是 `measured`，否则 `hold_diff`（冻结读数的 0 差商不是车停了）。")
    L.append("- **结论**：按规格 §3 表；「完成」要求位置为新鲜证据且进带 + "
             "实测横速落稳。位置只有保持值时最多只能给 `unconfirmed`。")
    L.append("- **有效性**：`valid` 可进指标分母；`undecidable` 为目标道变了/"
             "缺值/时间不完整/段末换纪等；`missing_field` 为旧 trace 无所需列。")
    L.append("- **核验**：只用结束拍之后的拍，窗口止于下一次 merging 起拍或"
             "下一次换纪；优先 `lateral_frame.x`（只取影子接受新鲜观测的拍，"
             "排除 reanchor 跳变过渡态；与判定链共享路心读数，半独立），"
             "否则 `lane_obs.x`（异源线检测，已知会离谱）。")
    L.append("- **来源不独立**只降核验列的可信度，不撤销段的判定有效性："
             "本 trace 里没有与路心读数完全独立的横位源，错误完成率暂不可算。")
    L.append("- **一致性**：判定偏移与核验最近一拍偏移比较符号；两侧都在带内算"
             "同号。它只说明两套估计不打架——「首达」是窗口内是否到过目标带，"
             "与最近一拍一致不是同一件事。")

    L.append("## ② 按局汇总（分母=段；同一局内多段相关，不作独立样本，"
             "跨局不池化）")
    L.append("")
    L.append("| 局 | 段数 | valid | undecidable | missing_field | "
             "completed | not_completed | unconfirmed | 仅 valid 段 c/nc/u | "
             "结束 vd 分布 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    all_segs = [s for g in games for s in g["segs"]]
    for g in games:
        segs = g["segs"]
        n = len(segs)
        vc = Counter(s["valid"] for s in segs)
        cc = Counter(s["jd"]["conclusion"] for s in segs)
        ccv = Counter(s["jd"]["conclusion"] for s in segs
                      if s["valid"] == "valid")
        vd = Counter(f"{s['vd']}/{s['why']}" for s in segs)
        L.append(
            f"| {g['label']} | {n} | {vc['valid']} | {vc['undecidable']} | "
            f"{vc['missing_field']} | {cc['completed']} | "
            f"{cc['not_completed']} | {cc['unconfirmed']} | "
            f"{ccv['completed']}/{ccv['not_completed']}/{ccv['unconfirmed']} | "
            f"{', '.join(f'{k}×{v}' for k, v in sorted(vd.items()))} |")
    L.append("")

    L.append("## ③ 全局说明")
    L.append("")
    n = len(all_segs)
    cc = Counter(s["jd"]["conclusion"] for s in all_segs)
    vc = Counter(s["valid"] for s in all_segs)
    vd = Counter(f"{s['vd']}/{s['why']}" for s in all_segs)
    ag = Counter(s["agree_cls"] for s in all_segs)
    ver = Counter(s["ver"]["label"] for s in all_segs)
    L.append(f"- 局数 {len(games)}，换道段 {n}（判定分母）、"
             f"有效段 {vc['valid']}、不可判 {vc['undecidable']}、"
             f"缺列 {vc['missing_field']}。")
    L.append(f"- 三值结论（全部段）：completed {cc['completed']}、"
             f"not_completed {cc['not_completed']}、"
             f"unconfirmed {cc['unconfirmed']}。")
    vsegs = [s for s in all_segs if s["valid"] == "valid"]
    ccv = Counter(s["jd"]["conclusion"] for s in vsegs)
    L.append(f"- 三值结论（仅 valid 段，n={len(vsegs)}）：completed "
             f"{ccv['completed']}、not_completed {ccv['not_completed']}、"
             f"unconfirmed {ccv['unconfirmed']}。")
    L.append(f"- 结束拍 vd 分布："
             f"{', '.join(f'{k}×{v}' for k, v in sorted(vd.items()))}。")
    fin_all = [s for s in all_segs if s["vd"] == "finish"]
    cf = Counter(s["jd"]["conclusion"] for s in fin_all)
    nf = Counter(s["jd"]["conclusion"] for s in all_segs
                 if s["vd"] != "finish")
    L.append(f"- 结束拍 vd=finish 的段按本判据重判：completed {cf['completed']}、"
             f"not_completed {cf['not_completed']}、"
             f"unconfirmed {cf['unconfirmed']}（共 {len(fin_all)}）。"
             f"非 finish 段：completed {nf['completed']}、"
             f"not_completed {nf['not_completed']}、"
             f"unconfirmed {nf['unconfirmed']}。")
    L.append(f"- 事后核验标签："
             f"{', '.join(f'{k}×{v}' for k, v in sorted(ver.items()))}；"
             f"核验来源：{dict(Counter(s['ver']['src'] for s in all_segs))}。")
    L.append(f"- 判定—核验一致性："
             f"{', '.join(f'{k}×{v}' for k, v in sorted(ag.items()))}。"
             f"**一致性只说明两套估计不打架，不代表完成判定正确。**")
    L.append("")
    L.append("### 缺列情况（旧 trace 无此列 → missing_field）")
    L.append("")
    lack = [g for g in games if g["missing"]]
    if not lack:
        L.append("- 无：所有局的判定所需列均存在。")
    for g in lack:
        L.append(f"- {g['label']}：缺 {', '.join(g['missing'])}；"
                 f"null 拍数 {dict(g['nulls'])}")
    L.append("")
    L.append("### 位置项复算与结束 vd 的自洽检查（只用于确认复算没读错列）")
    L.append("")
    fin = [s for s in all_segs if s["vd"] == "finish"]
    fin_in = sum(1 for s in fin if s["jd"]["pos_in"])
    fin_v_bad = [s for s in fin if s["jd"]["v"] is not None
                 and abs(s["jd"]["v"]) > tol_v]
    nonfin_in = [s for s in all_segs
                 if s["vd"] != "finish" and s["jd"]["pos_in"]]
    L.append(f"- vd=finish 的 {len(fin)} 段中，复算位置进带 {fin_in} 段、"
             f"复算 |v| 超速度容差 {len(fin_v_bad)} 段"
             f"（finish 的原始门就是这两条，否则说明本次复算读错了列/dt）。")
    L.append(f"  - 其中位置与速度都是新鲜证据的只有 "
             f"{sum(1 for s in fin if s['jd']['v_class'] == 'measured')} 段；"
             f"其余是保持差商（差商 0 由冻结读数给出，不等于车停稳）。"
             f"结束拍 route.ref 已等于目标道，ref−tgt 恒 0，不能作位置项。")
    L.append(f"- 非 finish 且复算位置进带的段 {len(nonfin_in)} 段"
             f"（多为超时/重建/撤销时车恰在带内；段末换纪的不参与）。")
    L.append("")
    L.append("### 样本量与结论边界")
    L.append("")
    per = ", ".join(f"{g['label']}={len(g['segs'])}" for g in games)
    L.append(f"- 每局段数：{per}。同一局内各段共享同一套感知与参考系误差，"
             f"段数不等于独立样本；本表按局分组报告，不做跨局池化。")
    L.append("- 本数据量只够核对解析、因果隔离与分母计算，不足以支撑统计"
             "结论（各局 n 很小且证据类高度不均）。")
    L.append("- **阈值未标定，本探针不做任何效果结论**；错误完成率、确认延迟"
             "等指标在核验侧具备独立有效证据前没有解释价值。")
    L.append("- 事后核验的横向估计与判定链共享输入或以车道线为源，"
             "两列不得互相推导（一致 ≠ 判对，不一致 ≠ 判错）。")
    return "\n".join(L)


def _inside(path: str, root: str) -> bool:
    """path 是否落在 root 内（跨盘符时 commonpath 会抛，按不落在内处理）。"""
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
        description="换道完成判定探针（只读；口径见 COMPLETION_SPEC.md）")
    ap.add_argument("--data", default=DEFAULT_DATA,
                    help="旧局目录（一文件一局）")
    ap.add_argument("--logs", default=DEFAULT_LOGS,
                    help="会话日志目录（一会话多局）")
    ap.add_argument("--runs", default="", help="局名，逗号分隔")
    ap.add_argument("--session", default="", help="会话 token，逗号分隔")
    ap.add_argument("--decision", default="",
                    help="决策真源 decision.json（默认取包内路径）")
    ap.add_argument("--commit", default="", help="覆盖代码版本号")
    ap.add_argument("--out", default="",
                    help="可选输出文件（只接受仓库外路径）")
    args = ap.parse_args(argv)

    root = _repo_root()
    decision = args.decision or (
        os.path.join(root, "maaracing_master", "plugins", "speedrush",
                     "resources", "policy", "decision.json") if root else "")
    try:
        tol_p, tol_v = load_tolerances(decision)
    except (OSError, KeyError, ValueError) as exc:
        print(f"容差不可用，拒判：{exc}")
        print("用 --decision 指向 decision.json；本脚本不写任何判定数值。")
        return 2

    if not _split(args.runs) and not _split(args.session):
        runs, sessions, repo_sessions = discover(args.data, args.logs, root)
        print("# 未指定 --runs / --session：先列可用数据源\n")
        if not os.path.isdir(args.data):
            data_txt = "不存在"
        elif runs:
            data_txt = f"{len(runs)} 局（{', '.join(runs)}）"
        else:
            data_txt = "存在，但没有 trace_<局>.jsonl（不是控制 trace）"
        print(f"- 旧局目录 {args.data}：{data_txt}")
        if not os.path.isdir(args.logs):
            logs_txt = "不存在"
        elif sessions:
            logs_txt = f"{len(sessions)} 个会话（{', '.join(sessions)}）"
        else:
            logs_txt = "存在，但没有会话流"
        print(f"- 会话目录 {args.logs}：{logs_txt}")
        if repo_sessions:
            print(f"- 仓库 logs/ 下会话流：{', '.join(repo_sessions)}")
        print("\n用法：--runs <局名>[,..] 或 --session <token>[,..]"
              "（--logs 默认取数据目录同级的 logs/）。")
        return 2

    if args.out:
        out_abs = os.path.abspath(args.out)
        if root and _inside(out_abs, root):
            print(f"拒绝写入仓库内路径：{out_abs}\n"
                  f"探针只写仓库外文件，默认只打印 stdout。")
            return 2
        if not os.path.isdir(os.path.dirname(out_abs)):
            print(f"输出目录不存在：{os.path.dirname(out_abs)}（不自动创建）")
            return 2

    try:
        base = _baseline()
        games = load_games(args, base)
    except FileNotFoundError as exc:
        print(f"trace 不存在：{exc.filename}\n"
              f"旧局用 --data/--runs，会话流用 --logs/--session。")
        return 2
    except ImportError as exc:
        print(f"无法复用 probe_merge_baseline.py：{exc}")
        return 2
    if not games:
        print("没有可判定的局（检查 --runs / --session）。")
        return 2

    results = [analyse_game(label, rows, path, tol_p, tol_v)
               for label, rows, path in games]
    meta = {"commit": _commit(root, args.commit), "decision": decision,
            "data": args.data, "logs": args.logs}
    text = render(results, meta, tol_p, tol_v)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入仓库外文件：{os.path.abspath(args.out)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
