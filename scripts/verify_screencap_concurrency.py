#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 MAA Win32Controller(FramePool) post_screencap 并发安全。

背景（2026-09-03）：treasure 手柄导航 approach 是主循环同步阻塞调用，阻塞期间
主循环不截图/不检测/不存图 → 匹配中等转移信号窗口被吃掉。拟改为导航线程化：
主循环（截图/检测）与导航线程（截图/推杆）并发。前提是 MAA post_screencap
能从两个线程并发调用而不崩溃/不撕裂/不串帧。

本脚本：
  1. 找到游戏窗口（标题关键词与生产 find_game_hwnd 一致）
  2. 创建 Win32Controller(FramePool) 并连接（与生产 controller.connect 同路径）
  3. 预热 1 次后，开 2 个线程各连续截图 N 次，与单线程基线对比
  4. 校验：每帧非 None / shape 一致 / 非全零；统计耗时与异常

用法：.venv\\Scripts\\python.exe scripts\\verify_screencap_concurrency.py [n_each]
依赖游戏窗口在线（否则报窗口未找到）。
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maa.controller import Win32Controller  # noqa: E402
from maa.define import MaaWin32ScreencapMethodEnum  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402


def _find_game_hwnd() -> int:
    # user_path 隔离：maa 需要可写目录放 cache/log，用系统临时目录即可
    import tempfile

    try:
        Toolkit.init_option(tempfile.mkdtemp(prefix="maarm_verify_"))
    except Exception:
        pass
    windows = Toolkit.find_desktop_windows()
    keywords = ["巅峰极速", "g112", "Racing Master"]
    for win in windows:
        for kw in keywords:
            if kw in win.window_name:
                return int(win.hwnd)
    print("未找到游戏窗口，可用窗口前10个:")
    for win in windows[:10]:
        print(f"  hWnd={win.hwnd}, class={win.class_name}, title={win.window_name}")
    return 0


def _shot(ctrl) -> object:
    """与生产 _screencap 相同的取帧路径（post → wait → cached_image）。"""
    job = ctrl.post_screencap()
    if not job.wait():
        return None
    return ctrl.cached_image


def _check_frame(img, tag: str) -> tuple:
    if img is None:
        return (False, f"{tag}: None")
    arr = getattr(img, "numpy", None)
    import numpy as np

    if arr is not None:
        a = np.asarray(arr())
    elif hasattr(img, "__array__"):
        a = np.asarray(img)
    else:
        return (False, f"{tag}: 未知图像类型 {type(img).__name__}")
    if a.size == 0 or a.ndim < 3:
        return (False, f"{tag}: 尺寸异常 {a.shape}")
    if int(a.sum()) == 0:
        return (False, f"{tag}: 全零帧")
    return (True, f"{tag}: ok shape={a.shape}")


def _serial_pass(ctrl, n: int, label: str) -> dict:
    """单线程连续截图 n 次（基线）。"""
    t0 = time.perf_counter()
    ok = 0
    for i in range(n):
        img = _shot(ctrl)
        good, msg = _check_frame(img, f"{label}[{i}]")
        if good:
            ok += 1
        else:
            print(f"  {msg}")
    return {"ok": ok, "n": n, "secs": time.perf_counter() - t0}


def _worker(ctrl, n: int, results: list, idx: int):
    t0 = time.perf_counter()
    ok = 0
    for i in range(n):
        img = _shot(ctrl)
        good, msg = _check_frame(img, f"thread{idx}[{i}]")
        if good:
            ok += 1
        else:
            print(f"  {msg}")
    results.append({"ok": ok, "n": n, "secs": time.perf_counter() - t0, "idx": idx})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("n_each", nargs="?", type=int, default=50)
    args = parser.parse_args()

    hwnd = _find_game_hwnd()
    if not hwnd:
        return 1
    print(f"窗口 hWnd={hwnd}，创建 Win32Controller(FramePool)...")

    ctrl = Win32Controller(hWnd=hwnd, screencap_method=MaaWin32ScreencapMethodEnum.FramePool)
    conn = ctrl.post_connection()
    if not conn.wait():
        print("连接失败")
        return 1
    print("连接成功")

    # 预热（首次截图初始化 FramePool/缓存）：FramePool 是后台帧池，首帧可能尚未
    # 就绪 → 多次预热直到拿到非全零帧（上限 10 次），验证"首帧时序"而非误报并发问题
    warm = None
    for _ in range(10):
        warm = _shot(ctrl)
        good, msg = _check_frame(warm, "warmup")
        if good:
            break
        time.sleep(0.3)
    print(f"预热: {msg}")
    if not good:
        return 1

    # 1) 单线程基线
    base = _serial_pass(ctrl, args.n_each, "serial")
    print(f"单线程 {base['n']} 次: 成功 {base['ok']}/{base['n']}, 耗时 {base['secs']:.2f}s")

    # 2) 双线程并发
    results: list = []
    threads = [
        threading.Thread(target=_worker, args=(ctrl, args.n_each, results, i))
        for i in range(2)
    ]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    total_ok = sum(r["ok"] for r in results)
    total_n = sum(r["n"] for r in results)
    per_thread = ", ".join(
        f"th{r['idx']} {r['ok']}/{r['n']} ({r['secs']:.2f}s)" for r in results
    )
    print(f"双线程 {total_n} 次: 成功 {total_ok}/{total_n} | {per_thread} | 墙钟 {wall:.2f}s")
    if total_ok == total_n:
        print("结论: 并发截图全部成功，无异常/无撕裂/无全零帧 → post_screencap 可并发调用")
        return 0
    print("结论: 存在失败帧 → 并发不安全，需要串行化或独立控制器")
    return 2


if __name__ == "__main__":
    sys.exit(main())
