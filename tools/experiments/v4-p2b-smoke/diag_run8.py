# -*- coding: utf-8 -*-
"""P2b 诊断 v8（帧存证 + 双序对照）：分辨「画面不对」还是「帧序不对」。

diag_run7：真实 WGC 帧 score=0.000（全零，非低分）。两种解释：
  (a) 当前画面不是大厅（实验环境因素，非 bug）；
  (b) WGC 帧与 jpg 帧系统性差异（BGRA 内存序假设不成立 → R/B 反）。
对策：抓一帧存 png 供肉眼验证画面内容；同一帧做「名义 RGB / 通道反转」两种序
的整帧多尺度匹配对照——哪种序命中，就说明生产链该按哪种序处理。
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

from maaracing_assistant.core.template_match import find_any_cs
from maaracing_assistant.core.wgcap import WgcCapture

user32 = ctypes.WinDLL("user32", use_last_error=True)


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    if h:
        return int(h)
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


def probe(frame_rgb: np.ndarray, image_dirs: list[Path], tag: str) -> None:
    """整帧多尺度、低阈匹配（不限 roi），打印名义 RGB 与反转两种序的分数。"""
    names = ["hall_peak_appraise_card", "act_goto_appraise_btn", "hall_session_cards"]
    for order, f in (("名义RGB", frame_rgb), ("R/B反转", frame_rgb[:, :, ::-1].copy())):
        f = np.ascontiguousarray(f)
        best = []
        for n in names:
            box, score, _ = find_any_cs(f, [n], image_dirs, colorspace="rgb",
                                        threshold=0.0, scales=(0.9, 1.0, 1.1))
            best.append(f"{n}={score:.3f}{'@' + str(box) if box else ''}")
        print(f"[probe:{tag}] {order}: " + " | ".join(best))


def main() -> None:
    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到「巅峰极速」窗口")
        return
    print(f"[hwnd] {hwnd}")
    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.2)
    rgb, fid, _ts, age = cap.get_latest_rgb()
    cap.stop()
    if rgb is None:
        print("[abort] WGC 无帧")
        return
    print(f"[frame] fid={fid} age={age:.1f}ms shape={rgb.shape} "
          f"通道均值 R={rgb[:, :, 0].mean():.1f} G={rgb[:, :, 1].mean():.1f} B={rgb[:, :, 2].mean():.1f}")
    out = Path("tools/navkit/out/diag_run8_frame.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))  # 按名义 RGB 存，肉眼验色
    print(f"[save] {out}")

    image_dirs = [
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ]
    probe(rgb, image_dirs, "真实WGC帧")

    # 对照：raw jpg 帧（diag_run6 命中 0.988 的同源素材）应双序至少一序命中
    jpg = Path.home() / "AppData/Roaming/MaaRacingAssistant/debug/treasure/20260906_212501/raw/0001_raw.jpg"
    if jpg.is_file():
        bgr = cv2.imread(str(jpg), cv2.IMREAD_COLOR)
        probe(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), image_dirs, "raw-jpg对照")


if __name__ == "__main__":
    main()
