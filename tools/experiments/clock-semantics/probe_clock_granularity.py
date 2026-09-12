"""三种时钟的粒度与跳变语义实测（.venv 3.11 / Windows）。

结论（本机 2026-09-12 实测，见仓库记忆）：
  • time.monotonic()  —— 粒度约 15ms（CPython 3.11 在 Windows 走 GetTickCount64
    一族；改用 QueryPerformanceCounter 是 3.12 才有的变更），且不受校时影响。
  • time.perf_counter() —— 亚微秒粒度，同样不受校时影响。
  • time.time()        —— 粒度约 0.5~1.5ms，但会被 NTP 校时/手动改表跳变。

推论（时钟选型口径）：
  判定/节流窗口（阈值 ≥ 100ms）→ monotonic 足够；
  毫秒级耗时测量（duration/age 指标）→ 必须 perf_counter，用 monotonic 会被量化到 15ms；
  事件时刻（落盘、命名、展示）→ time.time() / datetime.now()。

用法：.venv\\Scripts\\python.exe -B tools\\experiments\\clock-semantics\\probe_clock_granularity.py
"""

import sys
import time


def steps(fn, max_samples=200, max_calls=400000):
    """连续调用 fn，返回相邻不同值之间的最小若干步进（秒）。"""
    prev = fn()
    out = []
    for _ in range(max_calls):
        cur = fn()
        if cur != prev:
            out.append(cur - prev)
            prev = cur
            if len(out) >= max_samples:
                break
    return out


def report(name, fn):
    s = steps(fn)
    ms = sorted(round(x * 1000, 4) for x in s)
    print(f"{name:>16}: 采样 {len(s)} 步，最小步进 {ms[0] if ms else 'n/a'} ms，"
          f"众数步进 {ms[len(ms) // 2] if ms else 'n/a'} ms")


def main():
    print(f"python {sys.version.split()[0]} / {sys.platform}")
    report("monotonic", time.monotonic)
    report("perf_counter", time.perf_counter)
    report("time.time", time.time)

    for target in (0.0005, 0.002, 0.01, 0.05):
        t0 = time.monotonic()
        p0 = time.perf_counter()
        w0 = time.time()
        time.sleep(target)
        print(f"sleep({target:>6}s) 实测: monotonic "
              f"{(time.monotonic() - t0) * 1000:8.3f}ms | perf_counter "
              f"{(time.perf_counter() - p0) * 1000:8.3f}ms | time.time "
              f"{(time.time() - w0) * 1000:8.3f}ms")

    print(f"\nmonotonic 当前值 {time.monotonic():.2f}s"
          "（起算点＝开机，故进程内初值 0.0 表示『开机那一刻』而非『很久以前』）")


if __name__ == "__main__":
    main()
