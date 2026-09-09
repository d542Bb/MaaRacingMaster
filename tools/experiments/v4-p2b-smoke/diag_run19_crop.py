# -*- coding: utf-8 -*-
"""P2b 诊断 v19（采集裁剪取证）：run18 存证帧顶部含标题栏、底部客户区被裁。

记录 WgcCapture 裁剪链每一步的真实数值：
- Win32 侧：GetWindowRect / DWM extended bounds / GetClientRect / ClientToScreen
- 帧侧：frame_buffer 原始 shape → _crop_client 输出 shape → 最终 rgb shape
- 存证：原始帧（裁剪前）与最终帧（裁剪后）各存一张 PNG，肉眼对照
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, ".")

from maaracing_assistant.core.wgcap import WgcCapture, _dwmapi, _DWMWA_EXTENDED_FRAME_BOUNDS

user32 = ctypes.WinDLL("user32", use_last_error=True)

ROWS: list = []


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    return int(h) if h else 0


def main() -> None:
    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到窗口")
        return
    wr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    dr = wintypes.RECT()
    _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
                                  ctypes.byref(dr), ctypes.sizeof(dr))
    cr = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    print(f"[hwnd] {hwnd}")
    print(f"[winrect]   {wr.left},{wr.top} -> {wr.right},{wr.bottom}  "
          f"size={wr.right - wr.left}x{wr.bottom - wr.top}")
    print(f"[dwm bounds] {dr.left},{dr.top} -> {dr.right},{dr.bottom}  "
          f"size={dr.right - dr.left}x{dr.bottom - dr.top}")
    print(f"[clientrect] size={cr.right - cr.left}x{cr.bottom - cr.top}")
    print(f"[client origin screen] {pt.x},{pt.y}  "
          f"offset_vs_dwm=({pt.x - dr.left}, {pt.y - dr.top})")

    cap = WgcCapture(hwnd)
    cap._compute_client_rect()
    print(f"[cap] client_offset={cap._client_offset} client_size={cap._client_size} "
          f"dwm_size={cap._dwm_size}")

    orig = WgcCapture._crop_client
    n = 0
    raw_saved = []

    def spy(self, img):
        nonlocal n
        if n == 0 and not raw_saved:
            import cv2
            from pathlib import Path
            out_dir = Path("tools/navkit/out")
            out_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out_dir / "run19_raw_bgr.png"),
                        cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR))
            raw_saved.append(img.shape)
        out = orig(self, img)
        if n < 3:
            print(f"[frame{n}] raw={img.shape} -> cropped={out.shape}")
        n += 1
        return out

    WgcCapture._crop_client = spy
    cap.start()
    time.sleep(1.5)
    rgb, fid, _ts, age = cap.get_latest_rgb()

    out_dir = Path("tools/navkit/out")
    out_dir.mkdir(parents=True, exist_ok=True)
    if rgb is not None:
        cv2.imwrite(str(out_dir / "run19_final_rgb.png"), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        print(f"[save] final {rgb.shape} fid={fid} age={age:.1f}ms -> run19_final_rgb.png")
    cap.stop()
    time.sleep(0.3)
    print(f"[verdict] 共 {n} 帧进入裁剪；raw={raw_saved}")


if __name__ == "__main__":
    main()
