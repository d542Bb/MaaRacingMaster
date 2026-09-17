#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ROI Studio 帧库（与内容无关的会话/帧/模板名白名单 + 目录穿越防护）。

移植自 v3 Studio `tools/navkit/core/session.py`。**两套帧库真源形态**共用本模块：

- **debug 截图会话**（treasure 校准台，默认形态）：会话目录名 `YYYYMMDD_HHMMSS`
  （如 `20260812_183611`）；`session_` 前缀为遗留独立 trace 会话形态，保持可读。
  含 `raw/` 的是截图会话，仅含 `trace.jsonl` 的是纯决策会话；raw 帧文件名必须是
  `NNNN_raw.{png,jpg,jpeg,webp}`（只认 png 会让 jpg 帧全黑——历史坑）。
- **speedrush 演示会话**（HUD 只读复核，`SPEEDRUSH_*` 常量）：会话目录名带阶段后缀
  `YYYYMMDD_HHMMSS_p<N>`，帧在 `frames/`，帧名是纯 6 位序号 `NNNNNN.jpg`，
  索引文件是 `frames.jsonl`（见 `plugins/speedrush/recorder.py` 的产出形态）。

四件事——会话名正则、帧目录名、帧名正则、会话在场判据（帧目录或索引文件）——**由调用方
注入** `SessionBrowser`，默认值即上面的 debug 形态（既有调用方零改动）。这样做的理由：
**穿越防护只允许有一份实现**（`is_relative_to` + 会话/帧双白名单），而两套真源的命名规则
不同；把命名当参数、把防护留在类里，比复制一个平行浏览器类安全。

帧库根由调用方注入（treasure = `core.paths.debug_dir()/"treasure"`、
speedrush = `core.paths.data_dir()/"speedrush"/"demos"`），本模块不硬编码路径。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 会话目录名（debug/<module>/<会话>/），如 20260812_183611；兼容遗留 session_ 前缀形态
SESSION_RE = re.compile(r"^(?:session_)?\d{8}_\d{6}$")
# raw 帧文件名；同时放行 png/jpg/jpeg/webp，后端按扩展名回推真实文件
RAW_RE = re.compile(r"^\d{4}_raw\.(png|jpg|jpeg|webp)$")
# 模板名（模块资源目录内 .png）
TPL_RE = re.compile(r"^[\w\-]+\.png$")

# ---- speedrush 演示会话（recorder 产出形态，供 HUD 只读复核注入） ----
# 会话名带阶段后缀（一局两个驾驶阶段各一会话）；帧在 frames/；帧名是纯 6 位序号
# （`f"{seq:06d}.jpg"`）；索引文件 frames.jsonl（与 debug 的 trace.jsonl 同角色）。
SPEEDRUSH_SESSION_RE = re.compile(r"^\d{8}_\d{6}_p\d+$")
SPEEDRUSH_FRAME_RE = re.compile(r"^\d{6}\.jpg$")
SPEEDRUSH_FRAME_DIR = "frames"
SPEEDRUSH_TRACE_NAME = "frames.jsonl"


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
    """遍历一个帧库根目录，安全列出会话与帧，解析可读路径。

    布局由构造参数注入（默认 = debug 截图会话形态，见模块 docstring）：
    会话名正则 `session_re`、帧名正则 `frame_re`、帧目录名 `frame_dir`、
    会话在场判据用的索引文件名 `trace_name`。**穿越防护与白名单判定不随参数变化**。

    使用：
        browser = SessionBrowser(debug_root)   # debug_root = user_data/debug/<module>
        browser = SessionBrowser(demos_root,   # speedrush = user_data/data/speedrush/demos
                                 session_re=SPEEDRUSH_SESSION_RE,
                                 frame_re=SPEEDRUSH_FRAME_RE,
                                 frame_dir=SPEEDRUSH_FRAME_DIR,
                                 trace_name=SPEEDRUSH_TRACE_NAME)
        sessions = browser.list_sessions()
        raws = browser.list_raw(session)
        ref = browser.resolve_raw(session, name)   # 非法则返回 None（绝不返回越权路径）
    """

    def __init__(self, debug_root: Path, *, session_re: re.Pattern[str] = SESSION_RE,
                 frame_re: re.Pattern[str] = RAW_RE, frame_dir: str = "raw",
                 trace_name: str = "trace.jsonl"):
        self.debug_root = Path(debug_root)
        self.session_re = session_re
        self.frame_re = frame_re
        self.frame_dir = frame_dir
        self.trace_name = trace_name

    # ---- 会话 ----
    def _session_present(self, session_dir: Path) -> bool:
        """会话在场的判据：含帧目录（截图/录制会话）或含索引文件（纯流水会话）。"""
        return ((session_dir / self.frame_dir).is_dir()
                or (session_dir / self.trace_name).is_file())

    def list_sessions(self) -> list[str]:
        """列会话名（按时间戳降序）。

        截图会话（含帧目录）与独立流水会话（仅含索引文件）都是合法会话；两者皆无的
        空目录/非法名被排除。遗留 `session_` 前缀目录与新命名混合时按去掉前缀后的
        时间戳排序；speedrush 的阶段后缀 `_pN` 参与排序但不影响时间戳先后。
        """
        if not self.debug_root.is_dir():
            return []
        return sorted(
            (p.name for p in self.debug_root.iterdir()
             if p.is_dir() and self.session_re.match(p.name)
             and self._session_present(p)),
            key=lambda n: n.removeprefix("session_"),
            reverse=True,
        )

    def has_frames(self, session: str) -> bool:
        """会话是否含截图/录制帧目录（False = 纯流水会话）。"""
        return (bool(self.session_re.match(session))
                and (self.debug_root / session / self.frame_dir).is_dir())

    # ---- 帧 ----
    def _frames_dir(self, session: str) -> Path | None:
        if not self.session_re.match(session):
            return None
        frames = (self.debug_root / session / self.frame_dir).resolve()
        return frames if frames.is_dir() else None

    def list_raw(self, session: str) -> list[str]:
        """列出会话内合法帧文件名（升序）。非法会话返回空。"""
        frames = self._frames_dir(session)
        if frames is None:
            return []
        return sorted(p.name for p in frames.iterdir()
                      if p.is_file() and self.frame_re.match(p.name))

    def resolve_raw(self, session: str, name: str) -> Path | None:
        """解析一张帧到绝对路径；越权/非法返回 None（供后端读图前做白名单检查）。

        返回的路径保证位于 `<root>/<session>/<frame_dir>` 内（is_relative_to 严格判断）。
        """
        if not (self.session_re.match(session) and self.frame_re.match(name)):
            return None
        base = self._frames_dir(session)
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
