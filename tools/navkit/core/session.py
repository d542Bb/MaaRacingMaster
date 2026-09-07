#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话与文件白名单（NavKit Core · 与内容无关）。

把「哪个名字是合法会话 / 哪些 raw 帧文件允许读 / 目录穿越防护」收敛成纯逻辑，
供任何模块的调试浏览器复用。不从属于 treasure/racing 的任何内容知识。

三个契约（迁移自 NavKit 控制台/server.py 的白名单，保持严格防护语义）：
- 会话目录名必须是 `YYYYMMDD_HHMMSS` 形态（如 `20260812_183611`），与
  `core/navkit/trace.py` 写侧同形；`session_` 前缀为遗留独立 trace 会话形态，
  保持可读。含 `raw/` 的是截图会话，仅含 `trace.jsonl` 的是纯决策会话。
- raw 帧文件名必须是 `NNNN_raw.{png,jpg,jpeg,webp}`（原始抓帧格式由模块决定，这里同时放行）。
- 一切路径解析均以 `is_relative_to` 严格限定在会话 raw 目录内，防同前缀目录绕过 / 目录穿越。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 会话目录名（debug/<module>/<会话>/），如 20260812_183611；
# 兼容遗留 session_ 前缀形态（navkit 独立 trace 会话的历史命名）
SESSION_RE = re.compile(r"^(?:session_)?\d{8}_\d{6}$")
# raw 帧文件名；同时放行 png/jpg/webp，后端按扩展名回推真实文件（避免换格式改白名单）
RAW_RE = re.compile(r"^\d{4}_raw\.(png|jpg|jpeg|webp)$")
# 模板名（模块资源目录内 .png）
TPL_RE = re.compile(r"^[\w\-]+\.png$")


@dataclass(frozen=True)
class SessionInfo:
    """一个调试会话的描述（供列表展示）。"""

    name: str

    @classmethod
    def is_valid_name(cls, name: str) -> bool:
        return bool(SESSION_RE.match(name))


@dataclass(frozen=True)
class RawFrameRef:
    """一张已校验的 raw 帧引用：会话 + 文件名 + 解析后绝对路径（可读）。"""

    session: str
    name: str
    path: Path


class SessionBrowser:
    """遍历一个模块的调试根目录，安全列出会话与 raw 帧，解析可读路径。

    使用：
        browser = SessionBrowser(debug_root)   # debug_root = user_data/debug/<module>
        sessions = browser.list_sessions()
        raws = browser.list_raw(session)
        ref = browser.resolve_raw(session, name)   # 非法则返回 None（绝不返回越权路径）
    """

    def __init__(self, debug_root: Path):
        self.debug_root = Path(debug_root)

    # ---- 会话 ----
    def list_sessions(self) -> list[str]:
        """列会话名（按时间戳降序）。

        截图会话（含 `raw/`）与独立 trace 会话（仅含 `trace.jsonl`）都是合法
        会话；两者皆无的空目录/非法名被排除。遗留 `session_` 前缀目录与新命名
        混合时按去掉前缀后的时间戳排序。
        """
        if not self.debug_root.is_dir():
            return []
        return sorted(
            (p.name for p in self.debug_root.iterdir()
             if p.is_dir() and SESSION_RE.match(p.name)
             and ((p / "raw").is_dir() or (p / "trace.jsonl").is_file())),
            key=lambda n: n.removeprefix("session_"),
            reverse=True,
        )

    def has_frames(self, session: str) -> bool:
        """会话是否含截图帧目录 raw/（False = 纯决策流水会话）。"""
        return bool(SESSION_RE.match(session)) and (self.debug_root / session / "raw").is_dir()

    # ---- raw 帧 ----
    def _raw_dir(self, session: str) -> Path | None:
        if not SESSION_RE.match(session):
            return None
        raw = (self.debug_root / session / "raw").resolve()
        return raw if raw.is_dir() else None

    def list_raw(self, session: str) -> list[str]:
        """列出会话内合法 raw 帧文件名（升序）。非法会话返回空。"""
        raw = self._raw_dir(session)
        if raw is None:
            return []
        return sorted(p.name for p in raw.iterdir() if p.is_file() and RAW_RE.match(p.name))

    def resolve_raw(self, session: str, name: str) -> Path | None:
        """解析一张 raw 帧到绝对路径；越权/非法返回 None（供后端读图前做白名单检查）。

        返回的路径保证位于 `debug_root/<session>/raw` 内（is_relative_to 严格判断）。
        """
        if not (SESSION_RE.match(session) and RAW_RE.match(name)):
            return None
        base = self._raw_dir(session)
        if base is None:
            return None
        p = (base / name).resolve()
        if p.is_file() and p.is_relative_to(base):
            return p
        return None


def list_templates(template_dir: Path) -> list[str]:
    """列出模板目录内的合法 .png 名（升序）。目录不存在返回空。"""
    d = Path(template_dir)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and TPL_RE.match(p.name))