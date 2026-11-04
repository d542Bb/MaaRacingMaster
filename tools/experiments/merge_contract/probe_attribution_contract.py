#!/usr/bin/env python
"""到达位置归因契约：具名枚举 + 边界自测 + 真实 trace 统计（只读）。

规则（与 C_GATE_DESIGN.md 一致）：

- 换档标记属于**相邻两次新鲜读数之间的变化**，标签挂在**较新那次的读数**
  上（它到达时算出的 hop）。
- 归因看当前完成位置来自哪一份读数：
  * ``SHIFT_HELD``：当前位置就是那份 hop=1 的新鲜读数，或它的保持值；
  * ``NORMAL``：当前位置是新鲜读数 / 其保持值，且那只读数的 hop 为 0 或 2；
  * ``OTHER``：已知不是上面两类（如保持值已经变了）；
  * ``UNKNOWN``：证据不足（首帧、缺读数、还没有新鲜读数、hop 缺失）。
- 只有 ``SHIFT_HELD`` 会被 C 拦；其余放行。``UNKNOWN`` 必须按原因分开记，
  不并入 NORMAL。

用法::

    python probe_attribution_contract.py --selftest
    python probe_attribution_contract.py --session 20261028_183009_1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

_APPDATA = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
_DEFAULT_LOGS = os.path.join(_APPDATA, "MaaRacingMaster", "logs")
_TOL_LANE = 1e-3

NORMAL = "NORMAL"
SHIFT_HELD = "SHIFT_HELD"
OTHER = "OTHER"
UNKNOWN = "UNKNOWN"


def pos_class(row: dict) -> str:
    """与完成判定器同一口径：新鲜 = dgeo_new 且非保持。"""
    if row.get("road_offset") is None:
        return "missing"
    if row.get("dgeo_new") is True and row.get("ro_hold") is False:
        return "fresh"
    if row.get("ro_hold") is True or row.get("dgeo_new") is False:
        return "hold"
    return "unknown"


class Lineage:
    """路心读数序列上的归因器；状态只有「上一份新鲜读数」。"""

    def __init__(self, tol: float = _TOL_LANE):
        self.tol = tol
        self.last: tuple[float, int | None, int | None] | None = None
        # (road_offset, hop, fid)

    def step(self, row: dict) -> tuple[str, str | None]:
        """返回 (枚举, 原因)。原因只对 OTHER/UNKNOWN 有意义。"""
        pc = pos_class(row)
        ro = row.get("road_offset")
        fid = row.get("fid")
        if pc == "missing":
            return UNKNOWN, "no_reading"
        if pc == "unknown":
            return UNKNOWN, "freshness_unknown"
        if pc == "fresh":
            hop = row.get("ro_hop")
            prev = self.last
            if prev is None:
                attr = (UNKNOWN, "first_fresh")
            elif hop is None:
                attr = (UNKNOWN, "no_hop")
            elif hop == 1:
                attr = (SHIFT_HELD, None)
            elif hop in (0, 2):
                attr = (NORMAL, None)
            else:
                attr = (UNKNOWN, "bad_hop")
            self.last = (float(ro), hop, fid)
            return attr
        # hold
        if self.last is None:
            return UNKNOWN, "no_fresh"
        lro, lhop, _ = self.last
        if abs(float(ro) - lro) <= self.tol:
            if lhop == 1:
                return SHIFT_HELD, None
            if lhop in (0, 2):
                return NORMAL, None
            return UNKNOWN, "last_hop_missing"
        return OTHER, "held_changed"


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    def row(ro=None, hold=False, new=True, hop=None, fid=1):
        return {"road_offset": ro, "ro_hold": hold, "dgeo_new": new,
                "ro_hop": hop, "fid": fid}

    cases = [
        ("首帧新鲜（hop=None）", [row(0.0, hop=None)], UNKNOWN, "first_fresh"),
        ("新鲜 hop=0", [row(0.2, hop=0), row(0.5, hop=0)], NORMAL, None),
        ("新鲜 hop=1", [row(0.2, hop=0), row(1.3, hop=1)], SHIFT_HELD, None),
        ("新鲜 hop=2 黑视", [row(0.2, hop=0), row(0.4, hop=2)], NORMAL, None),
        ("hop=1 的保持值", [row(0.2, hop=0), row(1.3, hop=1),
                            row(1.3, hold=True)], SHIFT_HELD, None),
        ("hop=0 的保持值", [row(0.2, hop=0), row(0.5, hop=0),
                            row(0.5, hold=True)], NORMAL, None),
        ("hop=2 的保持值", [row(0.2, hop=0), row(0.4, hop=2),
                            row(0.4, hold=True)], NORMAL, None),
        ("保持值变了", [row(0.2, hop=0), row(0.5, hop=0),
                        row(0.9, hold=True)], OTHER, "held_changed"),
        ("还没有新鲜读数", [row(0.5, hold=True)], UNKNOWN, "no_fresh"),
        ("缺读数", [row(None)], UNKNOWN, "no_reading"),
        ("新鲜度未知", [{"road_offset": 0.2, "ro_hold": None,
                         "dgeo_new": None, "ro_hop": None, "fid": 1}],
         UNKNOWN, "freshness_unknown"),
        ("端点：hop1 之后来了新新鲜读数，保持旧值", [
            row(0.2, hop=0), row(1.3, hop=1), row(0.4, hop=0),
            row(1.3, hold=True)], OTHER, "held_changed"),
        ("端点：hop1 后接 hop0，保持后一份", [
            row(0.2, hop=0), row(1.3, hop=1), row(0.4, hop=0),
            row(0.4, hold=True)], NORMAL, None),
        ("连续保持同一份 hop=1", [
            row(0.2, hop=0), row(1.3, hop=1),
            row(1.3, hold=True), row(1.3, hold=True)], SHIFT_HELD, None),
        ("新新鲜读数清掉旧标签", [
            row(0.2, hop=0), row(1.3, hop=1), row(0.4, hop=0),
            row(0.4, hold=True)], NORMAL, None),
        ("新鲜但 hop 缺失（旧 trace）", [
            row(0.2, hop=0), row(0.4, hop=None)], UNKNOWN, "no_hop"),
    ]
    bad = []
    for name, seq, want_cls, want_reason in cases:
        ln = Lineage()
        got = None
        for r in seq:
            got = ln.step(r)
        if got != (want_cls, want_reason):
            bad.append((name, got, (want_cls, want_reason)))

    # 容差边界
    ln = Lineage(tol=1e-3)
    ln.step(row(1.3, hop=1))
    edge = ln.step(row(1.301, hold=True))
    over = ln.step(row(1.302, hold=True))
    if edge != (SHIFT_HELD, None):
        bad.append(("容差边界内", edge, (SHIFT_HELD, None)))
    if over != (OTHER, "held_changed"):
        bad.append(("容差边界外", over, (OTHER, "held_changed")))

    for name, got, want in bad:
        print(f"FAIL {name}: got={got} want={want}")
    print(f"selftest: {len(cases) + 2 - len(bad)}/{len(cases) + 2} passed")
    return 0 if not bad else 1


# ---------------------------------------------------------------- 扫 trace

def scan(sess: str, logs: str) -> dict:
    p = os.path.join(logs, sess, "trace", "speedrush.jsonl")
    rows = [json.loads(x) for x in open(p, encoding="utf-8")]
    ln = Lineage()
    run = None
    fin = Counter()
    fin_detail = []
    merge = Counter()
    cruise = Counter()
    unknown_reasons = Counter()
    shift_merge = []
    for r in rows:
        if r.get("run_id") != run:
            run = r.get("run_id")
            ln = Lineage()
        attr, reason = ln.step(r)
        if attr == UNKNOWN and reason:
            unknown_reasons[reason] += 1
        if r.get("state") != "CRUISE":
            continue
        rt = r.get("route") or {}
        cruise[attr] += 1
        if rt.get("ph") == "merging":
            merge[attr] += 1
            if attr == SHIFT_HELD:
                shift_merge.append(r.get("fid"))
        if rt.get("vd") == "finish":
            fin[attr] += 1
            if attr != NORMAL:
                fin_detail.append({
                    "fid": r.get("fid"),
                    "attr": attr, "reason": reason,
                    "last_fresh_fid": None if ln.last is None else ln.last[2],
                    "last_fresh_hop": None if ln.last is None else ln.last[1],
                    "d_ro": None if ln.last is None
                    else round(float(r["road_offset"]) - ln.last[0], 4),
                    "age_ms": r.get("dgeo_age_ms"),
                })
    return {"session": sess, "finish": dict(fin), "finish_detail": fin_detail,
            "merge": dict(merge), "cruise": dict(cruise),
            "unknown_reasons": dict(unknown_reasons),
            "shift_merge_n": len(shift_merge),
            "shift_merge_first": shift_merge[:10]}


def discover(logs: str) -> list:
    out = []
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
    ap = argparse.ArgumentParser(description="到达位置归因契约自测与统计")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--logs", default=_DEFAULT_LOGS)
    ap.add_argument("--session", default="", help="会话 token，逗号分隔")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not os.path.isdir(args.logs):
        print(f"日志目录不存在：{args.logs}")
        return 2
    sessions = ([s for s in args.session.split(",") if s]
                if args.session else discover(args.logs))
    for sess in sessions:
        res = scan(sess, args.logs)
        print(f"## {sess}")
        print(f"  完成拍归因: {res['finish']}")
        print(f"  并线拍归因: {res['merge']}；SHIFT_HELD {res['shift_merge_n']} 拍"
              + (f"（前几拍 {res['shift_merge_first']}）"
                 if res['shift_merge_first'] else ""))
        print(f"  巡航拍归因: {res['cruise']}")
        print(f"  UNKNOWN 原因（全表）: {res['unknown_reasons']}")
        for d in res["finish_detail"]:
            print(f"    非 NORMAL 完成: {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
