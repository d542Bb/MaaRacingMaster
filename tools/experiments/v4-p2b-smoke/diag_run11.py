# -*- coding: utf-8 -*-
"""P2b 诊断 v11：全部 nav 模板在当前真帧上的整帧健康扫描。

v3/v4 共用真源 rect（detector._crop 乘 W/H 裁剪，与 find_any_cs roi 同语义），
boot 未命中根因是资产 rect 过时——整帧扫描区分「模板失效」与「rect 过时」，
产出待修 rect 清单（命中位置 vs 现 rect）。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

from maaracing_assistant.core.template_match import find_any_cs
from maaracing_assistant.core.wgcap import WgcCapture

user32 = ctypes.WinDLL("user32", use_last_error=True)


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    if h:
        return int(h)
    found = []
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    IsWindowVisible = user32.IsWindowVisible

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _l):
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

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


def main() -> None:
    hwnd = find_hwnd()
    if not hwnd:
        print("[abort] 找不到窗口")
        return
    cap = WgcCapture(hwnd)
    cap.start()
    time.sleep(1.2)
    rgb, fid, _ts, age = cap.get_latest_rgb()
    cap.stop()
    if rgb is None:
        print("[abort] 无帧")
        return
    H, W = rgb.shape[:2]
    print(f"[frame] fid={fid} age={age:.1f}ms {W}x{H}")

    assets = json.loads(Path(
        "maaracing_assistant/plugins/treasure/resources/config/treasure_assets.json"
    ).read_text(encoding="utf-8"))
    image_dirs = [
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ]
    for name, a in sorted(assets["anchors"].items()):
        if a.get("kind") != "template":
            continue
        tpls = a.get("templates") or []
        if not tpls:
            continue
        rect = a.get("rect") or [0, 0, 1, 1]
        x1n, y1n, x2n, y2n = rect
        roi = (int(x1n * W), int(y1n * H),
               int((x2n - x1n) * W), int((y2n - y1n) * H))
        names = [Path(t).stem for t in tpls]
        box_r, score_r, _ = find_any_cs(np.ascontiguousarray(rgb), names, image_dirs,
                                        colorspace="rgb", threshold=0.75, roi=roi)
        box_f, score_f, hit_f = find_any_cs(np.ascontiguousarray(rgb), names, image_dirs,
                                            colorspace="rgb", threshold=0.75)
        mark = "OK  " if box_r else ("RECT" if box_f else "MISS")
        extra = ""
        if not box_r and box_f:
            extra = (f" 命中({box_f[0]},{box_f[1]})-({box_f[1]},{box_f[3]}) "
                     f"norm=({box_f[0]/W:.3f},{box_f[1]/H:.3f})-({box_f[2]/W:.3f},{box_f[3]/H:.3f})")
        print(f"[{mark}] {name:34} rect内={score_r:.3f} 整帧={score_f:.3f}"
              f"{f'@{hit_f}' if box_f else ''}{extra}")


if __name__ == "__main__":
    main()
