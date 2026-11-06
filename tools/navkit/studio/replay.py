#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5 会话回放数据层：只读清单与帧索引，不解析模块私有格式。

对外三件事：
- `sessions()`    枚举 `debug/<会话>/`；
- `behaviors()`   枚举会话内带清单的 `<模块>/<行为>`；
- `frames()`      按清单读帧索引，给出可按序号解析的帧路径。

帧文件解析一律经 `session_layout.resolve_behavior_file` 限定在行为目录内；
播放器只依赖清单与最小帧索引（序号、单调时刻、相对路径）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from maaracing_master.core import session_layout as sl
from maaracing_master.core.paths import debug_dir

FRAME_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class Behavior:
    session: str
    module: str
    behavior: str
    frames: int
    started_at: str | None
    ended_at: str | None


class ReplayIndex:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else debug_dir()

    def _session_dir(self, session: str) -> Path | None:
        if not sl.SESSION_TOKEN_RE.match(session or ""):
            return None
        path = self.root / session
        return path if path.is_dir() else None

    def sessions(self) -> list[str]:
        return sl.list_sessions(self.root)

    def behaviors(self, session: str) -> list[dict]:
        root = self._session_dir(session)
        if root is None:
            return []
        out = []
        for module, behavior, bdir, manifest in sl.list_behavior_manifests(root):
            rows = sl.read_frame_index(bdir, manifest)
            out.append(Behavior(
                session=session, module=module, behavior=behavior,
                frames=len(rows),
                started_at=manifest.get("started_at"),
                ended_at=manifest.get("ended_at"),
            ).__dict__)
        return out

    def manifest(self, session: str, module: str, behavior: str) -> dict | None:
        root = self._session_dir(session)
        if root is None:
            return None
        try:
            bdir = sl.behavior_dir(root, module, behavior)
        except ValueError:
            return None
        return sl.read_manifest(bdir)

    def frames(self, session: str, module: str, behavior: str) -> list[dict]:
        root = self._session_dir(session)
        if root is None:
            return []
        try:
            bdir = sl.behavior_dir(root, module, behavior)
        except ValueError:
            return []
        manifest = sl.read_manifest(bdir)
        if manifest is None:
            return []
        return sl.read_frame_index(bdir, manifest)

    def resolve_frame(self, session: str, module: str, behavior: str,
                      seq: int) -> Path | None:
        root = self._session_dir(session)
        if root is None:
            return None
        try:
            bdir = sl.behavior_dir(root, module, behavior)
        except ValueError:
            return None
        row = next((r for r in self.frames(session, module, behavior)
                    if int(r.get("seq", -1)) == int(seq)), None)
        if row is None:
            return None
        target = sl.resolve_behavior_file(bdir, str(row.get("file") or ""))
        if target is None or not target.is_file():
            return None
        if target.suffix.lower() not in FRAME_SUFFIXES:
            return None
        return target

    def timeline(self, session: str) -> list[dict]:
        root = self._session_dir(session)
        if root is None:
            return []
        path = root / "index.jsonl"
        if not path.is_file():
            return []
        rows: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except OSError:
            return []
        return rows
