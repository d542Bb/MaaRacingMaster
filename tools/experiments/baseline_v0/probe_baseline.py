# -*- coding: utf-8 -*-
"""固定协议基线的指标计算：只读局汇总与 trace，输出逐阶段指标与波动统计。

口径见同目录 README（跑之前定义，跑完不改）。不 import 生产代码。

用法：
    .venv/Scripts/python.exe tools/experiments/baseline_v0/probe_baseline.py \
        --tokens 20261029_XXXXXX_1,20261029_YYYYYY_1 [--json out.json]
    ... --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics as st
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / "maaracing_master/plugins/speedrush/resources/policy/decision.json"
CALIB = ROOT / "maaracing_master/plugins/speedrush/resources/calibration/gate0.json"


def quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = (len(ys) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.is_file() else "missing"


def _head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def stage_metrics(token: str, phase: int, rows: list[dict],
                  summary: dict | None) -> dict:
    dt = [float(r["dt"]) for r in rows if (r.get("dt") or 0) > 0]
    plans = [r for r in rows if isinstance(r.get("plan"), dict)]
    inv = [r for r in plans if not r["plan"].get("valid")]
    cons = 0
    seg = 0
    prev = False
    for r in rows:
        bad = r.get("state") in ("CONSERVE", "FAULT")
        if bad and not prev:
            seg += 1
        cons += 1 if bad else 0
        prev = bad
    jit: list[float] = []
    prev = None
    for r in rows:
        if r.get("state") != "CRUISE" or r.get("steer_norm") is None:
            prev = None
            continue
        cur = float(r["steer_norm"])
        if prev is not None:
            jit.append(abs(cur - prev))
        prev = cur
    reads = [r["hud_score"] for r in rows if r.get("hud_score") is not None]
    # 末次读数会被结算/弹窗污染：用最后 20 个有效读数的中位做稳健值，
    # 同时保留末值用于对账（差值大时打标记）。
    score = (st.median(reads[-20:]) if reads
             else (None if not summary else summary.get("score")))
    score_last = reads[-1] if reads else score
    dur = None if not summary else summary.get("dur_s")
    spm = (score / (dur / 60.0)
           if score is not None and dur and dur > 0 else None)
    jump_pct = (None if not score or score_last is None
                else 100.0 * (score_last - score) / score)
    return {
        "token": token, "phase": phase, "ticks": len(rows),
        "score": score, "score_last": score_last, "jump_pct": jump_pct,
        "dur_s": dur, "score_per_min": spm,
        "dt_p50": quantile(dt, 0.5), "dt_p95": quantile(dt, 0.95),
        "plan_n": len(plans),
        "plan_invalid_pct": 100.0 * len(inv) / len(plans) if plans else None,
        "conserve_ticks": cons, "conserve_segments": seg,
        "steer_jit_p95": quantile(jit, 0.95),
    }


def _load(root: Path, token: str) -> tuple[list[dict], list[dict]]:
    base = root / token / "trace"
    rows = []
    tp = base / "speedrush.jsonl"
    if tp.is_file():
        for line in tp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") is None:
                rows.append(r)
    sums: list[dict] = []
    sp = base / "speedrush_summary.jsonl"
    if sp.is_file():
        for line in sp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            sums.append(r)
    return rows, sums


def _segments(rows: list[dict]) -> list[list[dict]]:
    """按「阶段号变化或 >2s 断档」切分；同一会话可能有多轮，阶段号会复用。"""
    out: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for r in rows:
        gap = (None if prev is None
               else (r.get("ts_ns", 0) - prev.get("ts_ns", 0)) / 1e9)
        if cur and (r.get("stage_id") != prev.get("stage_id")
                    or (gap is not None and gap > 2.0)):
            out.append(cur)
            cur = []
        cur.append(r)
        prev = r
    if cur:
        out.append(cur)
    return out


def summarize(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    mean = st.mean(xs)
    sd = st.stdev(xs) if len(xs) > 1 else 0.0
    return {"n": len(xs), "median": st.median(xs),
            "q1": quantile(xs, 0.25), "q3": quantile(xs, 0.75),
            "min": min(xs), "max": max(xs),
            "cv_pct": 100.0 * sd / mean if mean else None}


def _fmt(x, nd=2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="logs 根（默认 %%APPDATA%%/MaaRacingMaster/logs）")
    ap.add_argument("--tokens", required=False, default="")
    ap.add_argument("--json", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    root = Path(args.root) if args.root else (
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs")
    tokens = [t for t in args.tokens.split(",") if t]
    if not tokens:
        raise SystemExit("需要 --tokens（逗号分隔的会话 token）")
    print(f"冻结记录：commit {_head()}  policy {_sha(POLICY)}  "
          f"calib {_sha(CALIB)}")
    out = []
    for token in tokens:
        rows, sums = _load(root, token)
        blocks = _segments(rows)
        if len(blocks) != len(sums):
            print(f"  [warn] {token}: 切出 {len(blocks)} 段、局汇总 {len(sums)} 条，"
                  f"按顺序配对")
        for i, srows in enumerate(blocks):
            sm = sums[i] if i < len(sums) else None
            phase = (int(sm.get("phase") or 0) if sm else
                     int(srows[0].get("stage_id") or 0))
            if sm and int(sm.get("ticks") or 0) != len(srows):
                print(f"  [warn] {token} 第 {i + 1} 段：trace {len(srows)} 拍、"
                      f"汇总 {sm.get('ticks')} 拍")
            out.append(stage_metrics(token, phase, srows, sm))
    print("\n逐阶段：")
    print("  token/stage  score  dur_s  score/min  dt_p95  plan_inv%  "
          "cons_seg  steer_jit  末次读数差")
    for m in out:
        print(f"  {m['token']}/{m['phase']:<2} {str(m['score']):>6} "
              f"{_fmt(m['dur_s'], 1):>6} {_fmt(m['score_per_min'], 1):>9} "
              f"{_fmt(m['dt_p95'], 3):>7} {_fmt(m['plan_invalid_pct'], 1):>9} "
              f"{m['conserve_segments']:>8} {_fmt(m['steer_jit_p95'], 4):>9}"
              f"  {_fmt(m['jump_pct'], 1):>6}%"
              + (" !" if (m["jump_pct"] or 0) > 5 else ""))
    p2 = [m for m in out if m["phase"] == 2]
    p1 = [m for m in out if m["phase"] == 1]
    for name, group in (("阶段 2（主判据）", p2), ("阶段 1（只报不判）", p1)):
        stats = summarize([m["score_per_min"] for m in group])
        print(f"\n{name} 得分速度：n={stats.get('n', 0)}"
              + ("" if not stats.get("n") else
                 f"  中位 {stats['median']:.1f}  四分位 "
                 f"[{stats['q1']:.1f}, {stats['q3']:.1f}]  "
                 f"范围 [{stats['min']:.1f}, {stats['max']:.1f}]  "
                 f"CV {stats['cv_pct']:.1f}%"))
        jit = summarize([m["steer_jit_p95"] for m in group])
        if jit.get("n"):
            print(f"  转向抖动 p95：中位 {jit['median']:.3f}  "
                  f"范围 [{jit['min']:.3f}, {jit['max']:.3f}]")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "frozen": {"commit": _head(), "policy": _sha(POLICY),
                       "calib": _sha(CALIB)},
            "stages": out,
            "phase2": summarize([m["score_per_min"] for m in p2]),
            "phase1": summarize([m["score_per_min"] for m in p1]),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON：{args.json}")
    return 0


def selftest() -> None:
    assert quantile([], 0.5) is None
    assert quantile([1.0], 0.9) == 1.0
    assert quantile([0.0, 10.0], 0.5) == 5.0
    rows = [
        {"dt": 0.05, "state": "CRUISE", "steer_norm": 0.1,
         "plan": {"valid": True}},
        {"dt": 0.10, "state": "CRUISE", "steer_norm": 0.3,
         "plan": {"valid": False}},
        {"dt": 0.0, "state": "CONSERVE", "steer_norm": 0.9,
         "plan": None},
    ]
    m = stage_metrics("t", 2, rows, {"score": 600, "dur_s": 60.0})
    assert m["score_per_min"] == 600.0
    assert m["ticks"] == 3 and m["plan_n"] == 2
    assert abs(m["plan_invalid_pct"] - 50.0) < 1e-9
    assert m["conserve_segments"] == 1 and m["conserve_ticks"] == 1
    assert abs(m["steer_jit_p95"] - 0.2) < 1e-6
    stats = summarize([10.0, 20.0, 30.0])
    assert stats["median"] == 20.0 and stats["n"] == 3
    print("selftest ok")


if __name__ == "__main__":
    raise SystemExit(main())
