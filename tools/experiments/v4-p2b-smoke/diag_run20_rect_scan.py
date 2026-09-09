# -*- coding: utf-8 -*-
"""P2b 诊断 v20（rect 重校准扫描）：采集修复后的干净客户区帧上，双分数扫描。

旧 rect 全部在「含标题栏、底部裁切」的错位帧上校准（run10/11 时代），采集
修复后帧内容整体位移。对每个关键模板：整帧匹配（真位置）vs rect 内匹配
（当前可用性），数据定 rect 是否过时、应改为多少。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, ".")

from maaracing_assistant.core.wgcap import WgcCapture

user32 = ctypes.WinDLL("user32", use_last_error=True)
ROOT = Path(".")
ASSETS = ROOT / "maaracing_assistant/plugins/treasure/resources/config/treasure_assets.json"
IMG_DIRS = [ROOT / "maaracing_assistant/plugins/treasure/resources/image",
            ROOT / "maaracing_assistant/core/resources/image"]

TARGETS = ["hall_session_cards", "session_start_match_click"]
FRAME_PATH: Path | None = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    return int(h) if h else 0


def load_template(name: str) -> np.ndarray | None:
    for d in IMG_DIRS:
        for p in d.rglob(f"{name}.png"):
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img is not None:
                return img
    return None


def main() -> None:
    if FRAME_PATH is not None:  # 离线模式：直接读存证帧
        frame = cv2.imread(str(FRAME_PATH), cv2.IMREAD_COLOR)
        if frame is None:
            print(f"[abort] 读帧失败 {FRAME_PATH}")
            return
        H, W = frame.shape[:2]
        print(f"[frame] 离线 {W}x{H} {FRAME_PATH.name}")
        scan(frame)
        return
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
    frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    H, W = frame.shape[:2]
    print(f"[frame] {W}x{H} fid={fid} age={age:.1f}ms")
    scan(frame)


def scan(frame) -> None:
    H, W = frame.shape[:2]
    doc = json.loads(ASSETS.read_text(encoding="utf-8"))
    anchors = doc["anchors"]
    for name in TARGETS:
        a = anchors.get(name)
        tpl = load_template(name if name != "goto_appraise_btn" else "act_goto_appraise_btn")
        if tpl is None:
            tpl = load_template(name)
        if a is None or tpl is None:
            print(f"[skip] {name} 无锚点或无模板")
            continue
        x1, y1, x2, y2 = a["rect"]
        # 整帧匹配（灰度）
        g, gt = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
        full = cv2.matchTemplate(g, gt, cv2.TM_CCOEFF_NORMED)
        _minv, maxf, _minl, maxloc = cv2.minMaxLoc(full)
        th, tw = gt.shape
        fx, fy = maxloc
        # rect 内匹配
        rx1, ry1, rx2, ry2 = int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H)
        roi = g[max(ry1, 0):min(ry2, H), max(rx1, 0):min(rx2, W)]
        if roi.shape[0] >= th and roi.shape[1] >= tw:
            in_rect = cv2.matchTemplate(roi, gt, cv2.TM_CCOEFF_NORMED)
            _m, maxr, _ml, locr = cv2.minMaxLoc(in_rect)
            abs_x, abs_y = locr[0] + max(rx1, 0), locr[1] + max(ry1, 0)
        else:
            maxr, abs_x, abs_y = -1, -1, -1  # rect 装不下模板
        print(f"{name:28} rect=({x1:.3f},{y1:.3f},{x2:.3f},{y2:.3f}) "
              f"整帧={maxf:.3f}@({fx},{fy},{tw}x{th}) "
              f"rect内={maxr:.3f}@({abs_x},{abs_y})")
        if maxf > 0.8:
            nx1, ny1, nx2, ny2 = fx / W, fy / H, (fx + tw) / W, (fy + th) / H
            print(f"{'':28}建议 rect=({nx1:.3f}, {ny1:.3f}, {nx2:.3f}, {ny2:.3f})")


if __name__ == "__main__":
    main()
