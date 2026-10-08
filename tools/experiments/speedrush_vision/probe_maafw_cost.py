"""复现 MaaFW Win32Controller(FramePool) 截图链路的线程开销。

sidecar 对局中进程吃 ~4.3 核，其中 3 个纯原生线程各 ~0.88 核（py-spy 看不见）。
已排除：项目自建 WGC 截图、onnxruntime 单/多会话、深度后处理。
剩下最后一个大件：MaaFramework 的 Win32Controller(FramePool) 原生截图链路。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tools/experiments/speedrush_vision"))

from maa.controller import Win32Controller  # noqa: E402
from maa.define import MaaWin32ScreencapMethodEnum  # noqa: E402
from maaracing_master.core.window_utils import ensure_dpi_aware, find_game_hwnd  # noqa: E402
from probe_thread_cpu import snapshot_threads  # noqa: E402


def mine():
    return snapshot_threads().get(os.getpid(), {})


def measure(label, dur, drive=None, hz=0.0):
    stop = threading.Event()
    cnt = [0]
    if drive is not None:
        def loop():
            while not stop.is_set():
                try:
                    drive()
                    cnt[0] += 1
                except Exception as e:
                    print("   drive 异常:", type(e).__name__, e)
                    break
                if hz > 0:
                    time.sleep(max(0.0, 1.0 / hz - 0.003))
        threading.Thread(target=loop, daemon=True).start()
    time.sleep(0.5)
    a = mine(); ta = time.monotonic()
    time.sleep(dur)
    b = mine(); tb = time.monotonic()
    stop.set()
    dt = tb - ta
    rows = []
    for tid, (u, k) in b.items():
        if tid in a:
            c = ((u + k) - (a[tid][0] + a[tid][1])) / dt
            if c > 0.01:
                rows.append((tid, c))
    rows.sort(key=lambda x: -x[1])
    tot = sum(c for _, c in rows)
    print(f"\n### {label}  进程合计 {tot:.2f} 核  线程数 {len(b)}  驱动 {cnt[0]} 次")
    for tid, c in rows[:12]:
        print(f"    tid={tid:7} {c:6.3f} 核")
    return tot


def main():
    ensure_dpi_aware()
    hwnd = find_game_hwnd()
    print("游戏窗口 hwnd =", hwnd)
    if not hwnd:
        return 1
    measure("0. 连接前基线", 4)
    c = Win32Controller(hWnd=hwnd, screencap_method=MaaWin32ScreencapMethodEnum.FramePool)
    ok = c.post_connection().wait()
    print("连接:", ok)
    time.sleep(1.0)
    measure("1. 已连接、不截图", 10)
    measure("2. 60Hz 反复截图", 15, drive=lambda: c.post_screencap().wait(), hz=60)
    measure("3. 停止截图后", 6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
