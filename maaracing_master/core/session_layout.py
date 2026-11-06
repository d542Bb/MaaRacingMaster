#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话优先取证布局 v5 的公共契约（纯标准库）。

一次程序启动 = 一个会话编号，开关状态不改变它。：

    debug/<会话>/
      index.jsonl                 会话时间线（段/阶段/开关，不含模块私有帧）
      <模块>/<行为>/
        manifest.json             行为清单（必填）
        frames.jsonl              最小帧索引（或清单指向的模块私有索引）
        <模块自定义>

本模块只提供**契约层**：会话编号生成、行为目录校验、清单与帧索引读写、越界
防护。谁产帧谁写索引；统一流向只写清单路径与段边界。

清单最小字段：`schema` / `session` / `module` / `behavior` / `started_at` /
`ended_at` / `frames_index` / `frames_dir`；可选 `runs` / `segments` / `meta`。
帧索引最小字段：`seq` / `ts_mono_ns` / `file`（相对行为目录）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

SESSION_SCHEMA_VERSION = 5
SESSION_TOKEN_FMT = "%Y%m%d_%H%M%S"
SESSION_TOKEN_RE = re.compile(r"^\d{8}_\d{6}(?:_\d+)?$")
BEHAVIOR_RE = re.compile(r"^[a-z0-9_-]+$")
MODULE_RE = re.compile(r"^[a-z0-9_-]+$")

MANIFEST_NAME = "manifest.json"
FRAMES_INDEX_NAME = "frames.jsonl"


def _suffixed(base: str, seq: int) -> str:
    return base if seq <= 1 else f"{base}_{seq}"


def _token_base(token: str) -> str:
    """编号的未撞名部分：`20261103_142233_2` → `20261103_142233`；不合规则原样返回。"""
    parts = token.split("_")
    return "_".join(parts[:2]) if SESSION_TOKEN_RE.match(token) else token


def new_session_token(logs_root: Path, debug_root: Path) -> str:
    """生成进程会话编号；同秒撞上既有日志或调试目录时追加 `_2`、`_3`。"""
    ts = datetime.now().strftime(SESSION_TOKEN_FMT)
    seq = 1
    while True:
        token = _suffixed(ts, seq)
        if not (Path(logs_root) / token).exists() \
                and not (Path(debug_root) / token).exists():
            return token
        seq += 1


def fresh_session_dir(root: Path, token: str) -> Path:
    """在 root 下为会话编号换一个不撞名的目录，撞名一律**换名**、不给既有会话续写。

    复用会让新一批产物覆盖旧的、索引被追加成两批混合（2026-10-30 实机踩过）。
    编号自带撞名后缀时从基础编号重数，不叠后缀（`..._142233_2` 撞上 → `..._142233_3`）。
    """
    base = _token_base(str(token))
    seq = 1
    while True:
        path = Path(root) / _suffixed(base, seq)
        if not path.exists():
            return path
        seq += 1


def session_token_label(token: str) -> str:
    """会话编号 → 给人看的 `2026-11-03 14:22:33`；带撞名后缀时追加 ` (2)`。

    不合规的编号（人工改过、带阶段后缀）原样返回——宁可给难读的真名，不猜内容。
    """
    name = str(token or "")
    if not SESSION_TOKEN_RE.match(name):
        return name
    day, clock, *rest = name.split("_")
    label = (f"{day[0:4]}-{day[4:6]}-{day[6:8]} "
             f"{clock[0:2]}:{clock[2:4]}:{clock[4:6]}")
    return f"{label} ({rest[0]})" if rest else label


def _check_part(value: str, pattern: re.Pattern, what: str) -> str:
    text = str(value or "")
    if not pattern.match(text):
        raise ValueError(f"{what}不合规: {value!r}")
    return text


def behavior_dir(session_root: Path, module: str, behavior: str) -> Path:
    """`debug/<会话>/<模块>/<行为>/`；两段都过白名单，不拼未校验字符串。"""
    module = _check_part(module, MODULE_RE, "模块名")
    behavior = _check_part(behavior, BEHAVIOR_RE, "行为名")
    return Path(session_root) / module / behavior


def _write_json_atomic(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def write_manifest(session_root: Path, module: str, behavior: str, *,
                   started_at: str | None = None,
                   frames_index: str = FRAMES_INDEX_NAME,
                   frames_dir: str | None = ".",
                   runs: list[str] | None = None,
                   segments: list[dict] | None = None,
                   meta: dict | None = None) -> dict:
    """建/覆盖一份行为清单。调用方负责给出模块自己认得的可选字段。"""
    session = Path(session_root).name
    target = behavior_dir(session_root, module, behavior)
    payload = {
        "schema": SESSION_SCHEMA_VERSION,
        "session": session,
        "module": module,
        "behavior": behavior,
        "started_at": started_at or datetime.now().isoformat(timespec="seconds"),
        "ended_at": None,
        "frames_index": frames_index,
        "frames_dir": frames_dir,
    }
    if runs:
        payload["runs"] = list(dict.fromkeys(runs))
    if segments:
        payload["segments"] = list(segments)
    if meta:
        payload["meta"] = dict(meta)
    _write_json_atomic(target / MANIFEST_NAME, payload)
    return payload


def read_manifest(behavior_path: Path) -> dict | None:
    """读并校验一份行为清单；非法/越名返回 None（回放只认清单）。"""
    path = Path(behavior_path)
    if not path.is_dir():
        return None
    if not MODULE_RE.match(path.parent.name) or not BEHAVIOR_RE.match(path.name):
        return None
    try:
        doc = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("schema") != SESSION_SCHEMA_VERSION:
        return None
    if doc.get("module") != path.parent.name or doc.get("behavior") != path.name:
        return None
    return doc


def update_manifest(behavior_path: Path, **updates) -> dict | None:
    """读-改-写清单；只更新调用方给出的键。清单不存在时返回 None。"""
    path = Path(behavior_path)
    doc = read_manifest(path)
    if doc is None:
        return None
    for key, value in updates.items():
        if key in ("schema", "session", "module", "behavior"):
            continue
        doc[key] = value
    _write_json_atomic(path / MANIFEST_NAME, doc)
    return doc


def append_frame_row(behavior_path: Path, row: dict) -> None:
    """向行为帧索引追加一行（JSONL，逐行 flush，崩溃可读）。"""
    path = Path(behavior_path) / FRAMES_INDEX_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()


def read_frame_index(behavior_path: Path, manifest: dict | None = None) -> list[dict]:
    """按清单声明读取帧索引；缺失/坏行跳过（回放按可得证据降级）。"""
    path = Path(behavior_path)
    doc = manifest or read_manifest(path)
    if doc is None:
        return []
    rel = doc.get("frames_index")
    if not isinstance(rel, str) or not rel:
        return []
    target = resolve_behavior_file(path, rel)
    if target is None or not target.is_file():
        return []
    rows: list[dict] = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "seq" in row and "file" in row:
                rows.append(row)
    except OSError:
        return []
    return rows


def resolve_behavior_file(behavior_path: Path, rel: str) -> Path | None:
    """把清单/索引里的相对路径安全解析到行为目录内；越界返回 None。"""
    path = Path(behavior_path)
    try:
        target = (path / rel).resolve()
        root = path.resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(root):
        return None
    return target


def list_behavior_manifests(session_root: Path) -> list[tuple[str, str, Path, dict]]:
    """枚举一个会话下的全部行为：`(模块, 行为, 目录, 清单)`，按模块/行为排序。"""
    root = Path(session_root)
    if not root.is_dir():
        return []
    out: list[tuple[str, str, Path, dict]] = []
    for module_dir in sorted(root.iterdir()):
        if not module_dir.is_dir() or not MODULE_RE.match(module_dir.name):
            continue
        for behavior in sorted(module_dir.iterdir()):
            if not behavior.is_dir() or not BEHAVIOR_RE.match(behavior.name):
                continue
            doc = read_manifest(behavior)
            if doc is not None:
                out.append((module_dir.name, behavior.name, behavior, doc))
    return out


def list_sessions(debug_root: Path) -> list[str]:
    """枚举 `debug/<会话>/`（带行为清单或用例时间线），按时间戳降序。"""
    root = Path(debug_root)
    if not root.is_dir():
        return []
    names = []
    for entry in root.iterdir():
        if not entry.is_dir() or not SESSION_TOKEN_RE.match(entry.name):
            continue
        if list_behavior_manifests(entry) or (entry / "index.jsonl").is_file():
            names.append(entry.name)
    return sorted(names, reverse=True)
