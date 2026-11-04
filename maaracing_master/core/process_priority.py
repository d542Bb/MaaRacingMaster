#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程级资源优先级（GUI「性能优先」开关的执行体，core 进程级能力件）。

对**本进程**（sidecar python：模块感知/控制/OCR 所在进程）应用提级：

    CPU  psutil nice → HIGH_PRIORITY_CLASS（正式依赖，OCR 亲和性同源）——
         唯一标准权限下真实可提的通道
    IO   NtSetInformationProcess(ProcessIoPriority=3 high)——**尽力而为**：提到
         high 需要 SeIncreaseBasePriorityPrivilege 特权，标准权限下返回
         STATUS_PRIVILEGE_NOT_HELD（normal=2 已是可达上限），失败不影响 CPU
         通道；保留以兼容未来特权环境

**探明不可行的两路**（2026-10-01 实测，记录防复踩）：
    内存 SetProcessInformation(ProcessMemoryPriority)——MEMORY_PRIORITY 枚举
         上限即默认 NORMAL=5，设计上只能自愿降不能升，提级无空间
    GPU  没有公开旋钮（D3DKMTSetProcessPriority 未文档化且对 DirectML 无保证；
         NVIDIA per-app profile 只影响选卡不影响调度优先级）——显存争用的
         正解在推理侧缓冲固定（IO binding），不在此模块范围

失败语义：单通道失败不抛异常——返回生效清单供日志明示。幂等：重复应用无害；
revert 恢复 NORMAL / IO 2。非 Windows 全部无操作。
"""

from __future__ import annotations

import ctypes
import sys

_CHANNELS = ("cpu", "io")

_IO_HIGH, _IO_NORMAL = 3, 2
_ProcessIoPriority = 0x21


def _set_cpu(high: bool) -> bool:
    try:
        import psutil
        proc = psutil.Process()
        proc.nice(psutil.HIGH_PRIORITY_CLASS if high else psutil.NORMAL_PRIORITY_CLASS)
        return True
    except Exception:
        return False


def _set_io(value: int) -> bool:
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        # argtypes 须显式：伪句柄 (HANDLE)-1 经 restype 变为最大无符号值，
        # 默认 int 传参会溢出（argument 1: OverflowError）
        ntdll.NtSetInformationProcess.argtypes = (
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_size_t)
        v = ctypes.c_ulong(value)
        st = ntdll.NtSetInformationProcess(
            k32.GetCurrentProcess(), _ProcessIoPriority, ctypes.byref(v),
            ctypes.sizeof(v))
        return st == 0
    except Exception:
        return False


def apply_boost() -> dict[str, bool]:
    """应用性能优先级（先 CPU 后 IO）。返回各通道生效状态。"""
    if sys.platform != "win32":
        return {c: False for c in _CHANNELS}
    cpu = _set_cpu(high=True)
    return {"cpu": cpu, "io": _set_io(_IO_HIGH)}


def revert() -> dict[str, bool]:
    """恢复默认优先级。返回各通道恢复状态。"""
    if sys.platform != "win32":
        return {c: False for c in _CHANNELS}
    cpu = _set_cpu(high=False)
    return {"cpu": cpu, "io": _set_io(_IO_NORMAL)}


def describe(applied: dict[str, bool]) -> str:
    """生效清单 → 日志串；全 False（非 Windows/全失败）时给明示。"""
    names = {"cpu": "CPU", "io": "磁盘IO"}
    ok = [names[c] for c in _CHANNELS if applied.get(c)]
    return "、".join(ok) if ok else "无（全部未生效）"
