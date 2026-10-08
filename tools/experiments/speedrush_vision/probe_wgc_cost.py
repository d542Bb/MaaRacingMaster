"""受控复现：单独跑 WGC 截图链路，看是否产生「纯原生自旋线程」。

sidecar 实测：对局中 3 个纯原生线程各烧 ~0.88 核（py-spy 完全采不到）。
WGC（windows_capture）的回调在原生线程上，是头号嫌疑。本探针只起截图，
不跑任何推理，用 NtQuerySystemInformation 读自己每线程 CPU。
"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tools/experiments/speedrush_vision"))

from maaracing_master.core.window_utils import ensure_dpi_aware, find_game_hwnd  # noqa: E402
from maaracing_master.core.wgcap import WgcCapture  # noqa: E402
from probe_thread_cpu import snapshot_threads  # noqa: E402


def mine() -> dict:
    return snapshot_threads().get(os.getpid(), {})


def measure(label: str, dur: float, pull=None, rate_hz: float = 0.0):
    stop = False
    if pull is not None:
        import threading

        def loop():
            while not stop:
                try:
                    pull()
                except Exception:
                    pass
                if rate_hz > 0:
                    time.sleep(max(0.0, 1.0 / rate_hz - 0.0005))

        threading.Thread(target=loop, daemon=True).start()
    time.sleep(0.4)
    a = mine(); ta = time.monotonic()
    time.sleep(dur)
    b = mine(); tb = time.monotonic()
    dt = tb - ta
    rows = []
    tot = 0.0
    for tid, (u, k) in b.items():
        if tid in a:
            c = ((u + k) - (a[tid][0] + a[tid][1])) / dt
            if c > 0.01:
                rows.append((tid, c)); tot += c
    rows.sort(key=lambda x: -x[1])
    print(f"\n### {label}  进程合计 {tot:.2f} 核  线程数 {len(b)}")
    for tid, c in rows[:12]:
        print(f"    tid={tid:7} {c:6.3f} 核")
    return tot


def main():
    ensure_dpi_aware()
    hwnd = find_game_hwnd()
    print("游戏窗口 hwnd =", hwnd)
    if not hwnd:
        print("未找到游戏窗口，退出")
        return 1
    measure("0. 截图前基线", 4)
    cap = WgcCapture(hwnd, max_fps=60)
    cap.start()
    time.sleep(1.0)
    measure("1. WGC 采集中（无消费者）", 12)
    n = [0]

    def pull():
        f = cap.get_latest_rgb()
        if f is not None:
            n[0] += 1

    measure("2. WGC 采集 + 消费者拉取", 12, pull=pull, rate_hz=20)
    print("   消费者拉到帧:", n[0])
    cap.stop()
    time.sleep(0.5)
    measure("3. 停止后", 5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
