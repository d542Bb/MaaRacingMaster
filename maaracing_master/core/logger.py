#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志模块：Logger 类 + 全局 logger 实例

磁盘写入默认关闭（GUI 内存缓冲不受影响）；由设置页「日志记录」开关或
sidecar 启动时的 profile 回读启用。日志根 = user_data_dir()/logs，
开发版与发行版位置一致（%APPDATA%/MaaRacingMaster/logs）。

设计要点：
- 进程内行缓冲用有界 deque（环形），长时间运行不随日志量增长。
- GUI 增量读取用单调序列号（_seq）作游标，而非行数；环形回绕后仍不重不漏。
- 落盘**单流全量**：一次开启只建一个 `MaaRM_<ts>.log`，各级别按发生顺序写入。
  不按级别分档——诊断是顺序的（「失败之前发生了什么」），按级别切分会把时序切断
  且不可逆：切出的两份文件各自都读不通（DEBUG 档没有业务锚点，INFO 档没有诊断
  细节）。分级过滤留给读侧（GUI）与导出侧，写侧只保证时序完整。
- 按大小轮转 + 启动时按会话组保留清理，占用有上限。
- 写盘持句柄 + Lock 跨线程安全。
"""

import re
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock

from maaracing_master.core.paths import logs_dir


class _TierFile:
    """单档落盘句柄：持句柄 + line-buffering + 大小轮转。

    轮转：超过 MAX_BYTES 时把当前文件依次后移为 `.1/.2/...`，最多保留
    backup_count 份备份，然后重开新句柄。写失败静默（I6 语义）。
    """

    def __init__(self, path: Path, max_bytes: int, backup_count: int):
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.fh = None

    def _rotate(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
        # 从最老备份开始逐个后移：.N -> .N+1，当前主文件 -> .1
        for i in range(self.backup_count, 0, -1):
            src = self.path if i == 1 else Path(f"{self.path}.{i - 1}")
            dst = Path(f"{self.path}.{i}")
            if src.exists():
                try:
                    src.replace(dst)
                except OSError:
                    pass
        try:
            self.fh = open(str(self.path), "a", encoding="utf-8", buffering=1)
        except OSError:
            self.fh = None

    def write(self, line: str) -> None:
        if self.fh is None:
            try:
                self.fh = open(str(self.path), "a", encoding="utf-8", buffering=1)
            except OSError:
                return  # 打不开（无权限等）：静默跳过本行
        try:
            if self.fh.tell() > self.max_bytes:
                self._rotate()
            self.fh.write(line + "\n")
        except OSError:
            pass

    def close(self) -> None:
        fh, self.fh = self.fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


class Logger:
    # 日志级别：TRACE < DEBUG < INFO < WARNING < ERROR
    LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARNING": 3, "ERROR": 4}
    GUI_MIN_LEVEL = "INFO"  # GUI 只显示 INFO 及以上级别
    DEFAULT_CHANNEL = "app" # 通道名：core 通用用 app；业务层用模块 id（core 不校验、不外显）
    # 进程内行缓冲上限。实测峰值约 4 行/秒，按最坏情况放大 25 倍取 100 行/秒，
    # 5000 行对应 50 秒最坏积压，安全裕度充足。
    BUFFER_CAPACITY = 5000
    # 落盘参数：单文件轮转上限 / 备份份数 / 启动保留的会话组数。
    MAX_BYTES = 16 * 1024 * 1024   # 16 MB（实测单档 <100KB，此为防跑飞安全阀）
    BACKUP_COUNT = 3               # 单会话最多 4 份 × 16MB = 64MB
    KEEP_SESSIONS = 20             # 会话组按 MaaRM_<ts> 前缀归组，整组同清
    # 会话组文件形态：MaaRM_+YYYYMMDD_HHMMSS，主档 .log，轮转尾标 .N。
    # `(?:\.debug)?` 兼容旧版分档产物，让保留清理顺带回收历史 .debug.log。
    _SESSION_RE = re.compile(r"MaaRM_(\d{8}_\d{6})(?:\.debug)?\.log(?:\.\d+)?$")

    def __init__(self, log_dir: Path):
        self._log_dir = Path(log_dir)
        self.log_file = None          # 兼容属性：INFO 档路径（磁盘写入启用后非 None）
        self._file_enabled = False
        # 环形缓冲：元素为 (seq, line)。deque 满时自动丢弃最旧元素。
        self._lines = deque(maxlen=self.BUFFER_CAPACITY)
        self._seq = 0                 # 每写一行 +1，只增不减；GUI 增量读取的单调游标
        self._lock = Lock()
        self._session_ts = None       # 当前会话时间戳（YYYYMMDD_HHMMSS），用于跳过一次当前会话清理
        self._tier: _TierFile | None = None   # 单流写盘句柄（未启用写盘时为 None）
        self._channel_levels: dict[str, str] = {}  # 通道 → 级别（运行时调级）；查不到回落 _min_level
        self._min_level: str | None = None        # 全局最低记录级别；None = 全记录（兼容旧行为）

    @property
    def file_logging(self) -> bool:
        """当前是否启用磁盘写入。"""
        return self._file_enabled

    def set_file_logging(self, enabled: bool) -> None:
        """开关磁盘写入。

        开启：惰性创建日志目录并为本次开启新建一个日志文件（单流全量，各级别按
        发生顺序写入）；关闭：立即停止写盘（已写文件保留，内存缓冲继续累积，
        GUI 显示不受影响）。
        """
        self._file_enabled = bool(enabled)
        if self._file_enabled:
            try:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self._session_ts = ts          # 与 _SESSION_RE 的 group(1) 同形（带下划线）
                self.log_file = self._log_dir / f"MaaRM_{ts}.log"
                # 注意：传字符串路径用内置 open（buffering=1 行缓冲；本环境 open 的
                # line_buffering 关键字被拦截，故用等价的标准 buffering=1）。
                self._tier = _TierFile(self.log_file, self.MAX_BYTES, self.BACKUP_COUNT)
            except OSError:
                self._file_enabled = False   # 目录建不出来（无权限等）：回到关闭态，不干扰主流程
                self.log_file = None
                self._tier = None
                self._session_ts = None
        else:
            self._close_tier()
            self.log_file = None
            self._session_ts = None

    def _close_tier(self) -> None:
        """关闭写盘句柄（幂等）。假设持有 `_lock` 或无并发写盘。"""
        tier, self._tier = self._tier, None
        if tier is not None:
            tier.close()

    def close(self) -> None:
        """关闭写盘句柄并 flush 尾部（进程退出路径调用）。"""
        with self._lock:
            self._close_tier()

    def prune_sessions(self, keep: int = KEEP_SESSIONS) -> None:
        """按会话组清理 logs/ 下旧日志，保留最近 `keep` 组。

        仅匹配 `MaaRM_<14位数字>(.debug)?.log(.N)?` 形态的文件；当前正在写入的
        会话永不删除；不碰 sidecar_stderr.log 等其它文件。删除失败不中断业务。
        """
        if not self._log_dir.exists():
            return
        # 按会话时间戳归组
        groups: dict[str, list] = {}
        for p in self._log_dir.iterdir():
            if p.is_dir() or not p.name.startswith("MaaRM_"):
                continue
            m = self._SESSION_RE.match(p.name)
            if not m:
                continue
            groups.setdefault(m.group(1), []).append(p)
        if not groups:
            return
        ordered = sorted(groups.keys(), reverse=True)  # 新的在前
        for ts in ordered[keep:]:
            if ts == self._session_ts:
                continue  # 当前会话永不删
            for p in groups[ts]:
                try:
                    p.unlink()
                except OSError:
                    pass  # 删除失败不中断业务

    def log(self, msg: str, level: str = "INFO", channel: str | None = None):
        """记录一行日志。

        channel=None → DEFAULT_CHANNEL（app）。通道管「要不要打」（调级）：
        低于该通道有效级别的行直接丢弃（不进内存、不落盘）。通道级别查不到时
        回落到 _min_level（None=全记录）。级别不参与写侧落点判定——落盘是单流
        全量，级别过滤只发生在读侧（GUI）与导出侧。
        """
        channel = channel or self.DEFAULT_CHANNEL
        eff = self._channel_levels.get(channel, self._min_level)
        if eff is not None and self.LEVELS.get(level, 2) < self.LEVELS.get(eff, 2):
            return  # 通道级别过滤
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        with self._lock:
            self._seq += 1
            self._lines.append((self._seq, line))
            if self._tier is not None:
                self._tier.write(line)

    def set_channel_level(self, channel: str, level: str) -> None:
        """运行时调整某通道的最低记录级别（channel 名不透明字符串）。"""
        self._channel_levels[channel] = level

    def clear_channel_level(self, channel: str) -> None:
        """移除通道级别覆盖，回落全局 _min_level。"""
        self._channel_levels.pop(channel, None)

    def get_lines(self, min_level: str = "INFO"):
        """获取日志，可按级别过滤。GUI 默认只显示 INFO 及以上"""
        min_val = self.LEVELS.get(min_level, 2)
        with self._lock:
            return [line for _seq, line in self._lines
                    if self.LEVELS.get(self._extract_level(line), 2) >= min_val]

    def get_lines_since(self, seq: int, min_level: str = "INFO"):
        """返回单调序列号 `seq` 之后满足级别的行，及最新游标。

        返回 (lines, new_seq, truncated)：
          - lines: list[str]，seq 严格大于给定 seq 且级别满足的行。
          - new_seq: 本次快照的总写入数，作为下一次 fetch 的游标。
          - truncated: 给定 seq 已落在环形缓冲覆盖范围之外（GUI 落后过多），
            由调用方据此插入提示分隔，而非静默丢行。
        """
        min_val = self.LEVELS.get(min_level, 2)
        with self._lock:
            out = []
            truncated = False
            if self._lines:
                oldest_seq = self._lines[0][0]
                # 游标 seq 的语义是「只要 seq 之后的行」，故最早需要的是 seq+1；
                # 只有 seq+1 也已落出缓冲才算真丢行，即 seq < oldest_seq - 1。
                # 写成 seq < oldest_seq 会误判边界：缓冲未回绕时 oldest_seq 恒为 1，
                # 首帧游标 0 即落在边界上，会让 GUI 每次会话首次拉取都插一条假的截断提示。
                if seq < oldest_seq - 1:
                    truncated = True
                for s, line in self._lines:
                    if s <= seq:
                        continue
                    if self.LEVELS.get(self._extract_level(line), 2) >= min_val:
                        out.append(line)
            new_seq = self._seq
        return out, new_seq, truncated

    @staticmethod
    def _extract_level(line: str) -> str:
        """从日志行中提取级别，如 [INFO] → INFO"""
        parts = line.split("] [")
        if len(parts) >= 2:
            return parts[1].split("]")[0]
        return "INFO"


# 全局日志单例（日志根 = 用户数据目录 / logs，开发版与发行版一致；默认不写盘）
logger = Logger(logs_dir())