#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显卡状态探针——驾驶阶段逐 0.5s 采样 nvidia-smi，行进独立流水汇（模块名 "gpu"）。

背景（2026-11-07 离线复现实验，commit 3dc0f8b）：8GB 卡显存近上限时的大块分配
突发会触发驱动级停摆 0.4~2s——本进程全部 GPU 上下文（DML 会话、WGC 采集）冻结，
生产停顿验证靠本探针：停顿时刻若显存 >7.4GB 或利用率塌 0，即实锤触发源。

设计要点：
- 子进程轮询 nvidia-smi（零新增依赖）；**单次调用时长本身是信号**——驱动停摆
  时外部采样会被一起卡住，``call_ms`` 飙高 + 行间隔拉大就是停摆窗口；
- 无日志会话不采样（单元测试与无会话场景零开销）；nvidia-smi 不存在直接放弃；
- 超时 5s 兜底：驱动挂死时不至永久阻塞，停不动的线程是 daemon 不拦退出。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

from maaracing_master.core.logger import logger
from maaracing_master.core.trace_sink import get_trace_sink

_QUERY = ("utilization.gpu,memory.used,memory.total,temperature.gpu,"
          "clocks.sm,power.draw")
_CALL_TIMEOUT_S = 5.0


def _num(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class GpuProbe:
    """nvidia-smi 采样线程：start/stop 成对；行进 "gpu" 流水汇。

    行字段：util（%）、vram_mib / vram_total_mib、temp、clock_sm（MHz）、
    power_w、call_ms（本次外部调用时长，停摆哨）、err（调用失败原因）。
    协议头时间戳由流水汇补齐，与 speedrush 流水同钟可离线对齐。
    """

    def __init__(self, interval_s: float = 0.5):
        self._interval_s = interval_s
        self._sink = get_trace_sink("gpu")
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        if self._th is not None or not self._supported():
            return
        if logger.session_dir is None:
            return          # 无日志会话（单元测试/旁路调用）不采样，零开销
        self._stop.clear()
        self._th = threading.Thread(target=self._run, daemon=True,
                                    name="gpu-probe")
        self._th.start()
        logger.log("[GpuProbe] 显卡采样已启动（0.5s）", "DEBUG")

    def stop(self) -> None:
        th = self._th
        if th is None:
            return
        self._th = None
        self._stop.set()
        th.join(timeout=self._interval_s + _CALL_TIMEOUT_S + 1.0)
        self._sink.flush(1.0)
        logger.log("[GpuProbe] 显卡采样已停止", "DEBUG")

    @staticmethod
    def _supported() -> bool:
        if shutil.which("nvidia-smi") is None:
            logger.log("[GpuProbe] 无 nvidia-smi，采样放弃", "DEBUG")
            return False
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            row = self._sample()
            row["call_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            self._sink.append(row)
            self._stop.wait(self._interval_s)

    @staticmethod
    def _sample() -> dict:
        try:
            r = subprocess.run(
                ["nvidia-smi", f"--query-gpu={_QUERY}",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=_CALL_TIMEOUT_S)
            parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
            # util, mem_used, mem_total, temp, clock_sm, power
            while len(parts) < 6:
                parts.append("")
            err = None if r.returncode == 0 else f"exit={r.returncode}"
            return {"util": _num(parts[0]), "vram_mib": _num(parts[1]),
                    "vram_total_mib": _num(parts[2]), "temp": _num(parts[3]),
                    "clock_sm": _num(parts[4]), "power_w": _num(parts[5]),
                    "err": err}
        except Exception as exc:                       # 超时/进程不存在等
            return {"util": None, "vram_mib": None, "vram_total_mib": None,
                    "temp": None, "clock_sm": None, "power_w": None,
                    "err": repr(exc)}
