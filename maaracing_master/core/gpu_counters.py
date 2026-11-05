# -*- coding: utf-8 -*-
"""Windows GPU 引擎计数器（PDH）：按进程 × 引擎的 GPU 占用。

与厂商无关——计数器由系统提供，NVIDIA / AMD / Intel 核显都有实例，实例名形如
``pid_1234_luid_0x00000000_0x00011E0F_phys_0_eng_0_engtype_3D``，从中解析出进程号、
显卡（LUID）与引擎类型。任务管理器里每个进程的显卡占用读的就是这套数据。

两个口径：

- **本进程占用** = 该 pid 各引擎之和（截到 100）
- **整卡占用** = 每个引擎把所有进程求和后取最大（引擎并行，不能跨引擎相加）

为什么不用现成的库：能 pip 装的 GPU 小库（GPUtil / gpuinfo / pynvml）全是
``nvidia-smi`` 的包装，只覆盖 NVIDIA 且拿不到按进程的算力占用；AMD 的 ``pyadl``
停在 2017 年且要随包带驱动 DLL。系统自带的 PDH 接口零依赖（ctypes 直调），
跨厂商，且给得出按进程的数据。

实测（RTX 3060 单卡，闲置桌面）：611 个实例、3D 引擎 18.9%，按 pid 可直接排序。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
from collections import defaultdict

COUNTER_PATH = r"\GPU Engine(*)\Utilization Percentage"

# pid_10576_luid_0x00000000_0x00011E0F_phys_0_eng_0_engtype_3D
_INSTANCE_RE = re.compile(
    r"^pid_(\d+)_luid_(0x[0-9A-Fa-f]+)_(0x[0-9A-Fa-f]+)"
    r"_phys_(\d+)_eng_(\d+)_engtype_(\w+)$")

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class _FmtCounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class _FmtCounterValueItem(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", _FmtCounterValue)]


def parse_instance(name: str) -> dict | None:
    """把实例名拆成 {pid, luid, engtype}；不认识的形态返回 None（不猜）。"""
    m = _INSTANCE_RE.match(name or "")
    if not m:
        return None
    return {
        "pid": int(m.group(1)),
        "luid": m.group(3),
        "phys": int(m.group(4)),
        "engtype": m.group(6),
    }


def aggregate(items, pid: int) -> tuple[float, float]:
    """items 是 (解析结果, 百分比) 的序列，返回 (整卡占用, 本进程占用)，均截到 100。"""
    per_pid: dict[int, float] = defaultdict(float)
    per_engine: dict[str, float] = defaultdict(float)
    for info, value in items:
        if not info or not isinstance(value, (int, float)) or value <= 0:
            continue
        per_pid[info["pid"]] += value
        per_engine[info["engtype"]] += value
    util = max(per_engine.values()) if per_engine else 0.0
    return (min(100.0, util), min(100.0, per_pid.get(pid, 0.0)))


class GpuCounters:
    """常驻的 PDH 查询句柄：每次 ``read()`` 采一拍，值反映距上一拍这段时间的占用。"""

    def __init__(self):
        self._pdh = ctypes.WinDLL("pdh")
        self._query = wt.HANDLE()
        self._counter = wt.HANDLE()
        self._open()

    def _open(self) -> None:
        add = getattr(self._pdh, "PdhAddEnglishCounterW", None)
        if add is None:                      # 老系统只有本地化计数器名
            raise OSError("PdhAddEnglishCounterW 不可用")
        rc = self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if rc != 0:
            raise OSError(f"PdhOpenQueryW rc={rc}")
        rc = add(self._query, COUNTER_PATH, 0, ctypes.byref(self._counter))
        if rc != 0:
            self.close()
            raise OSError(f"PdhAddEnglishCounterW rc={rc}")

    def read(self) -> dict | None:
        """采一拍。失败返回 None（调用方保留上一次的值或退回驱动查询）。"""
        rc = self._pdh.PdhCollectQueryData(self._query) & 0xFFFFFFFF
        if rc != 0:
            return None

        size = wt.DWORD(0)
        count = wt.DWORD(0)
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE,
            ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF
        if rc != PDH_MORE_DATA:
            return None
        buf = ctypes.create_string_buffer(size.value)
        arr = ctypes.cast(buf, ctypes.POINTER(_FmtCounterValueItem))
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE,
            ctypes.byref(size), ctypes.byref(count), arr) & 0xFFFFFFFF
        if rc != 0:
            return None

        items = []
        for i in range(count.value):
            item = arr[i]
            if item.FmtValue.CStatus != 0:
                continue
            items.append((parse_instance(item.szName), item.FmtValue.doubleValue))

        util, prog = aggregate(items, os.getpid())
        return {"util_pct": round(util, 1), "prog_pct": round(prog, 1),
                "instances": count.value}

    def close(self) -> None:
        try:
            if self._query and self._query.value:
                self._pdh.PdhCloseQuery(self._query)
        finally:
            self._query = wt.HANDLE()
            self._counter = wt.HANDLE()
