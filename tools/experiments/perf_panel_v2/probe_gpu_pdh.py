"""临时探针：用 Windows PDH 计数器读「按进程的 GPU 占用」（用完即删，不入仓库）。"""
import ctypes
import ctypes.wintypes as wt
import re
import time
from collections import defaultdict

pdh = ctypes.WinDLL("pdh")

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


def read_once():
    q = wt.HANDLE()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
        raise OSError("open query failed")
    c = wt.HANDLE()
    rc = pdh.PdhAddEnglishCounterW(
        q, r"\GPU Engine(*)\Utilization Percentage", 0, ctypes.byref(c))
    if rc != 0:
        raise OSError(f"add counter failed rc={rc}")
    pdh.PdhCollectQueryData(q)
    time.sleep(1.0)
    pdh.PdhCollectQueryData(q)

    size = wt.DWORD(0)
    count = wt.DWORD(0)
    rc = pdh.PdhGetFormattedCounterArrayW(
        c, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
    if (rc & 0xFFFFFFFF) != PDH_MORE_DATA:
        raise OSError(f"size query rc={rc}")
    buf = ctypes.create_string_buffer(size.value)
    arr = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
    rc = pdh.PdhGetFormattedCounterArrayW(
        c, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), arr)
    if rc != 0:
        raise OSError(f"read rc={rc}")

    per_pid = defaultdict(float)
    per_engine = defaultdict(float)
    for i in range(count.value):
        item = arr[i]
        m = re.match(r"pid_(\d+)_luid_(0x[0-9A-Fa-f]+)_(0x[0-9A-Fa-f]+)_phys_(\d+)_eng_(\d+)_engtype_(\w+)",
                     item.szName or "")
        if not m:
            continue
        pid = int(m.group(1))
        engine = m.group(6)
        val = item.FmtValue.doubleValue
        if item.FmtValue.CStatus != 0 or val <= 0:
            continue
        per_pid[pid] += val
        per_engine[engine] += val
    pdh.PdhCloseQuery(q)
    return per_pid, per_engine, count.value


if __name__ == "__main__":
    per_pid, per_engine, n = read_once()
    print("instances:", n)
    print("engines:", {k: round(v, 1) for k, v in sorted(per_engine.items(), key=lambda x: -x[1])})
    top = sorted(per_pid.items(), key=lambda x: -x[1])[:8]
    import os
    me = os.getpid()
    print("me pid:", me, "->", round(per_pid.get(me, 0.0), 1))
    print("top pids:")
    for pid, v in top:
        print("  pid", pid, round(min(100.0, v), 1))
