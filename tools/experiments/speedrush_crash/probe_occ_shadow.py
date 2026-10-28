"""生产选道影子的事件级汇总（2026-10-22）。

读 trace 的 ``occ`` 列（DecisionEngine 只读占用影子），把连续 ``veto`` 拍
合并成事件——回答“如果按这份占用去选，会多避让多少次”，而不是数帧。

口径：
- 事件 = 连续 ``occ.veto=true`` 的控制拍；相邻 veto 拍间隔 >0.2s 才断开。
- ``blocked`` 但 ``veto=false``：占用存在，但本拍选择已经横移到被占车道外，
  不算额外避让；单独计数。
- 事件内记录：id/模式/距离、选择层与选择目标、持续时间；事件后 2s 内同 id
  出现 pass 事件则标 ``pass``（最终确认车辆已掠过，不是假占用）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_occ_shadow.py \
        20261022_163005_p1 20261022_163050_p2
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

MERGE_GAP_S = 0.2
PASS_LOOKAHEAD_S = 2.0


def _root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("需要 APPDATA（Windows 运行数据目录）")
    return Path(base) / "MaaRacingMaster" / "debug" / "speedrush"


def _load(name: str) -> list[dict]:
    p = _root() / name / f"trace_{name}.jsonl"
    if not p.exists():
        raise SystemExit(f"找不到 {p}")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def _ids(occ: dict) -> set[int]:
    return {int(x) for x in (occ.get("ids") or [])}


def replay(name: str) -> dict:
    rows = _load(name)
    ts = [r["ts_ns"] / 1e9 for r in rows]
    events: list[dict] = []
    cur: dict | None = None
    last_veto_t = -1e9
    blocked_only = 0
    blocked_ticks = 0
    for i, r in enumerate(rows):
        occ = r.get("occ")
        veto = bool(occ and occ.get("veto"))
        if occ:
            blocked_ticks += 1
        if veto and occ:
            if cur is None or ts[i] - last_veto_t > MERGE_GAP_S:
                if cur is not None:
                    events.append(cur)
                cur = {
                    "start_fid": r["fid"], "start_ts": ts[i],
                    "end_fid": r["fid"], "end_ts": ts[i],
                    "ids": set(), "modes": {}, "m": {},
                    "sel_layer": occ.get("sel_layer"),
                    "sel_why": occ.get("sel_why"),
                    "sel_x0": occ.get("sel_x"), "sel_x1": occ.get("sel_x"),
                }
            cur["end_fid"] = r["fid"]
            cur["end_ts"] = ts[i]
            cur["ids"] |= _ids(occ)
            for k, v in (occ.get("modes") or {}).items():
                cur["modes"][k] = v
            for k, v in (occ.get("m") or {}).items():
                cur["m"][k] = v
            cur["sel_x1"] = occ.get("sel_x")
            last_veto_t = ts[i]
        elif occ and occ.get("blocked"):
            blocked_only += 1
    if cur is not None:
        events.append(cur)
    # 事件后 2s 内同 id 的 pass 事件（确认最终掠过，降低“假占用”嫌疑）
    pass_by_fid: list[tuple[float, int]] = []
    for r in rows:
        for e in (r.get("passes") or []):
            pass_by_fid.append((r["ts_ns"] / 1e9, int(e["id"])))
    for ev in events:
        ids = ev["ids"]
        ev["pass"] = any(
            tid in ids and 0.0 <= t - ev["end_ts"] <= PASS_LOOKAHEAD_S
            for t, tid in pass_by_fid)
        ev["dur_s"] = round(ev["end_ts"] - ev["start_ts"], 2)
        ev["ids"] = sorted(ids)
        ev["sel_moved_m"] = (None if ev["sel_x0"] is None
                             or ev["sel_x1"] is None
                             else round(ev["sel_x1"] - ev["sel_x0"], 2))
    return {"name": name, "events": events, "blocked_ticks": blocked_ticks,
            "blocked_only": blocked_only}


def report(r: dict) -> None:
    evs = r["events"]
    print(f"===== {r['name']} | occ 拍 {r['blocked_ticks']}"
          f" | veto 事件 {len(evs)} | 占用但选择已让开 {r['blocked_only']} 拍")
    for ev in evs:
        modes = ",".join(sorted({str(x) for x in ev["modes"].values()
                                 if x}))
        ms = ",".join(f"{k}:{v}" for k, v in ev["m"].items())
        print(f"  fid {ev['start_fid']}~{ev['end_fid']} {ev['dur_s']}s"
              f" ids={ev['ids']} 模式={modes} m={ms}"
              f" sel=L{ev['sel_layer']}/{ev['sel_why']}"
              f" selΔ={ev['sel_moved_m']}m"
              f"{' pass' if ev['pass'] else ''}")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = argv[1:]
    if not names:
        raise SystemExit("用法：probe_occ_shadow.py <session> [session...]")
    for n in names:
        report(replay(n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
