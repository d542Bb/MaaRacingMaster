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

import faulthandler
import shutil
import subprocess
import threading
import time
from pathlib import Path

from maaracing_master.core import dml_lock
from maaracing_master.core.logger import logger
from maaracing_master.core.trace_sink import get_trace_sink

_QUERY = ("utilization.gpu,memory.used,memory.total,temperature.gpu,"
          "clocks.sm,power.draw")
_CALL_TIMEOUT_S = 5.0
# 在飞 run 超过该时长即视为停顿现场，抓拍一次（生产实测停顿 0.8~1.2s，
# 正常 run 8ms——0.2s 阈值不会误触）
STALL_MARK_S = 0.2


def _cmd_output(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_CALL_TIMEOUT_S)
        return r.stdout or f"(无输出 exit={r.returncode})"
    except Exception as exc:                       # 驱动挂死时的 5s 兜底
        return f"(调用失败: {exc!r})"


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
        self._dumped: float | None = None     # 已抓拍的在飞 run 起点（去重）
        self._trace_dir: Path | None = None

    def start(self) -> None:
        if self._th is not None or not self._supported():
            return
        if logger.session_dir is None:
            return          # 无日志会话（单元测试/旁路调用）不采样，零开销
        self._trace_dir = Path(logger.session_dir) / "trace"
        self._trace_dir.mkdir(parents=True, exist_ok=True)
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
        # 内环 50ms 盯在飞 run（抓案发中现场）；外环 interval 采常规状态
        next_gpu = 0.0
        while not self._stop.is_set():
            now = time.perf_counter()
            self._watch(now)
            if now >= next_gpu:
                t0 = time.monotonic()
                row = self._sample()
                row["call_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                self._sink.append(row)
                next_gpu = now + self._interval_s
            self._stop.wait(0.05)

    def _watch(self, now: float) -> None:
        """在飞 run 超 200ms 即抓现场（每火一次，去重键 = run 起点）。"""
        since, who = dml_lock.RUN_WATCH.snapshot()
        if since is None or now - since < STALL_MARK_S or self._dumped == since:
            return
        self._dumped = since
        stamp = time.strftime("%H%M%S")
        elapsed_ms = round((now - since) * 1000.0, 1)
        self._sink.append({"stall_dump": stamp, "run_who": who,
                           "run_ms_so_far": elapsed_ms})
        if self._trace_dir is None:
            return
        path = self._trace_dir / f"stall_{stamp}_{who}.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"停顿现场 {stamp} who={who} 在飞已 {elapsed_ms}ms\n")
                # ① 节流原因位：直接回答「哪种节流」（功耗/温度/调度/空闲降频）
                f.write("\n===== nvidia-smi -q -d PERFORMANCE =====\n")
                f.write(_cmd_output(["nvidia-smi", "-q", "-d", "PERFORMANCE"]))
                # ② 每进程 GPU 引擎占用：回答「是不是别的进程占着独显」
                f.write("\n===== nvidia-smi pmon -c 1 =====\n")
                f.write(_cmd_output(["nvidia-smi", "pmon", "-c", "1"]))
                # ③ 全线程栈：回答「我们自己的线程都停在哪」
                f.write("\n===== faulthandler 全线程栈 =====\n")
                faulthandler.dump_traceback(f, all_threads=True)
        except Exception as exc:                   # 落盘失败不影响采样主路
            logger.log(f"[GpuProbe] 停顿抓拍失败: {exc!r}", "WARNING")

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
