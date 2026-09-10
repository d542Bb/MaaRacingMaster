#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit 决策流水记录器（S2，纯标准库）。

每帧一行 JSONL，默认常开；会话保留由 `keep_sessions` 控制，默认 10。
写入目录由调用方传入（运行时应传 C 盘既有 debug 目录），本模块不自行决定路径。

设计约束：
- 只接收普通 Python 值，不能把 numpy / cv2 对象写进 trace；
- 单行追加，崩溃前已有帧仍可读；
- 完成会话后按文件名时间排序清理旧会话；
- 不记录绝对路径、密钥或个人隐私。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

__all__ = ["FrameTrace", "TraceWriter", "json_safe"]

# 会话目录名统一形态：与 debug 截图会话同为 YYYYMMDD_HHMMSS；
# `session_` 前缀为历史独立 trace 会话的遗留形态，保持可读（写侧不再生成）。
_SESSION_RE = re.compile(r"^(?:session_)?\d{8}_\d{6}$")


def json_safe(value: Any) -> Any:
    """把常见 numpy 标量/序列转换成 JSON 基础类型，不导入 numpy。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [json_safe(v) for v in value]
    # numpy 标量有 item()；numpy 数组有 tolist()。用鸭子类型隔离可选依赖。
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return json_safe(item())
        except Exception:
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return json_safe(tolist())
        except Exception:
            pass
    return str(value)


class FrameTrace:
    """单帧决策流水记录。

    记录的可还原信息：阶段、回合、每锚点分数、命中锚点、
    当前感知集合、点击意图、点击结果。额外字段通过 `extra` 放入，
    但必须是 JSON 可序列化值。
    """

    __slots__ = ("frame", "stage", "round_no", "scores", "hit_anchor", "active_used",
                 "intent", "click_result", "timestamp_ms", "extra")

    def __init__(
        self,
        *,
        frame: int,
        stage: str | None,
        round_no: int | None,
        scores: Mapping[str, Any] | None = None,
        hit_anchor: str | None = None,
        active_used: Any = (),
        intent: Mapping[str, Any] | None = None,
        click_result: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.frame = int(frame)
        self.stage = stage
        self.round_no = round_no
        self.scores = dict(scores or {})
        self.hit_anchor = hit_anchor
        self.active_used = active_used
        self.intent = intent
        self.click_result = click_result
        self.timestamp_ms = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        self.extra = dict(extra or {})

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "frame": self.frame,
            "timestamp_ms": self.timestamp_ms,
            "stage": self.stage,
            "round_no": self.round_no,
            "scores": json_safe(self.scores),
            "hit_anchor": self.hit_anchor,
            "active_used": json_safe(self.active_used),
            "intent": json_safe(self.intent),
            "click_result": json_safe(self.click_result),
        }
        data.update(json_safe(self.extra))
        return data


class TraceWriter:
    """按会话写入 `trace.jsonl`，并清理超过 keep_sessions 的旧会话。

    两种落点模式：
    - 传入 `session_dir`（既有会话目录，如 debug 帧的 `<ts>/`）：trace.jsonl 直接
      写进该目录，与 raw 帧同会话对齐；prune 不触碰外部会话目录。
    - 未传 `session_dir`：在 `root` 下自建 `YYYYMMDD_HHMMSS/` 子目录（无 debug
      会话时的独立 trace 会话，与截图会话同名形态、以无 `raw/` 区分），prune 按
      keep_sessions 清理。
    """

    def __init__(self, root: str | Path, *, keep_sessions: int = 10,
                 session_name: str | None = None,
                 session_dir: str | Path | None = None):
        self.root = Path(root)
        self.keep_sessions = max(1, int(keep_sessions))
        self.root.mkdir(parents=True, exist_ok=True)
        if session_dir is not None:
            self.session_dir = Path(session_dir)
            self.session_dir.mkdir(parents=True, exist_ok=True)
        else:
            if session_name is None:
                session_name = datetime.now().strftime("%Y%m%d_%H%M%S")
            if not _SESSION_RE.match(session_name):
                raise ValueError(
                    "session_name 必须形如 YYYYMMDD_HHMMSS"
                    "（遗留形态 session_YYYYMMDD_HHMMSS 仍可读）"
                )
            self.session_dir = self.root / session_name
            self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "trace.jsonl"
        self._file = open(self.path, "a", encoding="utf-8", buffering=1)
        self._closed = False

    def write(self, trace: FrameTrace | Mapping[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("TraceWriter 已关闭")
        data = trace.as_dict() if isinstance(trace, FrameTrace) else json_safe(dict(trace))
        self._file.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._file.flush()
        self._file.close()
        self._closed = True
        self.prune()

    def prune(self) -> list[Path]:
        """保留最近 keep_sessions 个独立 trace 会话（含当前会话），返回删除项。

        候选须同时满足：名称符合会话形态（含遗留 `session_` 前缀）、目录内有
        `trace.jsonl`、目录内**没有** `raw/`——有 raw/ 的是 debug 截图会话，
        trace 清理永不触碰；当前写入会话即使落入超额区间也永不删除。
        """
        candidates = []
        for p in self.root.iterdir():
            if not p.is_dir() or not _SESSION_RE.match(p.name):
                continue
            if (p / "raw").exists() or not (p / "trace.jsonl").is_file():
                continue
            candidates.append(p)
        candidates.sort(key=lambda p: p.name.removeprefix("session_"), reverse=True)
        current = self.session_dir.resolve()
        removed: list[Path] = []
        for old in candidates[self.keep_sessions:]:
            if old.resolve() == current:
                continue
            try:
                shutil.rmtree(old)
                removed.append(old)
            except OSError:
                # 清理失败不影响当前 trace；下次 close 再尝试。
                continue
        return removed

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
