# -*- coding: utf-8 -*-
"""P2b 诊断 v7（全真实验）：真实窗口 + 真实 WGC 采集器 → v4 帧链 → 真模板匹配。

探针（diag_run6）已证明代码路径全绿（通道序保真、真帧命中 0.988），真机不
命中只剩运行时差异嫌疑：①exe 里 frame_with_age 永远 (None,0,0,inf)（WGC 不
同源）→ screencap 每轮 stale；②打包资源路径差异。

本实验把唯一没被离线覆盖的环节跑成真的：找游戏窗口 → WgcCapture 实采 →
frame_with_age → WgcapController.screencap（含 stale 守卫）→ 真源入口节点
rect/模板匹配。若命中 → 根因锁定 exe 差异；若不命中 → 当场按 age/score 定位。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

import ctypes
from ctypes import wintypes

from maaracing_assistant.core.nav_graph import STALE_FRAME_MS, WgcapController
from maaracing_assistant.core.template_match import find_any_cs
from maaracing_assistant.core.wgcap import WgcCapture

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]


def find_hwnd() -> int:
    for title in ("巅峰极速",):
        h = user32.FindWindowW(None, title)
        if h:
            return int(h)
    # 兜底：枚举模糊匹配
    EnumWindows = user32.EnumWindows
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    IsWindowVisible = user32.IsWindowVisible
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        n = GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(hwnd, buf, n + 1)
        if "巅峰" in buf.value or "极速" in buf.value:
            found.append(int(hwnd))
        return True

    EnumWindows(_cb, 0)
    return found[0] if found else 0


def main() -> None:
    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到「巅峰极速」窗口——请开游戏后重跑本实验")
        return
    print(f"[hwnd] {hwnd}")

    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.5)  # 等首帧

    class _Adapter:
        """CaptureAdapter.frame_with_age 的最小复刻（读同一 WGC）。"""

        def frame_with_age(self):
            if cap.is_running:
                try:
                    return cap.get_latest_rgb()
                except Exception:  # noqa: BLE001
                    pass
            import math
            return (None, 0, 0, math.inf)

    ctrl = WgcapController(_Adapter())
    image_dirs = [
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ]
    rect = (0.7603703703703705, 0.8037448559670782, 0.8962962962962963, 0.8911111111111112)
    x1, y1, x2, y2 = rect

    ok = 0
    for i in range(10):
        f, fid, _ts, age = _Adapter().frame_with_age()
        line = f"[t{i}] adapter age={age:.1f}ms fid={fid}"
        if f is None:
            print(line + " 帧缺失！")
            time.sleep(0.5)
            continue
        H, W = f.shape[:2]
        roi = (int(x1 * W), int(y1 * H), int((x2 - x1) * W), int((y2 - y1) * H))
        try:
            bgr = ctrl.screencap()  # 走生产 stale 守卫 + RGB→BGR
        except Exception as e:  # noqa: BLE001
            print(line + f" screencap 抛: {e!r}")
            time.sleep(0.5)
            continue
        rgb = bgr[:, :, ::-1]  # 还原 analyze 端的 BGR2RGB（等价）
        box, score, name = find_any_cs(
            np.ascontiguousarray(rgb), ["hall_peak_appraise_card"], image_dirs,
            colorspace="rgb", threshold=0.7, roi=roi)
        print(line + f" W={W}x{H} screencap OK(fid={ctrl.last_frame_id}) "
              f"-> box={box} score={score:.3f} tpl={name}")
        if box:
            ok += 1
        time.sleep(0.5)

    cap.stop()
    print(f"[verdict] 真实 WGC 帧链命中 {ok}/10 | STALE_MS={STALE_FRAME_MS}")


if __name__ == "__main__":
    main()
