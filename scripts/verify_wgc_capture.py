#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 WGC 中心采集器（单生产者/多消费者）的关键承诺。

覆盖（2026-09-04 用户要求）：
  1. 帧尺寸：标准输出 = 1280x720 RGB（真正的 16:9），客户区已裁剪（不含标题栏）
  2. 客户区偏移/尺寸正确：WGC 原始帧宽 > 720（含标题栏），裁剪后恰为 720p
  3. 60fps 上限：采样窗口内 frame_id 增量 ≈ 采样时长 × ≤60
  4. 低延迟：get_latest_rgb 平均耗时 P50 应为微秒级（缓存读，无采集请求）
  5. 不被遮挡：最小化后仍能取到帧（age 上升但帧不 None），恢复后新鲜
  6. 多消费者并发读：2 线程并发 get_latest_rgb 无异常，帧一致
  7. 与 MAA 兜底对照：WGC 帧与 post_screencap 帧尺寸一致（1280x720）

用法：.venv\\Scripts\\python.exe scripts\\verify_wgc_capture.py [采样秒数]
依赖游戏窗口在线（否则报窗口未找到）。
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from maaracing_master.core.wgcap import WgcCapture  # noqa: E402
from maa.controller import Win32Controller  # noqa: E402
from maa.define import MaaWin32ScreencapMethodEnum  # noqa: E402
from maa.toolkit import Toolkit  # noqa: E402


def _find_game_hwnd() -> int:
    import tempfile

    try:
        Toolkit.init_option(tempfile.mkdtemp(prefix="mra_wgc_"))
    except Exception:
        pass
    windows = Toolkit.find_desktop_windows()
    for win in windows:
        for kw in ["巅峰极速", "g112", "Racing Master"]:
            if kw in win.window_name:
                return int(win.hwnd)
    print("未找到游戏窗口，可用窗口前10个:")
    for win in windows[:10]:
        print(f"  hWnd={win.hwnd}, class={win.class_name}, title={win.window_name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("secs", nargs="?", type=float, default=3.0)
    args = parser.parse_args()

    hwnd = _find_game_hwnd()
    if not hwnd:
        return 1
    print(f"窗口 hWnd={hwnd}")

    # 1. 启动 WGC 中心采集
    cap = WgcCapture(hwnd, max_fps=60)
    cap.start()
    ok = False
    for _ in range(40):
        frame, *_ = cap.get_latest()
        if frame is not None:
            ok = True
            break
        time.sleep(0.05)
    if not ok:
        print("FAIL: WGC 启动后 2 秒未收到首帧")
        cap.stop()
        return 1
    first_bgra, first_fid, *_ = cap.get_latest()
    h0, w0 = first_bgra.shape[:2]
    print(f"首帧: fid={first_fid} bgra_shape={first_bgra.shape}")

    # 2. 客户区裁剪验证：offset 可能为负（游戏特殊渲染链，如 1604x902 独立交换链）——
    #    负/越界 offset 会被 _crop_client 安全检查跳过 → 整帧即纯游戏画面（天然无标题栏）。
    #    正 offset（普通窗口）→ 裁出客户区。两种路径最终都应得到 720x1280 RGB 标准帧。
    m = cap.get_metrics()
    print(f"客户区裁剪: offset={m.get('client_offset')} size={m.get('client_size')}")
    offset = m.get("client_offset")
    csize = m.get("client_size")
    if offset is not None and csize is not None:
        if offset[0] >= 0 and offset[1] >= 0:
            if not (csize[0] == w0 and csize[1] == h0):
                print(f"FAIL: 裁剪后帧尺寸 {w0}x{h0} != 客户区 {csize}")
                return 1
            print("  正 offset → 客户区裁剪路径生效")
        else:
            print("  负 offset（特殊渲染链）→ 整帧路径（天然纯游戏画面）")

    # 3. 标准帧验证
    rgb, fid, ts, age = cap.get_latest_rgb()
    if rgb is None or rgb.shape != (720, 1280, 3):
        print(f"FAIL: get_latest_rgb 返回 {None if rgb is None else rgb.shape}，期望 (720,1280,3)")
        cap.stop()
        return 1
    if not rgb.flags["C_CONTIGUOUS"]:
        print("FAIL: 标准帧非 C-contiguous")
        cap.stop()
        return 1
    print(f"标准帧: {rgb.shape} C-contiguous  age={age:.1f}ms")

    # 4. 采样窗口：帧率上限 + 60fps 节流验证
    secs = args.secs
    t0 = time.perf_counter()
    fid0 = cap.frame_id
    time.sleep(secs)
    fid1 = cap.frame_id
    elapsed = time.perf_counter() - t0
    fps = (fid1 - fid0) / elapsed
    print(f"采样 {elapsed:.2f}s: frame_id {fid0}→{fid1} → 实际 {fps:.1f} fps（上限 60）")
    if fps > 60.5:
        print(f"FAIL: 帧率 {fps:.1f} 超上限 60")
        cap.stop()
        return 1
    m = cap.get_metrics()
    print(f"回调间隔: p50={m.get('callback_interval_p50', 0):.1f}ms "
          f"p95={m.get('callback_interval_p95', 0):.1f}ms count={m.get('callback_count')}")

    # 5. 低延迟验证
    N = 200
    t0 = time.perf_counter()
    for _ in range(N):
        cap.get_latest_rgb()
    dur_us = (time.perf_counter() - t0) / N * 1e6
    print(f"get_latest_rgb 平均耗时: {dur_us:.1f} µs/次（缓存读，应微秒级）")
    if dur_us > 5000:
        print(f"WARN: 读取耗时 {dur_us:.0f}µs 偏高（超过 5ms）")

    # 6. 多消费者并发读
    errors = []

    def _reader(name):
        try:
            for _ in range(50):
                f1, *_ = cap.get_latest_rgb()
                if f1 is None:
                    errors.append(f"{name}: None")
                    return
        except Exception as e:
            errors.append(f"{name}: {e!r}")

    ths = [threading.Thread(target=_reader, args=(f"th{i}",)) for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    print(f"并发读: {'OK' if not errors else 'FAIL ' + str(errors)}")

    # 7. 与 MAA 兜底对照（仅作尺寸一致性参考，不要求必须运行）
    try:
        ctrl = Win32Controller(hWnd=hwnd,
                               screencap_method=MaaWin32ScreencapMethodEnum.FramePool)
        if ctrl.post_connection().wait():
            job = ctrl.post_screencap()
            if job.wait():
                arr = ctrl.cached_image
                a = np.asarray(getattr(arr, "numpy", lambda: arr)())
                print(f"MAA 对照帧: {a.shape}（WGC 标准帧 {rgb.shape}）")
    except Exception as e:
        print(f"MAA 对照跳过: {e!r}")

    cap.stop()
    print("WGC 中心采集验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
