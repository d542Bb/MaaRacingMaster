#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显卡状态探针——驾驶阶段逐 0.5s 采样 nvidia-smi（独立流水汇 "gpu"），
DML 调用卡住时抓拍停顿现场。

两条取证面：
- 常规采样：利用率 / 显存 / 温度 / SM 频率 / 功耗，外加 ``call_ms``——单次
  外部调用时长本身是信号，驱动停摆时 nvidia-smi 会被一起卡住；
- 现场抓拍：在飞 DML 调用超 ``STALL_MARK_S`` 即落 ``trace/stall_*.txt``，
  内容为我方全线程栈 + 节流原因位 + 每进程 GPU 引擎占用。

线程划分由「外部调用会被停摆卡住」决定：盯梢（纯进程内，50ms）、常规采样
（0.5s）、抓拍各走一条线程，任一条被驱动卡住都不拖累其余取证。无日志会话
不采样（单元测试与旁路调用零开销）；无 nvidia-smi 直接放弃。
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
# 现场的外部取证：节流原因位答「哪种节流」，每进程占用答「是否他进程占卡」
_SCENE_QUERIES = (
    ("节流原因位（nvidia-smi -q -d PERFORMANCE）",
     ["nvidia-smi", "-q", "-d", "PERFORMANCE"]),
    ("每进程 GPU 引擎占用（nvidia-smi pmon -c 1）",
     ["nvidia-smi", "pmon", "-c", "1"]),
)


def _inflight_desc() -> str:
    since, who = dml_lock.RUN_WATCH.snapshot()
    if since is None:
        return "无（run 已结束）"
    return f"{who} 已 {(time.perf_counter() - since) * 1000.0:.1f}ms"


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
    """显卡探针：start/stop 成对；行进 "gpu" 流水汇。

    采样行字段：util（%）、vram_mib / vram_total_mib、temp、clock_sm（MHz）、
    power_w、call_ms（本次外部调用时长，停摆哨）、err（调用失败原因）。
    抓派发一行 {stall_dump（现场文件名主干）, run_who, run_ms_so_far} 作时间线
    锚点。协议头时间戳由流水汇补齐，与 speedrush 流水同钟可离线对齐。
    """

    def __init__(self, interval_s: float = 0.5):
        self._interval_s = interval_s
        self._sink = get_trace_sink("gpu")
        self._stop = threading.Event()
        self._th: threading.Thread | None = None           # 盯梢
        self._sample_th: threading.Thread | None = None    # 常规采样
        self._dump_th: threading.Thread | None = None      # 现场抓拍
        self._dumped: float | None = None     # 已抓拍的在飞起点（去重）
        self._trace_dir: Path | None = None

    def start(self) -> None:
        if self._th is not None or not self._supported():
            return
        if logger.session_dir is None:
            return          # 无日志会话（单元测试/旁路调用）不采样，零开销
        self._trace_dir = Path(logger.session_dir) / "trace"
        self._trace_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._th = threading.Thread(target=self._watch_loop, daemon=True,
                                    name="gpu-probe")
        self._sample_th = threading.Thread(target=self._sample_loop, daemon=True,
                                           name="gpu-sample")
        self._th.start()
        self._sample_th.start()
        logger.log("[GpuProbe] 显卡采样已启动（0.5s）", "DEBUG")

    def stop(self) -> None:
        th, sample_th = self._th, self._sample_th
        if th is None:
            return
        self._th = self._sample_th = None
        self._stop.set()
        th.join(timeout=1.0)
        if sample_th is not None:
            sample_th.join(timeout=_CALL_TIMEOUT_S + 1.0)
        # 抓拍线程不 join：它可能正卡在停摆期的 nvidia-smi 上（最长 2×5s），
        # 交给 daemon 写完；会话目录还在，文件不会被截走。
        self._sink.flush(1.0)
        logger.log("[GpuProbe] 显卡采样已停止", "DEBUG")

    @staticmethod
    def _supported() -> bool:
        if shutil.which("nvidia-smi") is None:
            logger.log("[GpuProbe] 无 nvidia-smi，采样放弃", "DEBUG")
            return False
        return True

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            self._watch(time.perf_counter())
            self._stop.wait(0.05)

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            row = self._sample()
            spent = time.monotonic() - t0
            row["call_ms"] = round(spent * 1000.0, 1)
            self._sink.append(row)
            self._stop.wait(max(0.0, self._interval_s - spent))

    def _watch(self, now: float) -> None:
        """在飞 DML 调用超阈即起一条抓拍线程（去重键 = 在飞起点）。"""
        since, who = dml_lock.RUN_WATCH.snapshot()
        if since is None or now - since < STALL_MARK_S or self._dumped == since:
            return
        if self._dump_th is not None and self._dump_th.is_alive():
            return          # 上一份现场还在落盘，下一轮再试，事件不丢
        self._dumped = since
        wall = time.time()      # 单一时源取秒+毫秒：同一秒内两次停顿不得互相覆盖
        stamp = (time.strftime("%H%M%S", time.localtime(wall))
                 + f"_{int(wall * 1000) % 1000:03d}")
        elapsed_ms = round((now - since) * 1000.0, 1)
        self._sink.append({"stall_dump": stamp, "run_who": who,
                           "run_ms_so_far": elapsed_ms})
        self._dump_th = threading.Thread(
            target=self._dump_scene, args=(stamp, who, elapsed_ms),
            daemon=True, name="gpu-stall-dump")
        self._dump_th.start()

    def _dump_scene(self, stamp: str, who: str, elapsed_ms: float) -> None:
        if self._trace_dir is None:
            return
        path = self._trace_dir / f"stall_{stamp}_{who}.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"停顿现场 {stamp} who={who} 检出时在飞已 {elapsed_ms}ms\n")
                # 我方线程栈先落：停摆期外部 nvidia-smi 自己会被卡住（生产实测
                # 844ms），排在它后面等于拍恢复后的空栈，白丢「我方卡点」这一问。
                f.write(f"栈抓拍时在飞: {_inflight_desc()}\n")
                f.write("\n===== 我方全线程栈（faulthandler）=====\n")
                f.flush()       # faulthandler 直写 fd，缓冲文本须先落地
                faulthandler.dump_traceback(f, all_threads=True)
                for title, cmd in _SCENE_QUERIES:
                    t0 = time.monotonic()
                    out = _cmd_output(cmd)
                    f.write(f"\n===== {title}（耗时 "
                            f"{(time.monotonic() - t0) * 1000.0:.1f}ms）=====\n")
                    f.write(out)
                # 外部取证可能跨过恢复点：记下结束时是否仍在飞，判读节流位用
                f.write(f"\n外部取证结束时在飞: {_inflight_desc()}\n")
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
