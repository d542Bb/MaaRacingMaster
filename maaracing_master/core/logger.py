#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志模块：Logger 类 + 全局 logger 实例

磁盘写入默认关闭（GUI 内存缓冲不受影响）；由设置页「日志记录」开关或
sidecar 启动时的 profile 回读启用。日志根 = user_data_dir()/logs，
开发版与发行版位置一致（%APPDATA%/MaaRacingMaster/logs）。
"""

from pathlib import Path
from datetime import datetime

from maaracing_master.core.paths import logs_dir


class Logger:
    # 日志级别：TRACE < DEBUG < INFO < WARNING < ERROR
    LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARNING": 3, "ERROR": 4}
    GUI_MIN_LEVEL = "INFO"  # GUI 只显示 INFO 及以上级别

    def __init__(self, log_dir: Path):
        self._log_dir = Path(log_dir)
        self.log_file = None      # 仅在磁盘写入启用后非 None
        self._file_enabled = False
        self._lines = []

    @property
    def file_logging(self) -> bool:
        """当前是否启用磁盘写入。"""
        return self._file_enabled

    def set_file_logging(self, enabled: bool) -> None:
        """开关磁盘写入。

        开启：惰性创建日志目录并新建一个带启动时间戳的日志文件；
        关闭：立即停止写盘（已写文件保留，内存缓冲继续累积，GUI 显示不受影响）。
        """
        self._file_enabled = bool(enabled)
        if self._file_enabled:
            try:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                self.log_file = self._log_dir / f"MRA_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            except OSError:
                self._file_enabled = False   # 目录建不出来（无权限等）：回到关闭态，不干扰主流程
                self.log_file = None
        else:
            self.log_file = None

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        self._lines.append(line)
        if self._file_enabled and self.log_file is not None:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 单条写盘失败静默（内存缓冲仍有），不打断业务

    def get_lines(self, min_level: str = "INFO"):
        """获取日志，可按级别过滤。GUI 默认只显示 INFO 及以上"""
        min_val = self.LEVELS.get(min_level, 2)
        return [line for line in self._lines
                if self.LEVELS.get(self._extract_level(line), 2) >= min_val]

    @staticmethod
    def _extract_level(line: str) -> str:
        """从日志行中提取级别，如 [INFO] → INFO"""
        parts = line.split("] [")
        if len(parts) >= 2:
            return parts[1].split("]")[0]
        return "INFO"


# 全局日志单例（日志根 = 用户数据目录 / logs，开发版与发行版一致；默认不写盘）
logger = Logger(logs_dir())
