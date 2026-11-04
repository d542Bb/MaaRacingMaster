# -*- coding: utf-8 -*-
"""speedrush 控制流水统一读取器（探针兼容层，2026-10-25 起的新布局伴生件）。

四种布局一炉（自动识别）：
  new  logs/<会话token>/trace/speedrush.jsonl          阶段在行内 stage_id/run_id
  A    debug/speedrush/<ts>_pN/trace_<同名>.jsonl       逐阶段目录（伴 raw/、hud/）
  B    data/speedrush/control_traces/trace_<ts>_pN.jsonl 平铺文件名带阶段
  直接路径                                            传 .jsonl 即可

行级约定：数据行 = 没有 ``type`` 键的 JSON 行；新格式的协议行
（session_begin / stage_begin / stage_end / stage_switch / gap / end /
close_incomplete）一律跳过。**旧列名在新格式逐字保留**（协议头是新增键），
键式读取天然兼容。

局汇总：旧 = 会话目录 ``game_summary.jsonl``；新 = 会话目录
``trace/speedrush_summary.jsonl``（列结构未变）。

用法（探针侧最小改造）::

    from speedrush_trace_reader import iter_rows, load_stage, sessions, summary_rows
    rows = list(iter_rows(path))                # 任意布局路径 → 数据行
    rows = load_stage(path, stage_id=1)         # 新格式按行内阶段切段
    for s in sessions():                        # 枚举全部可读会话
        ...
    for rec in summary_rows(sess): ...          # 局汇总行

注意：新布局下帧证据（raw/、hud/、调试图）仍在 debug/speedrush/<ts>_pN/，
与流水分家；按 stage 时间窗（stage_begin 行 ts_wall_ns）桥接对齐。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator

__all__ = ["iter_rows", "load_stage", "stages", "sessions", "summary_rows",
           "pair_phases", "is_protocol_line"]

_STAGE_DIR_RE = re.compile(r"^(\d{8}_\d{6})(?:_p(\d+))?$")
_FLAT_RE = re.compile(r"^trace_(\d{8}_\d{6})(?:_p(\d+))?\.jsonl$")


def _approot() -> Path:
    base = os.environ.get("APPDATA")
    return Path(base) / "MaaRacingMaster" if base else Path.home() / ".MaaRacingMaster"


def root_of(layout: str) -> Path:
    """三种布局的根目录：new=logs；A=debug/speedrush；B=data/…/control_traces。"""
    r = _approot()
    return {"new": r / "logs",
            "A": r / "debug" / "speedrush",
            "B": r / "data" / "speedrush" / "control_traces"}[layout]


def is_protocol_line(rec: dict) -> bool:
    return "type" in rec


def _iter_records(path: str | Path) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def iter_rows(path: str | Path) -> Iterator[dict]:
    """数据行流（任意布局）：跳过协议行，列名与旧格式一致。"""
    for rec in _iter_records(path):
        if not is_protocol_line(rec):
            yield rec


def stages(path: str | Path) -> list[dict]:
    """切段：新格式按行内 stage 控制行；旧格式整文件一段（stage_id=None）。"""
    segs: list[dict] = []
    cur: dict | None = None

    def _close(rec):
        nonlocal cur
        if cur is not None:
            cur["rows_end"] = cur["rows"]
            segs.append(cur)
            cur = None

    saw_control = False
    for rec in _iter_records(path):
        t = rec.get("type")
        if t is None:
            if cur is not None:
                cur["rows"] += 1
            elif not segs:
                # 旧格式（无控制行）：整文件一段
                cur = {"stage_id": None, "run_id": None, "rows": 1}
            elif segs and not segs[-1].get("_closed_outside"):
                segs[-1]["rows"] = segs[-1].get("rows", 0) + 1
        elif t == "session_begin":
            saw_control = True
        elif t in ("stage_begin",):
            saw_control = True
            _close(rec)
            cur = {"stage_id": rec.get("stage_id"), "run_id": rec.get("run_id"),
                   "seq0": rec.get("seq"), "rows": 0,
                   "ts_wall_ns": rec.get("ts_wall_ns")}
        elif t == "stage_switch":
            saw_control = True
            _close(rec)
            cur = {"stage_id": rec.get("to_stage"), "run_id": rec.get("run_id"),
                   "seq0": rec.get("seq_to"), "rows": 0,
                   "ts_wall_ns": rec.get("ts_wall_ns")}
        elif t == "stage_end":
            saw_control = True
            _close(rec)
    _close(None)
    if not saw_control and segs:
        # 旧格式兜底：给唯一一段标「阶段未知」
        segs[0]["stage_id"] = segs[0].get("stage_id")
    return segs


def load_stage(path: str | Path, stage_id=None) -> list[dict]:
    """取一个阶段的数据行；旧格式（无行内阶段）stage_id 只能是 None=全量。"""
    if stage_id is None:
        return list(iter_rows(path))
    rows: list[dict] = []
    active = False
    for rec in _iter_records(path):
        t = rec.get("type")
        if t == "stage_begin":
            active = rec.get("stage_id") == stage_id
        elif t == "stage_switch":
            active = rec.get("to_stage") == stage_id
        elif t == "stage_end":
            active = False
        elif t is None and active:
            rows.append(rec)
    if not rows and stages(path) and stage_id is not None:
        # 旧布局兼容：按文件名阶段后缀对上就全量给
        m = _FLAT_RE.match(Path(path).name) or _STAGE_DIR_RE.match(Path(path).parent.name)
        if m and m.group(2) == str(stage_id):
            return list(iter_rows(path))
    return rows


def sessions(layouts=("new", "A", "B")) -> list[dict]:
    """枚举可读会话：[{layout, session, phase, trace, summary}]。

    new 的一个会话目录可能含多阶段（拆成多条，phase=行内 stage_id 的列表）；
    A/B 的 phase 来自目录名/文件名后缀。
    """
    out: list[dict] = []
    for layout in layouts:
        root = root_of(layout)
        if not root.is_dir():
            continue
        if layout == "new":
            for d in sorted(root.iterdir()):
                tr = d / "trace" / "speedrush.jsonl"
                if not tr.is_file():
                    continue
                sm = d / "trace" / "speedrush_summary.jsonl"
                out.append({"layout": "new", "session": d.name,
                            "phases": [s.get("stage_id") for s in stages(tr)],
                            "trace": tr, "summary": sm if sm.is_file() else None})
        elif layout == "A":
            for d in sorted(root.iterdir()):
                if not d.is_dir():
                    continue
                m = _STAGE_DIR_RE.match(d.name)
                if not m:
                    continue
                trs = sorted(d.glob("trace_*.jsonl"))
                if not trs:
                    continue
                gs = d / "game_summary.jsonl"
                out.append({"layout": "A", "session": m.group(1),
                            "phase": int(m.group(2)) if m.group(2) else None,
                            "trace": trs[0],
                            "summary": gs if gs.is_file() else None})
        elif layout == "B":
            for tr in sorted(root.glob("trace_*.jsonl")):
                m = _FLAT_RE.match(tr.name)
                if not m:
                    continue
                out.append({"layout": "B", "session": m.group(1),
                            "phase": int(m.group(2)) if m.group(2) else None,
                            "trace": tr, "summary": None})
    return out


def summary_rows(sess: dict | Path) -> list[dict]:
    """局汇总行（自动挑新旧文件名）。"""
    p = sess.get("summary") if isinstance(sess, dict) else sess
    if p is None or not Path(p).is_file():
        return []
    return [json.loads(x) for x in
            Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def pair_phases(sess_session: str, layout: str = "A") -> dict[int, Path]:
    """A/B 布局：同一时间戳的 p1/p2 目录配成一对（旧「一局」语义）。"""
    root = root_of(layout)
    pair: dict[int, Path] = {}
    if layout == "A":
        for d in sorted(root.glob(f"{sess_session}_p*")):
            m = _STAGE_DIR_RE.match(d.name)
            if m and m.group(2) and sorted(d.glob("trace_*.jsonl")):
                pair[int(m.group(2))] = sorted(d.glob("trace_*.jsonl"))[0]
    elif layout == "B":
        for tr in sorted(root.glob(f"trace_{sess_session}_p*.jsonl")):
            m = _FLAT_RE.match(tr.name)
            if m and m.group(2):
                pair[int(m.group(2))] = tr
    return pair


if __name__ == "__main__":
    for s in sessions():
        print(s["layout"], s["session"], s.get("phase", s.get("phases")), s["trace"])
