# -*- coding: utf-8 -*-
"""进程 CPU 时间原语（Win32 GetProcessTimes，零依赖）。

为什么自己写而不引 psutil：psutil 不在本项目依赖里，且「进程累计 CPU 时间」在
Windows 上就是 GetProcessTimes 一次调用；core/ 下的 wgcap.py 同类 Win32 调用
也一律走 ctypes，口径一致。

分层：本模块只给**无状态原语**（累计秒数），不做占用率差分——占用率需要「两次
采样 + 墙钟差」，那是消费者的节奏问题。目前唯一使用者是鉴宝性能仪表
（plugins/treasure/module.py 的 read_perf_snapshot）；等第二个模块真要做负载显示
时再抽差分，避免把首个使用者的私货固化进通用层。

非 Windows 平台返回 None（CI 只装 pytest 跑纯逻辑单测，导入本模块不得炸）。
"""
from __future__ import annotations

import sys

__all__ = ["process_cpu_seconds", "logical_core_count", "IS_WINDOWS"]

IS_WINDOWS = sys.platform == "win32"

# FILETIME 是 1601-01-01 起的计数，**每个 tick = 100 纳秒 = 1e-7 秒**。
# （写成 100e-7 会把秒数放大 100 倍——与 time.process_time() 对拍即可一眼看穿。）
_FILETIME_UNIT_S = 100e-9

_kernel32 = None
_GetProcessTimes = None
_CurrentProcessError = None


def _bind() -> bool:
    """懒绑定 Win32 入口；失败一次即永久判定为不可用（不每帧重试）。"""
    global _kernel32, _GetProcessTimes, _CurrentProcessError
    if _GetProcessTimes is not None or _CurrentProcessError is not None:
        return _GetProcessTimes is not None
    if not IS_WINDOWS:
        _CurrentProcessError = "非 Windows 平台"
        return False
    import ctypes

    try:
        lib = ctypes.windll.kernel32  # type: ignore[attr-defined]
        fn = lib.GetProcessTimes
        fn.restype = ctypes.c_bool
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        _kernel32, _GetProcessTimes = lib, fn
    except Exception as exc:  # noqa: BLE001 —— 平台事实取不到就是不可用
        _CurrentProcessError = str(exc)
        return False
    return True


def process_cpu_seconds() -> float | None:
    """本进程已消耗的 CPU 总秒数（内核态 + 用户态）；不可用时返回 None。

    用 GetCurrentProcess() 的伪句柄，无需也不得 CloseHandle。
    FILETIME 就是 8 字节 100ns 计数，小端下直接按 c_uint64 读即可，无需结构体
    （且 argtypes 声明为 POINTER(c_uint64)，塞 byref(Structure) 会被 ctypes 拒收）。
    """
    if not _bind():
        return None
    import ctypes

    creation = ctypes.c_uint64()
    exit_ = ctypes.c_uint64()
    kernel = ctypes.c_uint64()
    user = ctypes.c_uint64()
    kernel32 = _kernel32
    if kernel32 is None:  # _bind() 成功即已初始化；此分支仅为类型收窄
        return None
    handle = kernel32.GetCurrentProcess()  # 伪句柄，恒为 -1
    get_times = _GetProcessTimes
    if get_times is None:  # 同 _kernel32，_bind() 成功即已绑定；仅为类型收窄
        return None
    ok = get_times(ctypes.c_void_p(handle),
                   ctypes.byref(creation), ctypes.byref(exit_),
                   ctypes.byref(kernel), ctypes.byref(user))
    if not ok:
        return None
    return (kernel.value + user.value) * _FILETIME_UNIT_S


def logical_core_count() -> int:
    """可用于负载归一的逻辑处理器个数（含 E-core）。

    进程 CPU 占用是多核累计量、可 >100%，没有分母就无从判断"偏高"。
    ⚠️ 若将来真的做了亲和性绑定，可用核数应改取亲和掩码的 popcount 而不是这个值
       ——分母大于实际可调度范围会把负载读小。当前绑核未生效，两者一致。
    """
    import os

    return max(1, os.cpu_count() or 1)
