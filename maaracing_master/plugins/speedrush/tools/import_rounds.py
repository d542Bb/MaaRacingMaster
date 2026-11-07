#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把历史会话里的自动控制回合补进本地战绩库。

数据源（只读）：
- ``logs/<token>/trace/speedrush_summary.jsonl``
- ``debug/<会话>/speedrush/drive/<局前缀>_p<阶段>/meta.json`` 与 ``hud/hud.jsonl``
- ``logs/<token>/trace/speedrush.jsonl``（判 深度几何 / 全程直行）

只导入 ``meta.source == "control"`` 的回合；人工录制与宏不写入。重复运行
幂等；已有 ``source='live'`` 的行跳过，历史数据不覆盖实测数据。

用法（仓库根，.venv）：
    python -m plugins.speedrush.tools.import_rounds --dry-run
    python -m plugins.speedrush.tools.import_rounds
    python -m plugins.speedrush.tools.import_rounds --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[4]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from maaracing_master.core.paths import debug_dir, logs_dir  # noqa: E402
from maaracing_master.plugins.speedrush import dashboard_store  # noqa: E402
from maaracing_master.plugins.speedrush.hud import read_hud_scores  # noqa: E402

_LABELS = {"depth": "深度", "straight": "直行", "manual": "人工",
           "macro": "宏", "control": "自动", "unknown": "—"}
_MATCH_TOL_S = 3.0
_STAGE_TOL_NS = 10_000_000_000


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_summaries(logs_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(logs_root.glob("*/trace/speedrush_summary.jsonl")):
        token = path.parent.parent.name
        for rec in _jsonl(path):
            ts = _parse_dt(rec.get("ts"))
            phase = _int_or_none(rec.get("phase"))
            if ts is None or phase not in (1, 2):
                continue
            rows.append({
                "session": token, "ts": ts, "phase": phase,
                "ticks": _int_or_none(rec.get("ticks")) or 0,
                "score": _int_or_none(rec.get("score")),
                "rate": _float_or_none(rec.get("rate")),
                "dur_s": _float_or_none(rec.get("dur_s")),
            })
    return rows


def _load_metas(debug_root: Path) -> list[dict[str, Any]]:
    metas: list[dict[str, Any]] = []
    for path in sorted(debug_root.glob("*/speedrush/drive/*/meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = path.parent.name
        stamp, sep, phase_text = name.rpartition("_p")
        if not sep:
            continue
        phase = _int_or_none(phase_text)
        started = _parse_dt(meta.get("started_at"))
        if phase not in (1, 2) or started is None:
            continue
        metas.append({
            "stamp": stamp, "phase": phase, "dir": path.parent,
            "started": started, "ended": _parse_dt(meta.get("ended_at")),
            "round_no": _int_or_none(meta.get("round_no")) or 1,
            "source": str(meta.get("source") or ""),
            "stop_reason": str(meta.get("stop_reason") or ""),
        })
    return metas


def _match_summaries(summaries: list[dict[str, Any]],
                     metas: list[dict[str, Any]]) -> list[tuple[dict, dict]]:
    by_phase: dict[int, list[dict]] = {}
    for meta in metas:
        by_phase.setdefault(meta["phase"], []).append(meta)
    matched: list[tuple[dict, dict]] = []
    for row in summaries:
        candidates = by_phase.get(row["phase"], [])
        best = min(candidates,
                   key=lambda m: abs((m["started"] - row["ts"]).total_seconds()),
                   default=None)
        if best is None:
            continue
        if abs((best["started"] - row["ts"]).total_seconds()) > _MATCH_TOL_S:
            continue
        matched.append((row, best))
    return matched


def _trace_modes(session_dir: Path) -> tuple[dict[int, str], list[int]]:
    """返回 (每段驾驶形态, 每段开始的墙钟 ns 列表)。

    阶段号在同一会话里每局重复（1/2、1/2），故以**阶段开始时刻**为键，
    不用 stage_id。形态取该段任一非 STRAIGHT 状态 → depth；全 STRAIGHT
    → straight；无数据行 → 不收录（调用方回退 control）。
    """
    modes: dict[int, str] = {}
    walls: list[int] = []
    current_wall = 0
    path = session_dir / "trace" / "speedrush.jsonl"
    if not path.is_file():
        return modes, walls
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "stage_begin":
                current_wall = int(rec.get("ts_wall_ns") or 0)
                if current_wall:
                    walls.append(current_wall)
                continue
            if "type" in rec or not current_wall:
                continue
            state = rec.get("state")
            if state and state != "STRAIGHT":
                modes[current_wall] = "depth"
            elif current_wall not in modes:
                modes[current_wall] = "straight"
    return modes, walls


def _stage_for(row: dict[str, Any], walls: list[int]) -> int | None:
    if not walls:
        return None
    target = int(row["ts"].timestamp() * 1e9)
    wall = min(walls, key=lambda value: abs(value - target))
    if abs(wall - target) > _STAGE_TOL_NS:
        return None
    return wall


def _opponent(meta_dir: Path, ego_final: int | None) -> int | None:
    """以段汇总末分为锚选对手末分；与线上写库共用同一选择函数。

    读数对先过 ``ScoreGate``（与线上采集同参）：hud.jsonl 里的单步假读
    （实测 214,760 / 414,920 的尖峰）不进锚选面。
    """
    gates = dashboard_store.new_pair_gates()
    pairs: list[tuple[int, int]] = []
    for rec in _jsonl(meta_dir / "hud" / "hud.jsonl"):
        scores = read_hud_scores(rec.get("fields") or {})
        if scores.ego_score is None or scores.opponent_score is None:
            continue
        ts_ns = int(rec.get("ts_ns") or 0)
        if not (gates[0].feed(ts_ns, scores.ego_score)
                and gates[1].feed(ts_ns, scores.opponent_score)):
            continue
        pairs.append((scores.ego_score, scores.opponent_score))
    return dashboard_store.pick_opponent(pairs, ego_final)


def build_rounds(logs_root: Path, debug_root: Path) -> list[dict[str, Any]]:
    summaries = _load_summaries(logs_root)
    metas = _load_metas(debug_root)
    by_round: dict[tuple[str, str], dict[str, Any]] = {}
    for row, meta in _match_summaries(summaries, metas):
        if meta["source"] != "control":
            continue
        key = (row["session"], meta["stamp"])
        item = by_round.setdefault(key, {
            "session": row["session"], "stamp": meta["stamp"],
            "round_no": meta["round_no"], "phases": {},
        })
        item["phases"][meta["phase"]] = (row, meta)

    rounds: list[dict[str, Any]] = []
    for key in sorted(by_round):
        item = by_round[key]
        p1 = item["phases"].get(1)
        p2 = item["phases"].get(2)
        if p1 is None and p2 is None:
            continue
        session_dir = logs_root / item["session"]
        modes, walls = _trace_modes(session_dir)
        base_row = (p1 or p2)[0]
        wall = _stage_for(base_row, walls)
        mode = modes.get(wall, "control") if wall is not None else "control"
        rounds.append({**item, "mode": mode, "p1": p1, "p2": p2})
    return rounds


def _phase_record(round_item: dict[str, Any], phase: int) -> dict[str, Any] | None:
    pair = round_item["p1"] if phase == 1 else round_item["p2"]
    if pair is None:
        return None
    summary, meta = pair
    return {
        "session_token": round_item["session"],
        "round_stamp": round_item["stamp"],
        "round_no": int(round_item["round_no"]),
        "drive_mode": round_item["mode"],
        "phase": phase,
        "stats": dashboard_store.PhaseStats(
            ticks=int(summary.get("ticks") or 0),
            score=_int_or_none(summary.get("score")),
            rate=_float_or_none(summary.get("rate")),
            dur_s=_float_or_none(summary.get("dur_s")),
        ),
        "completed": meta.get("stop_reason") == "chain_end",
        "opponent": _opponent(meta["dir"], _int_or_none(summary.get("score"))),
        "started_at": meta["started"].isoformat(timespec="seconds"),
        "ended_at": (meta.get("ended") or meta["started"]).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把极速狂飙历史自动控制回合补进本地战绩库")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    parser.add_argument("--json", action="store_true", help="结束后打印看板载荷")
    parser.add_argument("--logs-root", type=Path, default=None)
    parser.add_argument("--debug-root", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.db:
        db = args.db.expanduser().resolve()
        dashboard_store.db_path = lambda: db
    logs_root = args.logs_root or logs_dir()
    debug_root = args.debug_root or debug_dir()

    rounds = build_rounds(logs_root, debug_root)
    if not rounds:
        print("没有可导入的自动控制回合。")
        return 0

    written = skipped_live = failed = 0
    for item in rounds:
        if dashboard_store.existing_source(item["stamp"], item["session"]) == "live":
            skipped_live += 1
            print(f"跳过（已有实测记录）: {item['session']} / {item['stamp']}")
            continue
        for phase in (1, 2):
            rec = _phase_record(item, phase)
            if rec is None:
                continue
            if args.dry_run:
                print(
                    f"[dry-run] {rec['round_stamp']} p{phase} "
                    f"score={rec['stats'].score} opp={rec['opponent']} "
                    f"dur={rec['stats'].dur_s} mode={rec['drive_mode']} "
                    f"ok={rec['completed']}")
                continue
            err = dashboard_store.write_phase(**rec, source="import")
            if err:
                failed += 1
                print(f"写入失败: {rec['round_stamp']} p{phase}: {err}")
            else:
                written += 1

    if args.dry_run:
        print(f"dry-run：{len(rounds)} 回合；未写库。")
    else:
        print(f"导入完成：写入 {written} 段；跳过实测行 {skipped_live} 回合"
              f"；失败 {failed} 段。")
    if args.json:
        print(json.dumps(dashboard_store.read_dashboard_payload(_LABELS),
                         ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
