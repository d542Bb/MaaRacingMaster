# -*- coding: utf-8 -*-
"""C 相关会话证据冻结（稀有事件取证）。

**为什么**：日志按会话组保留清理，C 案例一旦出现，常规清理就会把唯一素材
删掉；下游分析要的是原始逐拍输入，不是完成级摘要。冻结目录在 `logs/` 之外
（同盘 `evidence/c_gate/`），普通会话清理不碰。

**契约**：复制源 trace 与局汇总；manifest 记触发、来源、大小、sha256；失败
写 `.error.json` 并返回错误——调用方必须显式告警，不得把"触发了归档"当成
"素材已保存"；只保留最近 `keep` 份，其余按冻结时间清理。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

KEEP = 10
_TRACE_SUFFIX = ".trace.jsonl"
_SUMMARY_SUFFIX = ".summary.jsonl"
_MANIFEST_SUFFIX = ".json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune(root: Path, keep: int) -> list[str]:
    """按冻结时间保留最近 keep 份（trace/manifest/summary 同组删）。"""
    traces = sorted(root.glob(f"*{_TRACE_SUFFIX}"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for p in traces[max(keep, 1):]:
        stem = p.name[:-len(_TRACE_SUFFIX)]
        for q in (p,
                  root / f"{stem}{_MANIFEST_SUFFIX}",
                  root / f"{stem}{_SUMMARY_SUFFIX}"):
            try:
                if q.exists():
                    q.unlink()
                    removed.append(q.name)
            except OSError:
                pass
    return removed


def archive_session_evidence(session_dir, phase: int, trigger: dict,
                             *, config: dict | None = None,
                             keep: int = KEEP) -> dict:
    """冻结一次会话的 C 证据。

    返回 ``{ok, trace, manifest, bytes, pruned, error}``；失败时 ``ok=False``
    且已写 ``.error.json``（写失败也不抛，错误仍在返回值里）。
    """
    session = Path(session_dir)
    root = session.parent.parent / "evidence" / "c_gate"
    src = session / "trace" / "speedrush.jsonl"
    stem = f"{session.name}_p{phase}"
    dest = root / f"{stem}{_TRACE_SUFFIX}"
    manifest = root / f"{stem}{_MANIFEST_SUFFIX}"
    summary_src = session / "trace" / "speedrush_summary.jsonl"
    summary_dest = root / f"{stem}{_SUMMARY_SUFFIX}"
    try:
        if not src.is_file():
            raise FileNotFoundError(f"源 trace 不存在：{src}")
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if summary_src.is_file():
            shutil.copyfile(summary_src, summary_dest)
        rec = {
            "archived_at": time.strftime("%Y%m%d_%H%M%S"),
            "session": session.name,
            "phase": phase,
            "trigger": trigger,
            "config": config,
            "source": str(src),
            "trace": {"file": dest.name, "bytes": dest.stat().st_size,
                      "sha256": _sha256(dest)},
            "summary": ({"file": summary_dest.name,
                         "bytes": summary_dest.stat().st_size,
                         "sha256": _sha256(summary_dest)}
                        if summary_dest.exists() else None),
            "keep": keep,
        }
        manifest.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str),
                            encoding="utf-8")
        pruned = _prune(root, keep)
        return {"ok": True, "trace": str(dest), "manifest": str(manifest),
                "bytes": rec["trace"]["bytes"], "pruned": pruned,
                "error": None}
    except Exception as exc:  # noqa: BLE001 —— 证据丢失必须显式回报
        try:
            if dest.exists():
                dest.unlink()          # 半截副本不能冒充素材
        except OSError:
            pass
        err = {"archived_at": time.strftime("%Y%m%d_%H%M%S"),
               "session": session.name, "phase": phase, "trigger": trigger,
               "config": config, "source": str(src), "error": repr(exc)}
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / f"{stem}.error.json").write_text(
                json.dumps(err, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8")
        except OSError:
            pass
        return {"ok": False, "trace": None, "manifest": None, "bytes": 0,
                "pruned": [], "error": repr(exc)}
