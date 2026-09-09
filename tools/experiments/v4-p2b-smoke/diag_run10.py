# -*- coding: utf-8 -*-
"""P2b 诊断 v10：boot 的 44 个 Or 子项离线逐项检验（当前真实画面）。

run9 boot 未命中而 run8 单模板 0.973——分辨：
  子项参数在迁移中坏（离线逐项也不命中）→ 修迁移器；
  离线命中而框架不命中 → MaaFW 对大 Or/嵌套 Custom 的解析问题。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from collections import Counter
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

    trea = json.loads(Path(
        "maaracing_assistant/plugins/treasure/resources/nav/treasure.json"
    ).read_text(encoding="utf-8"))
    boot = trea["treasure.__boot.dwell"]
    subs = boot["recognition"]["param"]["any_of"]
    image_dirs = [
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ]
    print(f"[boot] 子项 {len(subs)} 个")
    forms = Counter(tuple(sorted(s.keys())) for s in subs)
    for form, c in forms.items():
        print(f"   形态 {form} x{c}")

    hits = 0
    for i, s in enumerate(subs):
        p = s.get("custom_recognition_param") or {}
        names = [n for n in p.get("templates", []) if isinstance(n, str) and n]
        rect = p.get("rect") or [0, 0, 1, 1]
        x1, y1, x2, y2 = rect
        roi = (int(x1 * W), int(y1 * H), int((x2 - x1) * W), int((y2 - y1) * H))
        box, score, name = find_any_cs(
            np.ascontiguousarray(rgb), names, image_dirs,
            colorspace=p.get("colorspace", "rgb"),
            threshold=float(p.get("threshold", 0.75)), roi=roi)
        if box:
            hits += 1
            print(f"[hit] 子项{i} {names} -> {name} score={score:.3f} box={box}")
    print(f"[verdict] 离线逐项命中 {hits}/{len(subs)}")


if __name__ == "__main__":
    main()
